"""Per-task Codex supervisor, Unix-socket client, and AgentRuntime adapter."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator
from uuid import uuid4

from project_hermes.config import (
    CodexTaskRuntimeConfig,
    ProjectHermesConfig,
    assert_private_directory,
)
from project_hermes.models import utc_now
from project_hermes.redaction import redact_text
from project_hermes.runtime.base import (
    RuntimeEvent,
    RuntimeHandle,
    RuntimeModelRoute,
    RuntimeRequest,
    RuntimeResult,
    RuntimeStatus,
)
from project_hermes.runtime.codex_protocol import (
    CodexCommand,
    CodexCommandKind,
    CodexReply,
    CodexTaskSpec,
)

_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class CodexSocketClient:
    """Short-lived client for one task-private Unix socket."""

    def __init__(self, socket_path: Path, *, timeout: float = 30.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def request(self, command: CodexCommand) -> dict[str, Any]:
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("ProjectHermes Codex transport requires Unix sockets")
        encoded = (
            json.dumps(command.model_dump(mode="json"), ensure_ascii=True) + "\n"
        ).encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout)
            client.connect(str(self.socket_path))
            client.sendall(encoded)
            response = self._read_line(client)
        reply = CodexReply.model_validate_json(response)
        if reply.request_id not in {command.request_id, "unknown"}:
            raise RuntimeError("Codex daemon response id does not match request")
        if not reply.ok:
            raise RuntimeError(reply.error or "Codex daemon request failed")
        return reply.payload

    @staticmethod
    def _read_line(client: socket.socket) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = client.recv(65_536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_RESPONSE_BYTES:
                raise RuntimeError("Codex daemon response exceeds 16 MiB")
            if b"\n" in chunk:
                break
        payload = b"".join(chunks)
        line, _, _remainder = payload.partition(b"\n")
        if not line:
            raise RuntimeError("Codex daemon closed without a response")
        return line


class TaskCodexSupervisor:
    """Provision and reconnect to exactly one daemon per task."""

    def __init__(
        self,
        config: ProjectHermesConfig,
        *,
        python_executable: str = sys.executable,
    ) -> None:
        self.config = config
        self.python_executable = python_executable
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.RLock()

    def provision(
        self,
        request: RuntimeRequest,
    ) -> CodexTaskSpec:
        """Start or reconnect to a task daemon."""

        codex = self.config.codex
        if not codex.enabled:
            raise RuntimeError(
                "per-task Codex runtime is disabled in ProjectHermes config"
            )
        if request.cwd is None:
            raise ValueError("Codex task runtime requires an exclusive worktree cwd")
        worktree = Path(request.cwd).resolve()
        if not worktree.is_dir():
            raise ValueError(f"worktree does not exist: {worktree}")

        route = self._resolve_model_route(codex, request)
        spec = CodexTaskSpec(
            task_id=request.task_id,
            session_id=request.session_id or f"codex-{uuid4().hex}",
            worktree_path=worktree,
            runtime_root=codex.runtime_root.resolve(),
            sdk_version=codex.sdk_version,
            cli_version=codex.cli_version,
            model_profile=route.profile,
            model=route.model,
            model_provider=route.model_provider,
            provider_endpoint=route.provider_endpoint,
            provider_api_key_env=route.provider_api_key_env,
            provider_wire_api=route.provider_wire_api,
            reasoning_mode=route.reasoning_mode,
            reasoning_effort=route.reasoning_effort,
            context_window=route.context_window,
            runtime_generation=request.runtime_generation,
            credentials_file=codex.credentials_file,
            network_access=codex.network_access,
            turn_timeout_seconds=codex.turn_timeout_seconds,
        )
        assert_private_directory(spec.runtime_root)
        assert_private_directory(spec.task_directory())
        assert_private_directory(spec.state_directory())

        with self._lock, self._task_lock(spec):
            if self._is_ready(spec):
                return self._load_existing_spec(spec)
            self._prepare_restart(spec)
            self._start_daemon(spec, codex)
        return spec

    def client(
        self,
        spec: CodexTaskSpec,
        *,
        timeout: float | None = None,
    ) -> CodexSocketClient:
        return CodexSocketClient(
            spec.socket_path(),
            timeout=timeout or self.config.codex.turn_timeout_seconds + 30,
        )

    def status(self, spec: CodexTaskSpec) -> dict[str, Any]:
        return self._command(spec, CodexCommandKind.STATUS)

    def events(
        self, spec: CodexTaskSpec, *, after_sequence: int = 0
    ) -> list[dict[str, Any]]:
        payload = self._command(
            spec,
            CodexCommandKind.EVENTS,
            after_sequence=after_sequence,
        )
        events = payload.get("events") or []
        return events if isinstance(events, list) else []

    def result(self, spec: CodexTaskSpec) -> dict[str, Any]:
        return self._command(spec, CodexCommandKind.RESULT)

    def run_turn(self, spec: CodexTaskSpec, prompt: str) -> dict[str, Any]:
        return self._command(
            spec,
            CodexCommandKind.RUN_TURN,
            prompt=prompt,
        )

    def cancel(self, spec: CodexTaskSpec) -> bool:
        payload = self._command(spec, CodexCommandKind.CANCEL)
        return bool(payload.get("cancel_requested"))

    def stop(self, spec: CodexTaskSpec, *, timeout: float = 10.0) -> None:
        with self._lock, self._task_lock(spec):
            try:
                self._command(spec, CodexCommandKind.SHUTDOWN)
            except (FileNotFoundError, ConnectionError, OSError, RuntimeError):
                pass
            deadline = time.monotonic() + timeout
            while (
                spec.socket_path().exists()
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            process = self._processes.pop(spec.task_id, None)
            if process is not None and process.poll() is None:
                try:
                    process.wait(
                        timeout=max(0.1, deadline - time.monotonic())
                    )
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=3)

    def _command(
        self,
        spec: CodexTaskSpec,
        kind: CodexCommandKind,
        *,
        prompt: str | None = None,
        after_sequence: int = 0,
    ) -> dict[str, Any]:
        return self.client(spec).request(
            CodexCommand(
                request_id=f"request-{uuid4().hex}",
                command=kind,
                prompt=prompt,
                after_sequence=after_sequence,
            )
        )

    def _start_daemon(
        self,
        spec: CodexTaskSpec,
        codex: CodexTaskRuntimeConfig,
    ) -> None:
        spec_path = spec.daemon_spec_path()
        spec_path.write_text(
            json.dumps(
                spec.model_dump(mode="json"),
                sort_keys=True,
                indent=2,
                ensure_ascii=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(spec_path, 0o600)
        log_path = spec.state_directory() / "daemon.log"
        environment = self._sanitized_environment()
        command = [
            self.python_executable,
            "-m",
            "project_hermes.runtime.codex_daemon",
            "--spec",
            str(spec_path),
        ]
        repository_root = Path(__file__).resolve().parents[2]
        with log_path.open("ab", buffering=0) as log:
            process = subprocess.Popen(
                command,
                cwd=str(repository_root),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self._processes[spec.task_id] = process
        deadline = time.monotonic() + codex.startup_timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"Codex daemon exited during startup with code "
                    f"{process.returncode}; inspect {log_path}"
                )
            if spec.socket_path().exists():
                try:
                    self.status(spec)
                    return
                except (ConnectionError, OSError, RuntimeError) as exc:
                    last_error = exc
            time.sleep(0.05)
        process.terminate()
        process.wait(timeout=3)
        raise TimeoutError(
            f"Codex daemon did not become ready within "
            f"{codex.startup_timeout_seconds}s"
            + (f": {last_error}" if last_error else "")
        )

    @staticmethod
    def _sanitized_environment() -> dict[str, str]:
        """Pass location/runtime settings but no inherited credentials."""

        allowed = {
            "HOME",
            "USER",
            "LOGNAME",
            "PATH",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in allowed
        }
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        environment["ROCR_VISIBLE_DEVICES"] = ""
        environment["HIP_VISIBLE_DEVICES"] = ""
        environment["CUDA_VISIBLE_DEVICES"] = ""
        return environment

    def _is_ready(self, spec: CodexTaskSpec) -> bool:
        if not spec.socket_path().exists():
            return False
        try:
            self.status(spec)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    def _prepare_restart(self, spec: CodexTaskSpec) -> None:
        managed = self._processes.pop(spec.task_id, None)
        if managed is not None and managed.poll() is None:
            managed.terminate()
            managed.wait(timeout=3)

        metadata_path = spec.metadata_path()
        if not metadata_path.exists():
            spec.socket_path().unlink(missing_ok=True)
            return
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            pid = int(metadata.get("pid") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            spec.socket_path().unlink(missing_ok=True)
            return
        if pid <= 0:
            spec.socket_path().unlink(missing_ok=True)
            return
        command_path = Path(f"/proc/{pid}/cmdline")
        try:
            command = command_path.read_bytes().replace(b"\x00", b" ")
        except OSError:
            spec.socket_path().unlink(missing_ok=True)
            return
        expected_spec = str(spec.daemon_spec_path()).encode("utf-8")
        if (
            b"project_hermes.runtime.codex_daemon" in command
            and expected_spec in command
        ):
            raise RuntimeError(
                "the task Codex process is alive but its socket is "
                "unhealthy; terminate or reconcile it before restart"
            )
        spec.socket_path().unlink(missing_ok=True)

    @staticmethod
    @contextmanager
    def _task_lock(spec: CodexTaskSpec) -> Iterator[None]:
        try:
            import fcntl
        except ImportError as exc:
            raise RuntimeError(
                "task-private Codex locking requires a POSIX runtime"
            ) from exc
        path = spec.task_directory() / "provision.lock"
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _load_existing_spec(expected: CodexTaskSpec) -> CodexTaskSpec:
        path = expected.task_directory() / "daemon-spec.json"
        with path.open("r", encoding="utf-8") as stream:
            existing = CodexTaskSpec.model_validate(json.load(stream))
        if existing.task_id != expected.task_id:
            raise RuntimeError("existing daemon belongs to a different task")
        if existing.session_id != expected.session_id:
            raise RuntimeError(
                "task already has a different root Codex session"
            )
        if existing.worktree_path.resolve() != expected.worktree_path.resolve():
            raise RuntimeError(
                "one task cannot be attached to multiple worktrees"
            )
        immutable_fields = (
            "sdk_version",
            "cli_version",
            "model_profile",
            "model",
            "model_provider",
            "provider_endpoint",
            "provider_api_key_env",
            "provider_wire_api",
            "reasoning_mode",
            "reasoning_effort",
            "runtime_generation",
            "credentials_file",
            "network_access",
            "turn_timeout_seconds",
        )
        changed = [
            field
            for field in immutable_fields
            if getattr(existing, field) != getattr(expected, field)
        ]
        if changed:
            raise RuntimeError(
                "running Codex task configuration changed; stop and "
                "reprovision the daemon: "
                + ", ".join(changed)
            )
        return existing

    @staticmethod
    def _resolve_model_route(
        codex: CodexTaskRuntimeConfig,
        request: RuntimeRequest,
    ) -> RuntimeModelRoute:
        if request.model_route is None:
            selected = codex.route()
            route = RuntimeModelRoute.model_validate(selected.model_dump())
        else:
            route = request.model_route
            approved = next(
                (
                    profile
                    for profile in codex.model_profiles.values()
                    if profile.profile == route.profile
                ),
                None,
            )
            if approved is None:
                raise ValueError(
                    f"unapproved Codex model profile: {route.profile}"
                )
            if RuntimeModelRoute.model_validate(
                approved.model_dump()
            ) != route:
                raise ValueError(
                    "runtime model route differs from configured profile"
                )
        if request.model is not None and request.model != route.model:
            raise ValueError(
                "runtime model override differs from the selected profile"
            )
        return route


class CodexTaskRuntimeAdapter:
    """AgentRuntime implementation backed by task-private daemons."""

    name = "codex-task"

    def __init__(self, supervisor: TaskCodexSupervisor) -> None:
        self.supervisor = supervisor
        self._specs: dict[str, CodexTaskSpec] = {}
        self._handles: dict[str, RuntimeHandle] = {}
        self._lock = threading.RLock()

    def start(self, request: RuntimeRequest) -> RuntimeHandle:
        spec = self.supervisor.provision(request)
        with self._lock:
            if spec.session_id in self._handles:
                raise ValueError(f"Codex session already exists: {spec.session_id}")
            handle = RuntimeHandle(
                runtime_name=self.name,
                session_id=spec.session_id,
                task_id=spec.task_id,
                status=RuntimeStatus.STARTING,
                model_route=_route_from_spec(spec),
                runtime_generation=spec.runtime_generation,
                worktree_path=str(spec.worktree_path.resolve()),
                transport_ref=str(spec.socket_path()),
            )
            self._specs[spec.session_id] = spec
            self._handles[spec.session_id] = handle
        return self._run_turn(handle, request.prompt)

    def resume(
        self, handle: RuntimeHandle, request: RuntimeRequest
    ) -> RuntimeHandle:
        self._validate_handle(handle)
        if request.task_id != handle.task_id:
            raise ValueError("resume request belongs to a different task")
        self._validate_model_identity(handle, request)
        return self._run_turn(handle, request.prompt)

    def reconnect(
        self,
        handle: RuntimeHandle,
        request: RuntimeRequest,
    ) -> RuntimeHandle:
        if handle.runtime_name != self.name:
            raise ValueError("runtime handle belongs to a different adapter")
        if request.task_id != handle.task_id:
            raise ValueError("reconnect request belongs to a different task")
        if request.session_id not in {None, handle.session_id}:
            raise ValueError("reconnect request names another session")
        reconnect_request = request.model_copy(
            update={
                "session_id": handle.session_id,
                "model_route": request.model_route or handle.model_route,
                "runtime_generation": handle.runtime_generation,
            }
        )
        spec = self.supervisor.provision(reconnect_request)
        status = self.supervisor.status(spec)
        updated = handle.model_copy(
            update={
                "status": _runtime_status(status.get("status")),
                "native_thread_id": status.get("root_thread_id"),
                "transport_ref": str(spec.socket_path()),
                "updated_at": utc_now(),
            }
        )
        with self._lock:
            self._specs[handle.session_id] = spec
            self._handles[handle.session_id] = updated
        return updated

    def cancel(self, handle: RuntimeHandle) -> RuntimeHandle:
        spec = self._spec(handle)
        self.supervisor.cancel(spec)
        updated = handle.model_copy(
            update={"status": RuntimeStatus.CANCELLED, "updated_at": utc_now()}
        )
        self._handles[handle.session_id] = updated
        return updated

    def events(
        self, handle: RuntimeHandle, *, after_sequence: int = 0
    ) -> Iterable[RuntimeEvent]:
        spec = self._spec(handle)
        return tuple(
            RuntimeEvent(
                event_id=(
                    f"{handle.session_id}:{int(event.get('sequence') or 0)}"
                ),
                session_id=handle.session_id,
                sequence=int(event.get("sequence") or 0),
                event_type=str(event.get("event_type") or "unknown"),
                payload=dict(event.get("payload") or {}),
                created_at=event.get("created_at") or utc_now(),
            )
            for event in self.supervisor.events(
                spec,
                after_sequence=after_sequence,
            )
        )

    def result(self, handle: RuntimeHandle) -> RuntimeResult | None:
        spec = self._spec(handle)
        payload = self.supervisor.result(spec)
        if not payload:
            return None
        error = payload.get("error")
        status_text = str(payload.get("status") or "").upper()
        if status_text == "CANCELLED":
            status = RuntimeStatus.CANCELLED
        elif error or status_text in {"FAILED", "ERROR"}:
            status = RuntimeStatus.FAILED
        else:
            status = RuntimeStatus.COMPLETED
        return RuntimeResult(
            session_id=handle.session_id,
            status=status,
            final_response=payload.get("final_response"),
            native_thread_id=self.supervisor.status(spec).get("root_thread_id"),
            native_turn_id=payload.get("turn_id"),
            model_route=_route_from_spec(spec),
            runtime_generation=spec.runtime_generation,
            output=payload,
            error=redact_text(str(error)) if error else None,
            started_at=payload.get("started_at"),
            completed_at=payload.get("completed_at") or utc_now(),
            duration_ms=max(0, int(payload.get("duration_ms") or 0)),
        )

    def close(self, handle: RuntimeHandle) -> RuntimeHandle:
        spec = self._spec(handle)
        self.supervisor.stop(spec)
        updated = handle.model_copy(
            update={"status": RuntimeStatus.CLOSED, "updated_at": utc_now()}
        )
        self._handles[handle.session_id] = updated
        return updated

    def _run_turn(
        self,
        handle: RuntimeHandle,
        prompt: str,
    ) -> RuntimeHandle:
        spec = self._spec(handle)
        running = handle.model_copy(
            update={"status": RuntimeStatus.RUNNING, "updated_at": utc_now()}
        )
        self._handles[handle.session_id] = running
        try:
            self.supervisor.run_turn(spec, prompt)
            status = self.supervisor.status(spec)
        except Exception:
            failed = running.model_copy(
                update={"status": RuntimeStatus.FAILED, "updated_at": utc_now()}
            )
            self._handles[handle.session_id] = failed
            return failed

        completed = running.model_copy(
            update={
                "status": RuntimeStatus.IDLE,
                "native_thread_id": status.get("root_thread_id"),
                "updated_at": utc_now(),
            }
        )
        self._handles[handle.session_id] = completed
        return completed

    def _spec(self, handle: RuntimeHandle) -> CodexTaskSpec:
        self._validate_handle(handle)
        return self._specs[handle.session_id]

    def _validate_handle(self, handle: RuntimeHandle) -> None:
        if handle.runtime_name != self.name:
            raise ValueError("runtime handle belongs to a different adapter")
        if handle.session_id not in self._specs:
            raise KeyError(f"unknown Codex session: {handle.session_id}")

    @staticmethod
    def _validate_model_identity(
        handle: RuntimeHandle,
        request: RuntimeRequest,
    ) -> None:
        if (
            request.model_route is not None
            and request.model_route != handle.model_route
        ):
            raise ValueError(
                "cannot change model profile on a live Codex daemon"
            )
        if request.runtime_generation != handle.runtime_generation:
            raise ValueError(
                "cannot change runtime generation on a live Codex daemon"
            )


def _route_from_spec(spec: CodexTaskSpec) -> RuntimeModelRoute:
    if not all(
        (
            spec.model_profile,
            spec.model,
            spec.model_provider,
            spec.provider_endpoint,
            spec.provider_api_key_env,
        )
    ):
        raise ValueError("Codex task spec does not contain an auditable model route")
    return RuntimeModelRoute(
        profile=spec.model_profile,
        model=spec.model,
        model_provider=spec.model_provider,
        provider_endpoint=spec.provider_endpoint,
        provider_api_key_env=spec.provider_api_key_env,
        provider_wire_api=spec.provider_wire_api,
        reasoning_mode=spec.reasoning_mode,
        reasoning_effort=spec.reasoning_effort,
        context_window=spec.context_window,
    )


def _runtime_status(value: Any) -> RuntimeStatus:
    normalized = str(value or "").upper()
    return {
        "STARTING": RuntimeStatus.STARTING,
        "IDLE": RuntimeStatus.IDLE,
        "RUNNING": RuntimeStatus.RUNNING,
        "FAILED": RuntimeStatus.FAILED,
        "CANCELLED": RuntimeStatus.CANCELLED,
        "CLOSED": RuntimeStatus.CLOSED,
    }.get(normalized, RuntimeStatus.FAILED)
