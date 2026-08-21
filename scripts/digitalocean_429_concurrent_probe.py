#!/usr/bin/env python3
"""Continuously correlate ProjectHermes Codex 429s with a control curl probe.

The service watches Kubernetes logs for Codex workers while issuing the same
model request at a fixed cadence. The active worker model is discovered from
its controller-owned Pod environment, so Kimi, Qwen, GLM, and later models can
be tested sequentially without redeploying the probe. Every request is written
as a sanitized JSONL record. When a Codex 429 appears, an additional same-model
probe is started immediately and an incident file captures the requests before,
overlapping, and after the observed 429.

The API key is delivered to curl over stdin (not argv) and is removed from the
curl environment.  Authorization and cookie headers are redacted before any
record is written.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import Any


API_URL = "https://inference.do-ai.run/v1/chat/completions"
DEFAULT_PROBE_MODEL = "qwen3.8-max"
WORKER_SELECTOR = "app.kubernetes.io/name=project-hermes-codex-worker"
REDACTED = "<redacted>"
_DATE_DIRECTORY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REQUEST_ID_RE = re.compile(
    r"(?i)request(?:[_ -]?id)?[\"']?\s*[:=]\s*[\"']?"
    r"([0-9a-f]{8}-[0-9a-f-]{27,}|[A-Za-z0-9_-]{12,})"
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+)([^\s\r\n\"']+)"
)
_PROXY_CREDENTIAL_RE = re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@")
_COOKIE_LINE_RE = re.compile(r"(?im)^([<>]?\s*(?:set-cookie|cookie)\s*:).*$")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_z(value: dt.datetime | None = None) -> str:
    current = value or utc_now()
    return current.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def parse_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    normalized = value.strip()
    # Kubernetes timestamps may contain nanoseconds; datetime accepts microseconds.
    match = re.match(r"^(.*?\.)(\d+)(Z|[+-]\d\d:\d\d)$", normalized)
    if match:
        normalized = f"{match.group(1)}{match.group(2)[:6]}{match.group(3)}"
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def sanitize_text(value: str, secret: str = "") -> str:
    sanitized = value
    if secret:
        sanitized = sanitized.replace(secret, REDACTED)
    sanitized = _AUTHORIZATION_RE.sub(rf"\1{REDACTED}", sanitized)
    sanitized = _COOKIE_LINE_RE.sub(rf"\1 {REDACTED}", sanitized)
    sanitized = _PROXY_CREDENTIAL_RE.sub(rf"\1{REDACTED}@", sanitized)
    return sanitized


def sanitize_value(value: Any, secret: str = "") -> Any:
    if isinstance(value, str):
        return sanitize_text(value, secret)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in {"authorization", "cookie", "set-cookie"}:
                sanitized[key] = REDACTED
            else:
                sanitized[key] = sanitize_value(item, secret)
        return sanitized
    if isinstance(value, list):
        return [sanitize_value(item, secret) for item in value]
    if isinstance(value, tuple):
        return [sanitize_value(item, secret) for item in value]
    return value


def curl_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def probe_payload(model: str) -> dict[str, Any]:
    normalized = model.strip()
    if not normalized:
        raise ValueError("probe model cannot be empty")
    return {
        "model": normalized,
        "messages": [
            {
                "role": "user",
                "content": "What is the capital of France?",
            }
        ],
        "max_tokens": 100,
    }


def build_curl_stdin_config(
    api_key: str,
    model: str = DEFAULT_PROBE_MODEL,
) -> str:
    payload = json.dumps(
        probe_payload(model),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "\n".join(
        (
            f'url = "{curl_quote(API_URL)}"',
            'header = "Content-Type: application/json"',
            f'header = "Authorization: Bearer {curl_quote(api_key)}"',
            f'data = "{curl_quote(payload)}"',
            "verbose",
            "silent",
            "show-error",
            "",
        )
    )


def sanitized_logical_command(
    model: str = DEFAULT_PROBE_MODEL,
) -> list[str]:
    return [
        "curl",
        "-v",
        API_URL,
        "-H",
        "Content-Type: application/json",
        "-H",
        f"Authorization: Bearer {REDACTED}",
        "-d",
        json.dumps(
            probe_payload(model),
            separators=(",", ":"),
            ensure_ascii=False,
        ),
    ]


def parse_http_header_blocks(raw_headers: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in raw_headers.replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip("\r")
        if line.startswith("HTTP/"):
            if current is not None:
                blocks.append(current)
            parts = line.split(None, 2)
            current = {
                "status_line": line,
                "status": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None,
                "headers": {},
            }
            continue
        if current is None or not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        normalized_name = name.strip().lower()
        normalized_value = value.strip()
        existing = current["headers"].get(normalized_name)
        if existing is None:
            current["headers"][normalized_name] = normalized_value
        elif isinstance(existing, list):
            existing.append(normalized_value)
        else:
            current["headers"][normalized_name] = [existing, normalized_value]
    if current is not None:
        blocks.append(current)
    return blocks


def final_origin_response(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    if not blocks:
        return {"status_line": None, "status": None, "headers": {}}
    return blocks[-1]


def extract_request_id(text: str) -> str | None:
    match = _REQUEST_ID_RE.search(text)
    return match.group(1) if match else None


def is_model_429_line(text: str) -> bool:
    lowered = text.lower()
    if "429" not in lowered:
        return False
    return any(
        marker in lowered
        for marker in (
            "too many requests",
            "status: 429",
            '"status":429',
            '"status": 429',
            "http 429",
            "rate limit",
            "ratelimit",
        )
    )


@dataclasses.dataclass(frozen=True)
class Settings:
    api_key: str
    namespace: str
    log_root: Path
    probe_interval_seconds: float = 3.0
    log_poll_seconds: float = 0.75
    log_since_seconds: int = 15
    pre_event_window_seconds: float = 12.0
    post_event_window_seconds: float = 5.0
    stop_probes_after_429_seconds: float = 5.0
    curl_connect_timeout_seconds: float = 10.0
    curl_max_time_seconds: float = 45.0
    active_only: bool = True
    startup_probe: bool = False
    retention_days: int = 30
    default_probe_model: str = DEFAULT_PROBE_MODEL

    @classmethod
    def from_environment(cls) -> "Settings":
        api_key = os.environ.get("MODEL_ACCESS_KEY", "").strip()
        if not api_key:
            raise ValueError("MODEL_ACCESS_KEY is required")
        return cls(
            api_key=api_key,
            namespace=os.environ.get("POD_NAMESPACE", "project-hermes-jobs"),
            log_root=Path(
                os.environ.get(
                    "PROBE_LOG_ROOT", "/logs/digitalocean-429-concurrent-probe"
                )
            ),
            probe_interval_seconds=float(os.environ.get("PROBE_INTERVAL_SECONDS", "3")),
            log_poll_seconds=float(os.environ.get("LOG_POLL_SECONDS", "0.75")),
            log_since_seconds=int(os.environ.get("LOG_SINCE_SECONDS", "15")),
            pre_event_window_seconds=float(
                os.environ.get("PRE_EVENT_WINDOW_SECONDS", "12")
            ),
            post_event_window_seconds=float(
                os.environ.get("POST_EVENT_WINDOW_SECONDS", "5")
            ),
            stop_probes_after_429_seconds=float(
                os.environ.get("STOP_PROBES_AFTER_429_SECONDS", "5")
            ),
            curl_connect_timeout_seconds=float(
                os.environ.get("CURL_CONNECT_TIMEOUT_SECONDS", "10")
            ),
            curl_max_time_seconds=float(os.environ.get("CURL_MAX_TIME_SECONDS", "45")),
            active_only=os.environ.get("PROBE_ACTIVE_ONLY", "true").lower()
            not in {"0", "false", "no"},
            # Monitor mode must discover the active Worker's exact model
            # before it sends anything.  ``--once`` can still use PROBE_MODEL.
            startup_probe=os.environ.get("STARTUP_PROBE", "false").lower()
            not in {"0", "false", "no"},
            retention_days=int(os.environ.get("PROBE_RETENTION_DAYS", "30")),
            default_probe_model=(
                os.environ.get("PROBE_MODEL", DEFAULT_PROBE_MODEL).strip()
                or DEFAULT_PROBE_MODEL
            ),
        )


class ProbeStorage:
    def __init__(self, root: Path, *, secret: str, retention_days: int) -> None:
        self.root = root.resolve()
        self.secret = secret
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            self.root.chmod(0o700)

    def _day_directory(self, timestamp: str | None = None) -> Path:
        parsed = parse_timestamp(timestamp) or utc_now()
        directory = self.root / parsed.date().isoformat()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory

    def _serialize(self, record: dict[str, Any]) -> str:
        sanitized = sanitize_value(record, self.secret)
        encoded = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
        if self.secret and self.secret in encoded:
            raise RuntimeError("refusing to write a record containing MODEL_ACCESS_KEY")
        return encoded

    def append(self, stream: str, record: dict[str, Any]) -> Path:
        line = self._serialize(record)
        directory = self._day_directory(str(record.get("recorded_at", "")))
        destination = directory / f"{stream}.jsonl"
        with self._lock:
            with destination.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")
            with contextlib.suppress(OSError):
                destination.chmod(0o600)
        return destination

    def write_incident(self, event_id: str, incident: dict[str, Any]) -> Path:
        data = self._serialize(incident)
        directory = self._day_directory(str(incident.get("recorded_at", ""))) / "incidents"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / f"{event_id}.json"
        temporary = directory / f".{event_id}.{uuid.uuid4().hex}.tmp"
        with self._lock:
            temporary.write_text(f"{data}\n", encoding="utf-8")
            with contextlib.suppress(OSError):
                temporary.chmod(0o600)
            temporary.replace(destination)
        return destination

    def heartbeat(self, status: dict[str, Any]) -> None:
        data = self._serialize(status)
        temporary = self.root / f".heartbeat.{uuid.uuid4().hex}.tmp"
        destination = self.root / "heartbeat.json"
        with self._lock:
            temporary.write_text(f"{data}\n", encoding="utf-8")
            with contextlib.suppress(OSError):
                temporary.chmod(0o600)
            temporary.replace(destination)

    def cleanup(self) -> list[str]:
        if self.retention_days <= 0:
            return []
        cutoff = utc_now().date() - dt.timedelta(days=self.retention_days)
        removed: list[str] = []
        for child in self.root.iterdir():
            if not child.is_dir() or not _DATE_DIRECTORY_RE.fullmatch(child.name):
                continue
            try:
                child_date = dt.date.fromisoformat(child.name)
            except ValueError:
                continue
            if child_date >= cutoff:
                continue
            shutil.rmtree(child)
            removed.append(child.name)
        return removed


class CurlProbe:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(
        self,
        *,
        model: str,
        reason: str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        payload = probe_payload(model)
        probe_id = f"probe-{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:10]}"
        started = utc_now()
        started_mono = time.monotonic()
        curl_metrics_format = json.dumps(
            {
                "http_code": "%{http_code}",
                "time_total": "%{time_total}",
                "time_connect": "%{time_connect}",
                "time_appconnect": "%{time_appconnect}",
                "time_starttransfer": "%{time_starttransfer}",
                "remote_ip": "%{remote_ip}",
                "http_version": "%{http_version}",
                "num_connects": "%{num_connects}",
                "size_download": "%{size_download}",
            },
            separators=(",", ":"),
        )
        with tempfile.TemporaryDirectory(prefix="do-429-probe-") as temporary:
            temporary_path = Path(temporary)
            body_path = temporary_path / "body"
            headers_path = temporary_path / "headers"
            command = [
                shutil.which("curl") or "/usr/bin/curl",
                "--disable",
                "--config",
                "-",
                "--output",
                str(body_path),
                "--dump-header",
                str(headers_path),
                "--connect-timeout",
                str(self.settings.curl_connect_timeout_seconds),
                "--max-time",
                str(self.settings.curl_max_time_seconds),
                "--write-out",
                curl_metrics_format,
            ]
            curl_environment = os.environ.copy()
            curl_environment.pop("MODEL_ACCESS_KEY", None)
            try:
                completed = subprocess.run(
                    command,
                    input=build_curl_stdin_config(
                        self.settings.api_key,
                        model,
                    ),
                    text=True,
                    capture_output=True,
                    timeout=self.settings.curl_max_time_seconds + 10,
                    check=False,
                    env=curl_environment,
                )
                exit_code = completed.returncode
                metrics_text = completed.stdout.strip()
                verbose = completed.stderr
                process_error = None
            except subprocess.TimeoutExpired as exc:
                exit_code = 124
                metrics_text = ""
                verbose = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
                process_error = "curl subprocess exceeded its outer timeout"
            completed_at = utc_now()
            duration_ms = round((time.monotonic() - started_mono) * 1000, 3)
            body = body_path.read_text(encoding="utf-8", errors="replace") if body_path.exists() else ""
            raw_headers = (
                headers_path.read_text(encoding="utf-8", errors="replace")
                if headers_path.exists()
                else ""
            )

        sanitized_headers = sanitize_text(raw_headers, self.settings.api_key)
        header_blocks = parse_http_header_blocks(sanitized_headers)
        origin = final_origin_response(header_blocks)
        metrics: dict[str, Any] = {}
        if metrics_text:
            with contextlib.suppress(json.JSONDecodeError):
                metrics = json.loads(metrics_text)
        origin_headers = origin.get("headers", {})
        request_id = origin_headers.get("x-request-id") or origin_headers.get("request-id")
        if isinstance(request_id, list):
            request_id = request_id[-1]
        parsed_body: Any = None
        with contextlib.suppress(json.JSONDecodeError):
            parsed_body = json.loads(body)
        rate_limits = {
            key: value
            for key, value in origin_headers.items()
            if key.startswith("x-ratelimit-")
        }
        return sanitize_value(
            {
                "schema_version": "digitalocean-concurrent-probe.v1",
                "record_type": "model_probe",
                "recorded_at": iso_z(completed_at),
                "probe_id": probe_id,
                "model": model,
                "reason": reason,
                "related_429_event_id": event_id,
                "started_at": iso_z(started),
                "completed_at": iso_z(completed_at),
                "wall_duration_ms": duration_ms,
                "logical_command": sanitized_logical_command(model),
                "request": {
                    "url": API_URL,
                    "method": "POST",
                    "body": payload,
                    "proxy": os.environ.get("HTTPS_PROXY")
                    or os.environ.get("https_proxy"),
                },
                "curl": {
                    "exit_code": exit_code,
                    "metrics": metrics,
                    "verbose": verbose,
                    "process_error": process_error,
                },
                "response": {
                    "status": origin.get("status"),
                    "status_line": origin.get("status_line"),
                    "request_id": request_id,
                    "headers": origin_headers,
                    "header_blocks": header_blocks,
                    "rate_limits": rate_limits,
                    "body": parsed_body if parsed_body is not None else body,
                },
            },
            self.settings.api_key,
        )


@dataclasses.dataclass(frozen=True)
class WorkerPod:
    name: str
    uid: str
    phase: str
    job_name: str | None
    execution_id: str | None
    task_id: str | None
    repository: str | None
    model: str | None
    provider_endpoint: str | None
    provider_wire_api: str | None

    @property
    def active(self) -> bool:
        return self.phase in {"Pending", "Running"}


class KubernetesClient:
    def __init__(self, namespace: str) -> None:
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        if not host:
            raise ValueError("KUBERNETES_SERVICE_HOST is required in monitor mode")
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        self.base_url = f"https://{host}:{port}"
        self.namespace = namespace
        self.token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        context = ssl.create_default_context(cafile=ca_path)
        # The model probe intentionally uses HTTPS_PROXY. Kubernetes API calls must not.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
        )

    def _get(self, path: str, query: dict[str, Any] | None = None) -> bytes:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        token = self.token_path.read_text(encoding="utf-8").strip()
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        with self.opener.open(request, timeout=10) as response:
            return response.read(4 * 1024 * 1024)

    def list_worker_pods(self) -> list[WorkerPod]:
        path = f"/api/v1/namespaces/{urllib.parse.quote(self.namespace)}/pods"
        payload = json.loads(
            self._get(path, {"labelSelector": WORKER_SELECTOR}).decode("utf-8")
        )
        pods: list[WorkerPod] = []
        for item in payload.get("items", []):
            metadata = item.get("metadata", {})
            labels = metadata.get("labels", {})
            values: dict[str, str] = {}
            for container in item.get("spec", {}).get("containers", []):
                if container.get("name") != "codex":
                    continue
                for entry in container.get("env", []):
                    if "value" in entry:
                        values[str(entry.get("name"))] = str(entry["value"])
            pods.append(
                WorkerPod(
                    name=str(metadata.get("name", "")),
                    uid=str(metadata.get("uid", "")),
                    phase=str(item.get("status", {}).get("phase", "Unknown")),
                    job_name=labels.get("batch.kubernetes.io/job-name")
                    or labels.get("job-name"),
                    execution_id=values.get("PROJECT_HERMES_EXECUTION_ID"),
                    task_id=values.get("PROJECT_HERMES_TASK_ID"),
                    repository=values.get("PROJECT_HERMES_REPOSITORY"),
                    model=values.get("PROJECT_HERMES_MODEL"),
                    provider_endpoint=values.get(
                        "PROJECT_HERMES_PROVIDER_ENDPOINT"
                    ),
                    provider_wire_api=values.get(
                        "PROJECT_HERMES_PROVIDER_WIRE_API"
                    ),
                )
            )
        return pods

    def recent_logs(self, pod: WorkerPod, since_seconds: int) -> str:
        path = (
            f"/api/v1/namespaces/{urllib.parse.quote(self.namespace)}/pods/"
            f"{urllib.parse.quote(pod.name)}/log"
        )
        try:
            return self._get(
                path,
                {
                    "container": "codex",
                    "timestamps": "true",
                    "sinceSeconds": str(since_seconds),
                    "limitBytes": str(2 * 1024 * 1024),
                },
            ).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 404}:
                return ""
            raise


class LogEventDetector:
    def __init__(self, *, secret: str) -> None:
        self.secret = secret
        self._seen_lines: dict[str, tuple[deque[str], set[str]]] = {}
        self._seen_events_order: deque[str] = deque()
        self._seen_events: set[str] = set()

    def _new_line(self, pod_uid: str, line: str) -> bool:
        order, values = self._seen_lines.setdefault(pod_uid, (deque(), set()))
        digest = hashlib.sha256(line.encode("utf-8", errors="replace")).hexdigest()
        if digest in values:
            return False
        order.append(digest)
        values.add(digest)
        while len(order) > 10_000:
            values.discard(order.popleft())
        return True

    def inspect(self, pod: WorkerPod, log_text: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for raw_line in log_text.splitlines():
            if not raw_line or not self._new_line(pod.uid, raw_line):
                continue
            first, separator, remainder = raw_line.partition(" ")
            source_time = parse_timestamp(first) if separator else None
            content = remainder if source_time is not None else raw_line
            if not is_model_429_line(content):
                continue
            request_id = extract_request_id(content)
            event_key = request_id or hashlib.sha256(
                f"{pod.uid}\0{content}".encode("utf-8", errors="replace")
            ).hexdigest()
            if event_key in self._seen_events:
                continue
            self._seen_events.add(event_key)
            self._seen_events_order.append(event_key)
            while len(self._seen_events_order) > 2_000:
                self._seen_events.discard(self._seen_events_order.popleft())
            detected = utc_now()
            events.append(
                sanitize_value(
                    {
                        "schema_version": "digitalocean-429-event.v2",
                        "record_type": "codex_429_detected",
                        "recorded_at": iso_z(detected),
                        "event_id": f"codex-429-{uuid.uuid4().hex}",
                        "watcher_detected_at": iso_z(detected),
                        "source_log_at": iso_z(source_time) if source_time else None,
                        "codex_request_id": request_id,
                        "model": pod.model,
                        "error": content,
                        "worker": dataclasses.asdict(pod),
                    },
                    self.secret,
                )
            )
        return events


def correlate_probes(
    event: dict[str, Any],
    probes: list[dict[str, Any]],
    *,
    pre_seconds: float,
    post_seconds: float,
) -> dict[str, Any]:
    event_time = (
        parse_timestamp(str(event.get("source_log_at", "")))
        or parse_timestamp(str(event.get("watcher_detected_at", "")))
        or utc_now()
    )
    event_model = str(event.get("model") or "").strip()
    lower = event_time - dt.timedelta(seconds=pre_seconds)
    upper = event_time + dt.timedelta(seconds=post_seconds)
    related: list[dict[str, Any]] = []
    for probe in probes:
        if event_model and str(probe.get("model") or "") != event_model:
            continue
        started = parse_timestamp(str(probe.get("started_at", "")))
        completed = parse_timestamp(str(probe.get("completed_at", "")))
        if started is None or completed is None or completed < lower or started > upper:
            continue
        if started <= event_time <= completed:
            relation = "overlap"
            delta_ms = 0.0
        elif completed < event_time:
            relation = "before"
            delta_ms = round((event_time - completed).total_seconds() * 1000, 3)
        else:
            relation = "after"
            delta_ms = round((started - event_time).total_seconds() * 1000, 3)
        correlated = dict(probe)
        correlated["relation_to_429_detection"] = relation
        correlated["gap_to_429_detection_ms"] = delta_ms
        related.append(correlated)
    related.sort(key=lambda item: str(item.get("started_at", "")))
    overlap = [item for item in related if item["relation_to_429_detection"] == "overlap"]
    before = [item for item in related if item["relation_to_429_detection"] == "before"]
    after = [item for item in related if item["relation_to_429_detection"] == "after"]
    nearest_before = min(before, key=lambda item: item["gap_to_429_detection_ms"], default=None)
    nearest_after = min(after, key=lambda item: item["gap_to_429_detection_ms"], default=None)
    return {
        "correlation_at": iso_z(event_time),
        "model": event_model or None,
        "probes": related,
        "overlapping_probe_ids": [item.get("probe_id") for item in overlap],
        "nearest_before_probe_id": nearest_before.get("probe_id") if nearest_before else None,
        "nearest_after_probe_id": nearest_after.get("probe_id") if nearest_after else None,
    }


def select_active_probe_target(pods: list[WorkerPod]) -> WorkerPod | None:
    """Select exactly one eligible DigitalOcean Codex worker."""

    active = [pod for pod in pods if pod.active]
    if len(active) != 1:
        return None
    target = active[0]
    if (
        not target.model
        or target.provider_endpoint != "https://inference.do-ai.run/v1"
        or target.provider_wire_api != "responses"
    ):
        return None
    return target


def should_run_continuous_probe(
    *,
    active_worker_count: int,
    paused_after_429: bool,
    stop_at_monotonic: float | None,
    now_monotonic: float,
    active_only: bool,
) -> bool:
    """Return whether another periodic control request may start.

    Once a 429 is observed, the five-second tail window takes precedence over
    worker liveness so a fast-exiting Codex Pod cannot cut the evidence short.
    At the cutoff, no new request starts; an already-running curl is allowed to
    finish and be persisted.
    """

    if paused_after_429:
        return False
    if stop_at_monotonic is not None:
        return now_monotonic < stop_at_monotonic
    return active_worker_count > 0 or not active_only


class ConcurrentProbeMonitor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = ProbeStorage(
            settings.log_root,
            secret=settings.api_key,
            retention_days=settings.retention_days,
        )
        self.probe = CurlProbe(settings)
        self.kubernetes = KubernetesClient(settings.namespace)
        self.detector = LogEventDetector(secret=settings.api_key)
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=3, thread_name_prefix="model-probe"
        )
        self.stop_event = threading.Event()
        self._record_lock = threading.Lock()
        self._probe_records: deque[dict[str, Any]] = deque(maxlen=512)
        self._pending_events: dict[str, tuple[dict[str, Any], float]] = {}
        self._probe_futures: set[concurrent.futures.Future[dict[str, Any]]] = set()
        self._periodic_future: concurrent.futures.Future[dict[str, Any]] | None = None
        self._active_worker_names: list[str] = []
        self._active_target_identity: tuple[str, str] | None = None
        self._active_probe_model: str | None = None
        self._probe_stop_at_monotonic: float | None = None
        self._probe_stop_at_utc: str | None = None
        self._probes_paused_after_429 = False
        self._last_cleanup = 0.0
        self._last_heartbeat = 0.0

    def request_stop(self, *_args: Any) -> None:
        self.stop_event.set()

    def _service_record(self, level: str, message: str, **fields: Any) -> None:
        record = sanitize_value(
            {
                "schema_version": "digitalocean-probe-service.v1",
                "record_type": "service",
                "recorded_at": iso_z(),
                "level": level,
                "message": message,
                **fields,
            },
            self.settings.api_key,
        )
        self.storage.append("service", record)
        print(json.dumps(record, ensure_ascii=False, separators=(",", ":")), flush=True)

    def _on_probe_complete(
        self, future: concurrent.futures.Future[dict[str, Any]]
    ) -> None:
        try:
            record = future.result()
        except Exception as exc:  # noqa: BLE001 - service boundary
            self._service_record(
                "error", "probe execution failed", error=sanitize_text(str(exc), self.settings.api_key)
            )
        else:
            self.storage.append("probes", record)
            with self._record_lock:
                self._probe_records.append(record)
            summary = {
                "schema_version": "digitalocean-probe-service.v1",
                "record_type": "probe_summary",
                "recorded_at": record["recorded_at"],
                "probe_id": record["probe_id"],
                "model": record["model"],
                "reason": record["reason"],
                "http_status": record["response"]["status"],
                "request_id": record["response"]["request_id"],
                "duration_ms": record["wall_duration_ms"],
                "curl_exit_code": record["curl"]["exit_code"],
            }
            print(
                json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                flush=True,
            )
        finally:
            with self._record_lock:
                self._probe_futures.discard(future)

    def _schedule_probe(
        self,
        *,
        model: str,
        reason: str,
        event_id: str | None = None,
    ) -> concurrent.futures.Future[dict[str, Any]]:
        future = self.executor.submit(
            self.probe.run,
            model=model,
            reason=reason,
            event_id=event_id,
        )
        with self._record_lock:
            self._probe_futures.add(future)
        future.add_done_callback(self._on_probe_complete)
        return future

    def _handle_event(self, event: dict[str, Any]) -> None:
        now_monotonic = time.monotonic()
        if (
            self._probe_stop_at_monotonic is None
            and not self._probes_paused_after_429
        ):
            tail_seconds = self.settings.stop_probes_after_429_seconds
            self._probe_stop_at_monotonic = now_monotonic + tail_seconds
            self._probe_stop_at_utc = iso_z(
                utc_now() + dt.timedelta(seconds=tail_seconds)
            )
        event = {
            **event,
            "continuous_probe_stop_scheduled_at": self._probe_stop_at_utc,
            "continuous_probe_tail_seconds": (
                self.settings.stop_probes_after_429_seconds
            ),
        }
        self.storage.append("events", event)
        self._pending_events[event["event_id"]] = (
            event,
            time.monotonic() + self.settings.post_event_window_seconds,
        )
        within_tail = (
            not self._probes_paused_after_429
            and self._probe_stop_at_monotonic is not None
            and now_monotonic < self._probe_stop_at_monotonic
        )
        self._service_record(
            "warning",
            (
                "Codex 429 detected; immediate same-model control probe started"
                if within_tail
                else "Codex 429 detected after continuous probes paused"
            ),
            event_id=event["event_id"],
            codex_request_id=event.get("codex_request_id"),
            model=event.get("model"),
            worker=event.get("worker"),
            continuous_probe_stop_scheduled_at=self._probe_stop_at_utc,
        )
        event_model = str(event.get("model") or "").strip()
        if within_tail and event_model:
            self._schedule_probe(
                model=event_model,
                reason="codex_429_event",
                event_id=event["event_id"],
            )

    def _finalize_due_events(self) -> None:
        due = [
            event_id
            for event_id, (_event, deadline) in self._pending_events.items()
            if deadline <= time.monotonic()
        ]
        for event_id in due:
            with self._record_lock:
                probe_in_flight = any(
                    not future.done() for future in self._probe_futures
                )
            if probe_in_flight:
                continue
            event, _deadline = self._pending_events.pop(event_id)
            with self._record_lock:
                probes = list(self._probe_records)
            correlation = correlate_probes(
                event,
                probes,
                pre_seconds=self.settings.pre_event_window_seconds,
                post_seconds=self.settings.post_event_window_seconds,
            )
            recorded_at = iso_z()
            incident = {
                "schema_version": "digitalocean-429-incident.v2",
                "record_type": "codex_429_incident",
                "recorded_at": recorded_at,
                "event": event,
                "correlation_clock": (
                    "source_log_at"
                    if event.get("source_log_at")
                    else "watcher_detected_at"
                ),
                "pre_event_window_seconds": self.settings.pre_event_window_seconds,
                "post_event_window_seconds": self.settings.post_event_window_seconds,
                **correlation,
            }
            path = self.storage.write_incident(event_id, incident)
            finalized = {
                "schema_version": "digitalocean-429-event.v2",
                "record_type": "codex_429_finalized",
                "recorded_at": recorded_at,
                "event_id": event_id,
                "codex_request_id": event.get("codex_request_id"),
                "model": event.get("model"),
                "incident_path": str(path),
                "probe_count": len(correlation["probes"]),
                "overlapping_probe_ids": correlation["overlapping_probe_ids"],
                "nearest_before_probe_id": correlation["nearest_before_probe_id"],
                "nearest_after_probe_id": correlation["nearest_after_probe_id"],
            }
            self.storage.append("events", finalized)
            self._service_record(
                "info",
                "429 incident finalized",
                **{key: value for key, value in finalized.items() if key != "message"},
            )

    def _write_heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat < 10:
            return
        self._last_heartbeat = now
        self.storage.heartbeat(
            {
                "schema_version": "digitalocean-probe-heartbeat.v1",
                "record_type": "heartbeat",
                "recorded_at": iso_z(),
                "active_workers": self._active_worker_names,
                "active_probe_model": self._active_probe_model,
                "pending_429_incidents": sorted(self._pending_events),
                "continuous_probe_state": (
                    "paused_after_429"
                    if self._probes_paused_after_429
                    else (
                        "429_tail"
                        if self._probe_stop_at_monotonic is not None
                        else ("active" if self._active_worker_names else "idle")
                    )
                ),
                "continuous_probe_stop_scheduled_at": self._probe_stop_at_utc,
            }
        )

    def _arm_for_new_worker(self) -> None:
        if not (
            self._probes_paused_after_429
            or self._probe_stop_at_monotonic is not None
        ):
            return
        self._probe_stop_at_monotonic = None
        self._probe_stop_at_utc = None
        self._probes_paused_after_429 = False
        self._service_record(
            "info",
            "continuous same-model probe re-armed for a new Codex worker",
            model=self._active_probe_model,
        )

    def _pause_if_tail_elapsed(self, now_monotonic: float) -> None:
        if (
            self._probe_stop_at_monotonic is None
            or self._probes_paused_after_429
            or now_monotonic < self._probe_stop_at_monotonic
        ):
            return
        self._probes_paused_after_429 = True
        self._service_record(
            "info",
            "continuous same-model probes paused five seconds after Codex 429",
            model=self._active_probe_model,
            continuous_probe_stop_scheduled_at=self._probe_stop_at_utc,
        )

    def _cleanup_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < 3600:
            return
        self._last_cleanup = now
        removed = self.storage.cleanup()
        if removed:
            self._service_record(
                "info",
                "expired probe log directories removed",
                retention_days=self.settings.retention_days,
                removed=removed,
            )

    def run(self) -> int:
        self._service_record(
            "info",
            "DigitalOcean concurrent 429 probe monitor started",
            namespace=self.settings.namespace,
            worker_selector=WORKER_SELECTOR,
            probe_interval_seconds=self.settings.probe_interval_seconds,
            active_only=self.settings.active_only,
            startup_probe=self.settings.startup_probe,
            retention_days=self.settings.retention_days,
            stop_probes_after_429_seconds=(
                self.settings.stop_probes_after_429_seconds
            ),
            probe_model_mode="active_worker",
            manual_probe_model=self.settings.default_probe_model,
        )
        if self.settings.startup_probe:
            self._schedule_probe(
                model=self.settings.default_probe_model,
                reason="service_startup",
            )
        next_periodic = time.monotonic()
        while not self.stop_event.is_set():
            try:
                pods = self.kubernetes.list_worker_pods()
                active_names = sorted(pod.name for pod in pods if pod.active)
                target = select_active_probe_target(pods)
                target_identity = (
                    (target.uid, target.model)
                    if target is not None and target.model is not None
                    else None
                )
                if (
                    active_names != self._active_worker_names
                    or target_identity != self._active_target_identity
                ):
                    previous_active_names = self._active_worker_names
                    previous_target_identity = self._active_target_identity
                    self._active_target_identity = target_identity
                    self._active_probe_model = (
                        target.model if target is not None else None
                    )
                    if target_identity is not None and (
                        not previous_active_names
                        or target_identity != previous_target_identity
                    ):
                        self._arm_for_new_worker()
                    self._active_worker_names = active_names
                    self._service_record(
                        "info",
                        "active Codex worker set changed",
                        active_workers=active_names,
                        selected_worker=(
                            dataclasses.asdict(target)
                            if target is not None
                            else None
                        ),
                        active_probe_model=self._active_probe_model,
                        selection_blocker=(
                            None
                            if target is not None or not active_names
                            else (
                                "exactly one active DigitalOcean Responses "
                                "worker with a declared model is required"
                            )
                        ),
                    )
                for pod in pods:
                    if not pod.active:
                        continue
                    logs = self.kubernetes.recent_logs(pod, self.settings.log_since_seconds)
                    for event in self.detector.inspect(pod, logs):
                        self._handle_event(event)
            except Exception as exc:  # noqa: BLE001 - resilient monitor loop
                self._service_record(
                    "error",
                    "Kubernetes log polling failed",
                    error=sanitize_text(str(exc), self.settings.api_key),
                )

            now = time.monotonic()
            self._pause_if_tail_elapsed(now)
            should_probe = should_run_continuous_probe(
                active_worker_count=(
                    1 if self._active_probe_model is not None else 0
                ),
                paused_after_429=self._probes_paused_after_429,
                stop_at_monotonic=self._probe_stop_at_monotonic,
                now_monotonic=now,
                active_only=self.settings.active_only,
            )
            periodic_done = self._periodic_future is None or self._periodic_future.done()
            if (
                should_probe
                and self._active_probe_model is not None
                and periodic_done
                and now >= next_periodic
            ):
                self._periodic_future = self._schedule_probe(
                    model=self._active_probe_model,
                    reason="periodic_worker_active",
                )
                next_periodic = now + self.settings.probe_interval_seconds
            elif not should_probe:
                next_periodic = now

            self._finalize_due_events()
            self._write_heartbeat()
            self._cleanup_if_due()
            self.stop_event.wait(self.settings.log_poll_seconds)

        self._service_record("info", "DigitalOcean concurrent 429 probe monitor stopping")
        self.executor.shutdown(wait=True, cancel_futures=False)
        self._finalize_due_events()
        return 0


def run_once(settings: Settings) -> int:
    record = CurlProbe(settings).run(
        model=settings.default_probe_model,
        reason="manual_once",
    )
    storage = ProbeStorage(
        settings.log_root,
        secret=settings.api_key,
        retention_days=settings.retention_days,
    )
    path = storage.append("probes", record)
    summary = {
        "probe_id": record["probe_id"],
        "model": record["model"],
        "http_status": record["response"]["status"],
        "request_id": record["response"]["request_id"],
        "curl_exit_code": record["curl"]["exit_code"],
        "duration_ms": record["wall_duration_ms"],
        "log_path": str(path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if record["curl"]["exit_code"] == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one sanitized control probe without watching Kubernetes",
    )
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_environment()
    except (ValueError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.once:
        return run_once(settings)
    try:
        monitor = ConcurrentProbeMonitor(settings)
    except (ValueError, OSError) as exc:
        print(f"startup error: {exc}", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, monitor.request_stop)
    signal.signal(signal.SIGINT, monitor.request_stop)
    return monitor.run()


if __name__ == "__main__":
    raise SystemExit(main())
