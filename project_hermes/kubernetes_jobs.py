"""Restricted Kubernetes Job execution backend."""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import time
from datetime import datetime
from http.client import HTTPResponse
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from pydantic import Field, field_validator, model_validator

from project_hermes.config import KubernetesJobConfig
from project_hermes.execution import (
    BackendExecution,
    DefinitiveExecutionStartError,
    ExecutionObservation,
    ExecutionRequest,
    ExecutionStatus,
)
from project_hermes.models import StrictModel
from project_hermes.release import parse_runtime_image, verify_release_root
from project_hermes.resources import ArtifactManifest, ExecutionEnvironment
from project_hermes.supply_chain import (
    TaskSourceBundleMetadata,
    WorkerArtifactArchive,
    verify_task_source_bundle,
    verify_worker_artifact_archive,
)

_SERVICE_ACCOUNT_ROOT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_SAFE_NAME_RE = re.compile(r"[^a-z0-9-]+")
_REGISTRY_RE = re.compile(
    r"^(?:localhost|[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)"
    r"(?::[1-9][0-9]{0,4})?$"
)
_IMAGE_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*$")
_RELEASE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARCHIVE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CODEX_WORKER_METADATA_KEY = "codex_worker"
_MODEL_ENDPOINT = "https://api.deepseek.com/v1"
_APPROVED_CODEX_MODEL_ROUTES = frozenset({
    (
        "deepseek",
        "https://api.deepseek.com/v1",
        "DEEPSEEK_API_KEY",
        "responses",
    ),
    (
        "digitalocean",
        "https://inference.do-ai.run/v1",
        "MODEL_ACCESS_KEY",
        "responses",
    ),
})
_APPROVED_HANDOFF_MODEL_ROUTES = frozenset({
    (
        "deepseek",
        "https://api.deepseek.com/v1",
        "DEEPSEEK_API_KEY",
        "chat",
    ),
    (
        "digitalocean",
        "https://inference.do-ai.run/v1",
        "MODEL_ACCESS_KEY",
        "chat",
    ),
})
_MODEL_SECRET_KEYS_BY_ENV = {
    "DEEPSEEK_API_KEY": "deepseek-api-key",
    "MODEL_ACCESS_KEY": "model-access-key",
}
_SKILL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_PROVIDER_CAPACITY_RE = re.compile(
    r"(?:\b429\b.*too many requests|too many requests.*\b429\b)",
    re.IGNORECASE,
)
_CAPACITY_RESULT_EVENT = "project_hermes.capacity_result_completed"
_FAILURE_CLASSIFICATION_TAIL_BYTES = 2 * 1024 * 1024


class CodexWorkerMetadata(StrictModel):
    """Credential-free, task-bound metadata for one isolated Codex Job."""

    schema_version: Literal["codex-kubernetes-worker.v1"] = "codex-kubernetes-worker.v1"
    release_digest: str
    source_bundle_request_id: str
    source_bundle_digest: str
    base_sha: str
    model_profile: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    model_provider: Literal["deepseek", "digitalocean"] = "deepseek"
    provider_endpoint: str = _MODEL_ENDPOINT
    provider_api_key_env: Literal[
        "DEEPSEEK_API_KEY",
        "MODEL_ACCESS_KEY",
    ] = "DEEPSEEK_API_KEY"
    provider_wire_api: Literal["chat", "responses"] = "responses"
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    context_window: int | None = Field(default=None, ge=1, le=10_000_000)
    handoff_model_profile: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
    )
    handoff_model: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    handoff_model_provider: Literal["deepseek", "digitalocean"] | None = None
    handoff_provider_endpoint: str | None = None
    handoff_provider_api_key_env: Literal[
        "DEEPSEEK_API_KEY",
        "MODEL_ACCESS_KEY",
    ] | None = None
    handoff_provider_wire_api: Literal["chat"] | None = None
    repository_skill_name: str | None = None
    repository_skill_digest: str | None = None

    @field_validator("release_digest")
    @classmethod
    def validate_release_digest(cls, value: str) -> str:
        if not _RELEASE_DIGEST_RE.fullmatch(value):
            raise ValueError("release_digest must be lowercase SHA-256 hex")
        return value

    @field_validator("source_bundle_digest")
    @classmethod
    def validate_source_digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("source_bundle_digest must be lowercase SHA-256")
        return value

    @field_validator("base_sha")
    @classmethod
    def validate_base_sha(cls, value: str) -> str:
        if not _GIT_OBJECT_RE.fullmatch(value):
            raise ValueError("base_sha must be a full Git object id")
        return value

    @field_validator("repository_skill_name")
    @classmethod
    def validate_repository_skill_name(cls, value: str | None) -> str | None:
        if value is not None and not _SKILL_NAME_RE.fullmatch(value):
            raise ValueError("repository_skill_name must use lowercase skill-name form")
        return value

    @field_validator("repository_skill_digest")
    @classmethod
    def validate_repository_skill_digest(
        cls,
        value: str | None,
    ) -> str | None:
        if value is not None and not _RELEASE_DIGEST_RE.fullmatch(value):
            raise ValueError("repository_skill_digest must be lowercase SHA-256 hex")
        return value

    @model_validator(mode="after")
    def endpoint_is_sanitized(self) -> "CodexWorkerMetadata":
        parsed = urlsplit(self.provider_endpoint)
        if (
            parsed.scheme != "https"
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (
                self.model_provider,
                self.provider_endpoint,
                self.provider_api_key_env,
                self.provider_wire_api,
            )
            not in _APPROVED_CODEX_MODEL_ROUTES
        ):
            raise ValueError("Codex worker model route is not allowlisted")
        if (self.repository_skill_name is None) != (
            self.repository_skill_digest is None
        ):
            raise ValueError(
                "repository Skill name and digest must be supplied together"
            )
        handoff_route = (
            self.handoff_model_profile,
            self.handoff_model,
            self.handoff_model_provider,
            self.handoff_provider_endpoint,
            self.handoff_provider_api_key_env,
            self.handoff_provider_wire_api,
        )
        if any(value is not None for value in handoff_route) and not all(
            value is not None for value in handoff_route
        ):
            raise ValueError("Hermes handoff model route must be supplied together")
        if all(value is not None for value in handoff_route):
            parsed_handoff = urlsplit(str(self.handoff_provider_endpoint))
            if (
                parsed_handoff.scheme != "https"
                or parsed_handoff.port not in {None, 443}
                or parsed_handoff.username
                or parsed_handoff.password
                or parsed_handoff.query
                or parsed_handoff.fragment
                or (
                    self.handoff_model_provider,
                    self.handoff_provider_endpoint,
                    self.handoff_provider_api_key_env,
                    self.handoff_provider_wire_api,
                )
                not in _APPROVED_HANDOFF_MODEL_ROUTES
            ):
                raise ValueError("Hermes handoff model route is not allowlisted")
        return self


class KubernetesApi(Protocol):
    """Minimal API surface required by the Job backend."""

    def create_job(
        self,
        namespace: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Create one Job."""

    def get_job(
        self,
        namespace: str,
        name: str,
    ) -> dict[str, Any] | None:
        """Return a Job, or ``None`` after deletion."""

    def list_job_pods(
        self,
        namespace: str,
        name: str,
    ) -> list[dict[str, Any]]:
        """List Pods owned by a Job."""

    def delete_job(self, namespace: str, name: str) -> bool:
        """Delete a Job with foreground propagation."""


class CodexWorkerKubernetesApi(KubernetesApi, Protocol):
    """Kubernetes API operations needed by isolated Codex workers."""

    def create_network_policy(
        self,
        namespace: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Create one worker-scoped NetworkPolicy."""

    def get_network_policy(
        self,
        namespace: str,
        name: str,
    ) -> dict[str, Any] | None:
        """Return a NetworkPolicy, or ``None`` after deletion."""

    def delete_network_policy(self, namespace: str, name: str) -> bool:
        """Delete one worker-scoped NetworkPolicy."""


class KubernetesApiError(RuntimeError):
    """Structured Kubernetes HTTP error without credential material."""

    def __init__(
        self,
        status_code: int,
        method: str,
        path: str,
        detail: str,
    ) -> None:
        self.status_code = status_code
        self.method = method
        self.path = path
        super().__init__(
            f"Kubernetes API {method} {path} failed with HTTP {status_code}: {detail}"
        )


class InClusterKubernetesApi:
    """Small dependency-free Kubernetes API client using the projected token."""

    def __init__(
        self,
        *,
        api_server: str | None = None,
        token_path: Path = _SERVICE_ACCOUNT_ROOT / "token",
        ca_path: Path = _SERVICE_ACCOUNT_ROOT / "ca.crt",
        request_timeout_seconds: int = 30,
    ) -> None:
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        self.api_server = api_server or (f"https://{host}:{port}" if host else "")
        if not self.api_server:
            raise RuntimeError(
                "KUBERNETES_SERVICE_HOST is unavailable; "
                "the Job backend must run in a Kubernetes Pod"
            )
        if not token_path.is_file() or not ca_path.is_file():
            raise RuntimeError(
                "the projected Kubernetes service account token is unavailable"
            )
        self.token_path = token_path
        self.ssl_context = ssl.create_default_context(cafile=str(ca_path))
        self.request_timeout_seconds = request_timeout_seconds

    def create_job(
        self,
        namespace: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        payload = self._request(
            "POST",
            f"/apis/batch/v1/namespaces/{quote(namespace)}/jobs",
            body=manifest,
        )
        assert payload is not None
        return payload

    def get_job(
        self,
        namespace: str,
        name: str,
    ) -> dict[str, Any] | None:
        return self._request(
            "GET",
            (f"/apis/batch/v1/namespaces/{quote(namespace)}/jobs/{quote(name)}"),
            allow_not_found=True,
        )

    def list_job_pods(
        self,
        namespace: str,
        name: str,
    ) -> list[dict[str, Any]]:
        query = urlencode({"labelSelector": f"job-name={name}"})
        payload = self._request(
            "GET",
            f"/api/v1/namespaces/{quote(namespace)}/pods?{query}",
        )
        assert payload is not None
        items = payload.get("items", [])
        return items if isinstance(items, list) else []

    def delete_job(self, namespace: str, name: str) -> bool:
        payload = self._request(
            "DELETE",
            (f"/apis/batch/v1/namespaces/{quote(namespace)}/jobs/{quote(name)}"),
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "propagationPolicy": "Foreground",
            },
            allow_not_found=True,
        )
        return payload is not None

    def create_network_policy(
        self,
        namespace: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        payload = self._request(
            "POST",
            (
                "/apis/networking.k8s.io/v1/namespaces/"
                f"{quote(namespace)}/networkpolicies"
            ),
            body=manifest,
        )
        assert payload is not None
        return payload

    def get_network_policy(
        self,
        namespace: str,
        name: str,
    ) -> dict[str, Any] | None:
        return self._request(
            "GET",
            (
                "/apis/networking.k8s.io/v1/namespaces/"
                f"{quote(namespace)}/networkpolicies/{quote(name)}"
            ),
            allow_not_found=True,
        )

    def delete_network_policy(self, namespace: str, name: str) -> bool:
        payload = self._request(
            "DELETE",
            (
                "/apis/networking.k8s.io/v1/namespaces/"
                f"{quote(namespace)}/networkpolicies/{quote(name)}"
            ),
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "propagationPolicy": "Foreground",
            },
            allow_not_found=True,
        )
        return payload is not None

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        token = self.token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError("the projected service account token is empty")
        encoded = (
            json.dumps(body, separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        )
        request = Request(
            self.api_server + path,
            data=encoded,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            response: HTTPResponse
            with urlopen(
                request,
                context=self.ssl_context,
                timeout=self.request_timeout_seconds,
            ) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            if allow_not_found and exc.code == 404:
                return None
            detail = _api_error_detail(raw)
            raise KubernetesApiError(
                exc.code,
                method,
                path,
                detail,
            ) from exc
        if not raw:
            return {}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("Kubernetes API returned a non-object payload")
        return payload


class KubernetesJobBackend:
    """Create digest-pinned, same-node, resource-bounded Kubernetes Jobs."""

    name = "kubernetes-job"

    def __init__(
        self,
        config: KubernetesJobConfig,
        *,
        api: KubernetesApi | None = None,
    ) -> None:
        if not config.enabled:
            raise ValueError("the Kubernetes Job backend is disabled")
        self.config = config
        self.node_name = config.node_name or os.environ.get("PROJECT_HERMES_NODE_NAME")
        if not self.node_name:
            raise ValueError(
                "kubernetes_jobs.node_name or PROJECT_HERMES_NODE_NAME is required"
            )
        self.api = api or InClusterKubernetesApi()

    def start(
        self,
        execution_id: str,
        request: ExecutionRequest,
        *,
        gpu_ids: tuple[str, ...],
        artifacts: tuple[ArtifactManifest, ...],
    ) -> BackendExecution:
        if request.environment not in {
            ExecutionEnvironment.KUBERNETES,
            ExecutionEnvironment.GPU_CLUSTER,
        }:
            raise ValueError(
                "Kubernetes Jobs require a kubernetes or gpu_cluster "
                "execution environment"
            )
        if artifacts:
            raise ValueError(
                "local artifacts are not mountable by the Kubernetes Job backend"
            )
        repository = _normalize_image_repository(request.image_repository)
        if not _repository_is_allowed(
            repository,
            self.config.allowed_image_prefixes,
        ):
            raise PermissionError(
                f"container image repository is not allowed: {repository}"
            )
        gpu_count = request.gpu.count if request.gpu is not None else 0
        if len(gpu_ids) != gpu_count:
            raise ValueError("controller GPU lease size does not match the Job request")

        job_name = _job_name(execution_id, request.task_id)
        manifest = self._manifest(
            job_name,
            execution_id,
            request,
            repository=repository,
            gpu_count=gpu_count,
        )
        try:
            created = self.api.create_job(self.config.namespace, manifest)
        except KubernetesApiError as exc:
            if exc.status_code != 409:
                raise
            created = self.api.get_job(self.config.namespace, job_name)
            if created is None:
                raise
            annotations = created.get("metadata", {}).get("annotations", {})
            if annotations.get("project-hermes.io/execution-id") != execution_id:
                raise RuntimeError(
                    "an unrelated Kubernetes Job already uses the "
                    "deterministic execution name"
                ) from exc
        metadata = created.get("metadata", {})
        native_name = str(metadata.get("name") or job_name)
        return BackendExecution(
            execution_id=execution_id,
            backend=self.name,
            native_id=f"{self.config.namespace}/{native_name}",
        )

    def observe(
        self,
        execution: BackendExecution,
    ) -> ExecutionObservation:
        namespace, name = _native_identity(execution.native_id)
        job = self.api.get_job(namespace, name)
        if job is None:
            return ExecutionObservation(
                execution_id=execution.execution_id,
                status=ExecutionStatus.CANCELLED,
                terminated=True,
                result={"reason": "job-not-found"},
                log_refs=[f"kubernetes://{namespace}/jobs/{name}/logs"],
                environment={
                    "backend": self.name,
                    "namespace": namespace,
                    "job": name,
                    "node": self.node_name,
                },
            )

        status = job.get("status", {})
        conditions = {
            str(condition.get("type")): condition
            for condition in status.get("conditions", [])
            if condition.get("status") == "True"
        }
        if "Complete" in conditions:
            execution_status = ExecutionStatus.SUCCEEDED
            terminated = True
        elif "Failed" in conditions:
            execution_status = ExecutionStatus.FAILED
            terminated = True
        elif job.get("metadata", {}).get("deletionTimestamp"):
            execution_status = ExecutionStatus.TERMINATING
            terminated = False
        else:
            execution_status = ExecutionStatus.RUNNING
            terminated = False

        pods = self.api.list_job_pods(namespace, name)
        exit_code = _last_exit_code(pods)
        terminal_condition = conditions.get("Complete") or conditions.get("Failed")
        started_at = _parse_kubernetes_timestamp(
            status.get("startTime") or job.get("metadata", {}).get("creationTimestamp")
        )
        completed_at = _parse_kubernetes_timestamp(
            status.get("completionTime")
            or (
                terminal_condition.get("lastTransitionTime")
                if terminal_condition
                else None
            )
        )
        duration_ms = (
            max(
                0,
                int((completed_at - started_at).total_seconds() * 1000),
            )
            if started_at is not None and completed_at is not None
            else None
        )
        result = {
            "active": int(status.get("active") or 0),
            "succeeded": int(status.get("succeeded") or 0),
            "failed": int(status.get("failed") or 0),
        }
        if terminal_condition:
            result["reason"] = str(terminal_condition.get("reason") or "")
            result["message"] = str(terminal_condition.get("message") or "")
        return ExecutionObservation(
            execution_id=execution.execution_id,
            status=execution_status,
            terminated=terminated,
            exit_code=exit_code,
            result=result,
            log_refs=[f"kubernetes://{namespace}/jobs/{name}/logs"],
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            environment={
                "backend": self.name,
                "namespace": namespace,
                "job": name,
                "node": self.node_name,
            },
        )

    def cancel(self, execution: BackendExecution) -> None:
        namespace, name = _native_identity(execution.native_id)
        self.api.delete_job(namespace, name)
        deadline = time.monotonic() + self.config.delete_timeout_seconds
        while time.monotonic() < deadline:
            if self.api.get_job(namespace, name) is None:
                return
            time.sleep(0.25)
        raise TimeoutError(
            f"Kubernetes Job deletion did not finish: {namespace}/{name}"
        )

    def _manifest(
        self,
        job_name: str,
        execution_id: str,
        request: ExecutionRequest,
        *,
        repository: str,
        gpu_count: int,
    ) -> dict[str, Any]:
        timeout_seconds = min(
            request.timeout_seconds,
            self.config.max_timeout_seconds,
        )
        requests: dict[str, str] = {
            "cpu": self.config.cpu_request,
            "memory": self.config.memory_request,
        }
        limits: dict[str, str] = {
            "cpu": self.config.cpu_limit,
            "memory": self.config.memory_limit,
        }
        if gpu_count:
            count = str(gpu_count)
            requests[self.config.gpu_resource_name] = count
            limits[self.config.gpu_resource_name] = count

        node_selector = {"kubernetes.io/hostname": self.node_name}
        if request.gpu is not None and request.gpu.architecture:
            node_selector[self.config.gpu_architecture_label] = request.gpu.architecture
        network_value = "allowed" if request.network_access else "denied"
        labels = {
            "app.kubernetes.io/name": "project-hermes-job",
            "app.kubernetes.io/managed-by": "project-hermes",
            "project-hermes.io/execution": hashlib.sha256(
                execution_id.encode("utf-8")
            ).hexdigest()[:16],
            "project-hermes.io/network-access": network_value,
        }
        pod_spec: dict[str, Any] = {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "restartPolicy": "Never",
            "serviceAccountName": self.config.job_service_account_name,
            "nodeSelector": node_selector,
            "terminationGracePeriodSeconds": 30,
            "securityContext": {
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "task",
                    "image": f"{repository}@{request.image_digest}",
                    "imagePullPolicy": "IfNotPresent",
                    "command": request.command,
                    "env": [
                        {
                            "name": "PROJECT_HERMES_EXECUTION_ID",
                            "value": execution_id,
                        },
                        {
                            "name": "PROJECT_HERMES_TASK_ID",
                            "value": request.task_id,
                        },
                        {
                            "name": "HERMES_KANBAN_TASK",
                            "value": request.task_id,
                        },
                        *(
                            [
                                {
                                    "name": "HERMES_KANBAN_RUN_ID",
                                    "value": request.run_id,
                                }
                            ]
                            if request.run_id
                            else []
                        ),
                    ],
                    "resources": {
                        "requests": requests,
                        "limits": limits,
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "privileged": False,
                    },
                }
            ],
        }
        if self.config.image_pull_secrets:
            pod_spec["imagePullSecrets"] = [
                {"name": name} for name in self.config.image_pull_secrets
            ]
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": self.config.namespace,
                "labels": labels,
                "annotations": {
                    "project-hermes.io/execution-id": execution_id,
                    "project-hermes.io/request-id": request.request_id,
                    "project-hermes.io/image-digest": request.image_digest,
                },
            },
            "spec": {
                "activeDeadlineSeconds": timeout_seconds,
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": (self.config.ttl_seconds_after_finished),
                "template": {
                    "metadata": {"labels": labels},
                    "spec": pod_spec,
                },
            },
        }


class CodexKubernetesWorkerBackend:
    """Run one task bundle through Codex with proxy-only model egress."""

    name = "kubernetes-codex-worker"

    def __init__(
        self,
        config: KubernetesJobConfig,
        *,
        release_digest: str,
        api: CodexWorkerKubernetesApi | None = None,
    ) -> None:
        if not config.enabled:
            raise ValueError("the Kubernetes Job backend is disabled")
        if not _RELEASE_DIGEST_RE.fullmatch(release_digest):
            raise ValueError("release_digest must be lowercase SHA-256 hex")
        self.config = config
        self.release_digest = release_digest
        self.node_name = config.node_name or os.environ.get("PROJECT_HERMES_NODE_NAME")
        if not self.node_name:
            raise ValueError(
                "kubernetes_jobs.node_name or PROJECT_HERMES_NODE_NAME is required"
            )
        self.release_manifest = verify_release_root(
            config.release_root,
            release_digest,
        )
        self.api = api or InClusterKubernetesApi()

    def start(
        self,
        execution_id: str,
        request: ExecutionRequest,
        *,
        gpu_ids: tuple[str, ...],
        artifacts: tuple[ArtifactManifest, ...],
    ) -> BackendExecution:
        if request.environment not in {
            ExecutionEnvironment.KUBERNETES,
            ExecutionEnvironment.GPU_CLUSTER,
        }:
            raise ValueError("Codex workers require a Kubernetes environment")
        if request.network_access:
            raise ValueError(
                "Codex workers must use proxy-only egress, not network_access"
            )
        worker = self._worker_metadata(request)
        if worker.release_digest != self.release_digest:
            raise ValueError("worker release digest differs from controller")
        expected_secret_key = _MODEL_SECRET_KEYS_BY_ENV[worker.provider_api_key_env]
        if self.config.model_secret_key != expected_secret_key:
            raise ValueError("worker Secret key does not match provider_api_key_env")
        handoff_route = (
            worker.handoff_model_profile,
            worker.handoff_model,
            worker.handoff_model_provider,
            worker.handoff_provider_endpoint,
            worker.handoff_provider_api_key_env,
            worker.handoff_provider_wire_api,
        )
        if any(value is None for value in handoff_route):
            raise ValueError("Codex worker requires a complete Hermes handoff route")
        repository = _normalize_image_repository(request.image_repository)
        if not _repository_is_allowed(
            repository,
            self.config.allowed_image_prefixes,
        ):
            raise PermissionError(
                f"container image repository is not allowed: {repository}"
            )
        release_repository, release_image_digest = parse_runtime_image(
            str(self.release_manifest.get("runtime_image") or "")
        )
        if (
            release_repository != repository
            or release_image_digest != request.image_digest
        ):
            raise ValueError(
                "worker image does not match the exact release runtime image"
            )
        gpu_count = request.gpu.count if request.gpu is not None else 0
        if len(gpu_ids) != gpu_count or gpu_count > self.config.max_total_gpus:
            raise ValueError("worker GPU allocation exceeds the locked limit")
        if request.artifact_request_ids != [worker.source_bundle_request_id]:
            raise ValueError("Codex worker must request exactly its task source bundle")
        if len(artifacts) != 1:
            raise ValueError("Codex worker requires exactly one verified source bundle")
        source_bundle = artifacts[0]
        if (
            source_bundle.request_id != worker.source_bundle_request_id
            or source_bundle.resolved_digest != worker.source_bundle_digest
        ):
            raise ValueError("source bundle identity differs from worker spec")
        source_metadata = TaskSourceBundleMetadata(
            task_id=request.task_id,
            repository=request.repository,
            workspace_lease_id=request.workspace_lease_id,
            base_sha=worker.base_sha,
            candidate_digest=request.candidate_digest,
        )
        verify_task_source_bundle(
            self.config.source_bundle_root,
            source_bundle,
            expected=source_metadata,
        )

        job_name = _job_name(execution_id, request.task_id)
        policy = self.network_policy_manifest(job_name, execution_id)
        self._create_network_policy(policy, execution_id)
        try:
            archive_scope = self._prepare_archive_scope(execution_id)
            manifest = self.job_manifest(
                job_name,
                execution_id,
                request,
                worker=worker,
                source_bundle=source_bundle,
                repository=repository,
                gpu_count=gpu_count,
                archive_scope=archive_scope,
            )
            created = self._create_or_recover_job(
                job_name,
                execution_id,
                manifest,
            )
        except Exception as exc:
            try:
                reclaimed = self._reclaim_policy_after_failed_start(
                    job_name,
                    execution_id,
                )
            except Exception:
                raise RuntimeError(
                    "Codex worker admission is uncertain and policy cleanup "
                    "could not be confirmed"
                ) from exc
            if not reclaimed:
                raise RuntimeError(
                    "Codex worker admission is uncertain because a matching Job exists"
                ) from exc
            raise DefinitiveExecutionStartError(
                f"Codex worker admission failed: {exc}"
            ) from exc
        native_name = str(created.get("metadata", {}).get("name") or job_name)
        return BackendExecution(
            execution_id=execution_id,
            backend=self.name,
            native_id=f"{self.config.namespace}/{native_name}",
        )

    def observe(
        self,
        execution: BackendExecution,
    ) -> ExecutionObservation:
        namespace, name = _native_identity(execution.native_id)
        job = self.api.get_job(namespace, name)
        if job is None:
            return ExecutionObservation(
                execution_id=execution.execution_id,
                status=ExecutionStatus.CANCELLED,
                terminated=True,
                result={"reason": "job-not-found"},
                log_refs=[f"kubernetes://{namespace}/jobs/{name}/logs"],
                environment={
                    "backend": self.name,
                    "namespace": namespace,
                    "job": name,
                    "node": self.node_name,
                },
            )
        status = job.get("status", {})
        conditions = {
            str(condition.get("type")): condition
            for condition in status.get("conditions", [])
            if condition.get("status") == "True"
        }
        terminal_condition = conditions.get("Complete") or conditions.get("Failed")
        if terminal_condition is None:
            execution_status = (
                ExecutionStatus.TERMINATING
                if job.get("metadata", {}).get("deletionTimestamp")
                else ExecutionStatus.RUNNING
            )
            return ExecutionObservation(
                execution_id=execution.execution_id,
                status=execution_status,
                terminated=False,
                result={
                    "active": int(status.get("active") or 0),
                    "succeeded": int(status.get("succeeded") or 0),
                    "failed": int(status.get("failed") or 0),
                },
                log_refs=[f"kubernetes://{namespace}/jobs/{name}/logs"],
                started_at=_parse_kubernetes_timestamp(
                    status.get("startTime")
                    or job.get("metadata", {}).get("creationTimestamp")
                ),
                environment={
                    "backend": self.name,
                    "namespace": namespace,
                    "job": name,
                    "node": self.node_name,
                },
            )

        pods = self.api.list_job_pods(namespace, name)
        pod_exit_code = _last_exit_code(pods)
        result: dict[str, Any] = {
            "active": int(status.get("active") or 0),
            "succeeded": int(status.get("succeeded") or 0),
            "failed": int(status.get("failed") or 0),
            "reason": str(terminal_condition.get("reason") or ""),
            "message": str(terminal_condition.get("message") or ""),
            "archive_verified": False,
            "job_retained": True,
        }
        archive: WorkerArtifactArchive | None = None
        metadata = job.get("metadata")
        annotations = (
            metadata.get("annotations") if isinstance(metadata, dict) else None
        )
        raw_release_digest = str(
            annotations.get("project-hermes.io/release-digest")
            if isinstance(annotations, dict)
            else ""
        )
        job_release_digest = (
            raw_release_digest
            if _RELEASE_DIGEST_RE.fullmatch(raw_release_digest)
            else None
        )
        try:
            archive = self._verify_archive(job, execution.execution_id)
            if pod_exit_code is not None and pod_exit_code != archive.exit_code:
                raise ValueError("Pod exit code differs from archived worker result")
        except (OSError, ValueError) as exc:
            result["archive_error"] = str(exc)[:500]
            execution_status = ExecutionStatus.FAILED
            exit_code = pod_exit_code
            log_refs = [f"kubernetes://{namespace}/jobs/{name}/logs"]
        else:
            result.update({
                "archive_verified": True,
                "archive_outcome": archive.outcome,
                "verified_archive": archive.model_dump(mode="json"),
                "job_retained": True,
            })
            failure_kind = _worker_failure_kind(
                self.config.artifact_archive_root.resolve()
                / "execution-scopes"
                / execution.execution_id,
                archive,
            )
            if failure_kind is not None:
                result["failure_kind"] = failure_kind
            execution_status = (
                ExecutionStatus.SUCCEEDED
                if "Complete" in conditions and archive.exit_code == 0
                else ExecutionStatus.FAILED
            )
            exit_code = archive.exit_code
            log_refs = [
                f"kubernetes://{namespace}/jobs/{name}/logs",
                f"artifact://worker/{execution.execution_id}",
            ]
        started_at = _parse_kubernetes_timestamp(
            status.get("startTime") or job.get("metadata", {}).get("creationTimestamp")
        )
        completed_at = _parse_kubernetes_timestamp(
            status.get("completionTime") or terminal_condition.get("lastTransitionTime")
        )
        duration_ms = (
            max(
                0,
                int((completed_at - started_at).total_seconds() * 1000),
            )
            if started_at is not None and completed_at is not None
            else None
        )
        environment: dict[str, Any] = {
            "backend": self.name,
            "namespace": namespace,
            "job": name,
            "node": self.node_name,
        }
        observed_release_digest = (
            archive.release_digest if archive is not None else job_release_digest
        )
        if observed_release_digest is not None:
            environment["release_digest"] = observed_release_digest
        return ExecutionObservation(
            execution_id=execution.execution_id,
            status=execution_status,
            terminated=True,
            exit_code=exit_code,
            result=result,
            log_refs=log_refs,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            environment=environment,
        )

    def delete_verified(
        self,
        execution: BackendExecution,
    ) -> WorkerArtifactArchive:
        """Delete a terminal Job only after re-verifying its durable archive."""

        namespace, name = _native_identity(execution.native_id)
        job = self.api.get_job(namespace, name)
        if job is None:
            raise RuntimeError("worker Job is already absent")
        conditions = {
            str(condition.get("type"))
            for condition in job.get("status", {}).get("conditions", [])
            if condition.get("status") == "True"
        }
        if not conditions.intersection({"Complete", "Failed"}):
            raise RuntimeError("active worker Job cannot be deleted")
        archive = self._verify_archive(job, execution.execution_id)
        self.api.delete_network_policy(
            namespace,
            _network_policy_name(name),
        )
        self.api.delete_job(namespace, name)
        deadline = time.monotonic() + self.config.delete_timeout_seconds
        while time.monotonic() < deadline:
            if self.api.get_job(namespace, name) is None:
                return archive
            time.sleep(0.25)
        raise TimeoutError(
            f"Kubernetes Job deletion did not finish: {namespace}/{name}"
        )

    def cancel(self, execution: BackendExecution) -> None:
        """Refuse lossy cancellation; verified terminal cleanup is allowed."""

        self.delete_verified(execution)

    def network_policy_manifest(
        self,
        job_name: str,
        execution_id: str,
    ) -> dict[str, Any]:
        execution_label = _execution_label(execution_id)
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": _network_policy_name(job_name),
                "namespace": self.config.namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "project-hermes",
                    "project-hermes.io/worker-kind": "codex-issue",
                },
                "annotations": {
                    "project-hermes.io/execution-id": execution_id,
                },
            },
            "spec": {
                "podSelector": {
                    "matchLabels": {
                        "project-hermes.io/execution": execution_label,
                        "project-hermes.io/worker-kind": "codex-issue",
                    }
                },
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [
                    {
                        "to": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {
                                        "kubernetes.io/metadata.name": ("kube-system")
                                    }
                                },
                                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                            }
                        ],
                        "ports": [
                            {"protocol": "UDP", "port": 53},
                            {"protocol": "TCP", "port": 53},
                        ],
                    },
                    {
                        "to": [
                            {
                                "podSelector": {
                                    "matchLabels": {
                                        "app.kubernetes.io/name": (
                                            self.config.model_proxy_service_name
                                        )
                                    }
                                }
                            }
                        ],
                        "ports": [
                            {
                                "protocol": "TCP",
                                "port": self.config.model_proxy_port,
                            }
                        ],
                    },
                ],
            },
        }

    def job_manifest(
        self,
        job_name: str,
        execution_id: str,
        request: ExecutionRequest,
        *,
        worker: CodexWorkerMetadata,
        source_bundle: ArtifactManifest,
        repository: str,
        gpu_count: int,
        archive_scope: Path,
    ) -> dict[str, Any]:
        timeout_seconds = min(
            request.timeout_seconds,
            self.config.max_timeout_seconds,
        )
        resource_requests: dict[str, str] = {
            "cpu": self.config.cpu_request,
            "memory": self.config.memory_request,
        }
        resource_limits: dict[str, str] = {
            "cpu": self.config.cpu_limit,
            "memory": self.config.memory_limit,
        }
        if gpu_count:
            count = str(gpu_count)
            resource_requests[self.config.gpu_resource_name] = count
            resource_limits[self.config.gpu_resource_name] = count
        node_selector = {"kubernetes.io/hostname": self.node_name}
        if request.gpu is not None and request.gpu.architecture:
            node_selector[self.config.gpu_architecture_label] = request.gpu.architecture
        labels = {
            "app.kubernetes.io/name": "project-hermes-codex-worker",
            "app.kubernetes.io/managed-by": "project-hermes",
            "project-hermes.io/execution": _execution_label(execution_id),
            "project-hermes.io/worker-kind": "codex-issue",
            "project-hermes.io/network-access": "model-proxy-only",
        }
        image = f"{repository}@{request.image_digest}"
        proxy_url = (
            f"http://{self.config.model_proxy_service_name}."
            f"{self.config.namespace}.svc:{self.config.model_proxy_port}"
        )
        handoff_api_key_env = worker.handoff_provider_api_key_env
        if handoff_api_key_env is None:
            raise ValueError("Codex worker requires a handoff credential route")
        credential_secret_keys = {
            worker.provider_api_key_env: self.config.model_secret_key,
            handoff_api_key_env: _MODEL_SECRET_KEYS_BY_ENV[handoff_api_key_env],
        }
        pvc_volume_names: dict[str, str] = {}
        pvc_volumes: list[dict[str, Any]] = []

        def register_pvc_volume(
            preferred_name: str,
            claim_name: str,
        ) -> str:
            existing_name = pvc_volume_names.get(claim_name)
            if existing_name is not None:
                return existing_name
            pvc_volume_names[claim_name] = preferred_name
            pvc_volumes.append({
                "name": preferred_name,
                "persistentVolumeClaim": {"claimName": claim_name},
            })
            return preferred_name

        release_volume_name = register_pvc_volume(
            "release",
            self.config.release_pvc_name,
        )
        source_bundle_volume_name = register_pvc_volume(
            "source-bundle",
            self.config.source_bundle_pvc_name,
        )
        artifact_archive_volume_name = register_pvc_volume(
            "artifacts",
            self.config.artifact_archive_pvc_name,
        )
        common_mounts = [
            {
                "name": release_volume_name,
                "mountPath": "/release",
                "subPath": (
                    f"{self.config.release_pvc_subpath_prefix}/{self.release_digest}"
                ),
                "readOnly": True,
            },
            {"name": "runtime", "mountPath": "/runtime"},
            {"name": "tmp", "mountPath": "/tmp"},
        ]
        security_context = {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "privileged": False,
            "readOnlyRootFilesystem": True,
        }
        source_subpath = (
            PurePosixPath(self.config.source_bundle_pvc_subpath)
            / source_bundle.local_path
        ).as_posix()
        archive_relative = archive_scope.relative_to(
            self.config.artifact_archive_root.resolve()
        )
        archive_subpath = (
            PurePosixPath(self.config.artifact_archive_pvc_subpath)
            / archive_relative.as_posix()
        ).as_posix()
        pod_spec: dict[str, Any] = {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "restartPolicy": "Never",
            "nodeSelector": node_selector,
            "terminationGracePeriodSeconds": 30,
            "securityContext": {
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "initContainers": [
                {
                    "name": "prepare-runtime",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": [
                        "/bin/bash",
                        "/release/worker/prepare-runtime.sh",
                    ],
                    "env": [
                        {
                            "name": "PROJECT_HERMES_RELEASE_ROOT",
                            "value": "/release",
                        },
                        {
                            "name": "PROJECT_HERMES_RELEASE_DIGEST",
                            "value": self.release_digest,
                        },
                        {
                            "name": "PROJECT_HERMES_WORKER_RUNTIME",
                            "value": "/runtime",
                        },
                    ],
                    "securityContext": security_context,
                    "volumeMounts": common_mounts,
                },
                {
                    "name": "prepare-source",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": [
                        "/runtime/venv/bin/python",
                        "/release/worker/prepare-source.py",
                    ],
                    "env": [
                        {
                            "name": "PROJECT_HERMES_SOURCE_BUNDLE",
                            "value": "/bundle/source.tar.gz",
                        },
                        {
                            "name": "PROJECT_HERMES_SOURCE_BUNDLE_DIGEST",
                            "value": worker.source_bundle_digest,
                        },
                        {
                            "name": "PROJECT_HERMES_SOURCE_ROOT",
                            "value": "/workspace",
                        },
                        {
                            "name": "PROJECT_HERMES_TASK_ID",
                            "value": request.task_id,
                        },
                        {
                            "name": "PROJECT_HERMES_REPOSITORY",
                            "value": request.repository,
                        },
                        {
                            "name": "PROJECT_HERMES_WORKSPACE_LEASE_ID",
                            "value": request.workspace_lease_id,
                        },
                        {
                            "name": "PROJECT_HERMES_BASE_SHA",
                            "value": worker.base_sha,
                        },
                        {
                            "name": "PROJECT_HERMES_CANDIDATE_DIGEST",
                            "value": request.candidate_digest,
                        },
                    ],
                    "securityContext": security_context,
                    "volumeMounts": [
                        *common_mounts,
                        {
                            "name": source_bundle_volume_name,
                            "mountPath": "/bundle/source.tar.gz",
                            "subPath": source_subpath,
                            "readOnly": True,
                        },
                        {"name": "workspace", "mountPath": "/workspace"},
                    ],
                },
            ],
            "containers": [
                {
                    "name": "codex",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "workingDir": "/workspace/repo",
                    "command": [
                        "/bin/bash",
                        "/release/worker/run.sh",
                        "/runtime/venv/bin/python",
                        "/release/worker/execute-task.py",
                        "--",
                        *request.command,
                    ],
                    "env": [
                        {
                            "name": "PROJECT_HERMES_WORKER_RUNTIME",
                            "value": "/runtime",
                        },
                        {
                            "name": "PROJECT_HERMES_EXECUTION_ID",
                            "value": execution_id,
                        },
                        {
                            "name": "PROJECT_HERMES_TASK_ID",
                            "value": request.task_id,
                        },
                        {
                            "name": "PROJECT_HERMES_REPOSITORY",
                            "value": request.repository,
                        },
                        *(
                            [
                                {
                                    "name": "PROJECT_HERMES_REPOSITORY_SKILL",
                                    "value": worker.repository_skill_name,
                                },
                                {
                                    "name": ("PROJECT_HERMES_REPOSITORY_SKILL_DIGEST"),
                                    "value": worker.repository_skill_digest,
                                },
                            ]
                            if worker.repository_skill_name is not None
                            else []
                        ),
                        {
                            "name": "PROJECT_HERMES_RELEASE_DIGEST",
                            "value": self.release_digest,
                        },
                        {
                            "name": "PROJECT_HERMES_IMAGE_DIGEST",
                            "value": request.image_digest,
                        },
                        {
                            "name": "PROJECT_HERMES_SOURCE_BUNDLE_DIGEST",
                            "value": worker.source_bundle_digest,
                        },
                        {
                            "name": "PROJECT_HERMES_SOURCE_ROOT",
                            "value": "/workspace",
                        },
                        {
                            "name": "PROJECT_HERMES_OUTPUT_ROOT",
                            "value": "/outputs",
                        },
                        {
                            "name": "PROJECT_HERMES_RESULT_PATH",
                            "value": "/outputs/result.json",
                        },
                        {
                            "name": "PROJECT_HERMES_USAGE_PATH",
                            "value": "/outputs/usage.json",
                        },
                        {
                            "name": "PROJECT_HERMES_ARTIFACT_ARCHIVE_ROOT",
                            "value": "/artifacts",
                        },
                        {
                            "name": "PROJECT_HERMES_MODEL_PROFILE",
                            "value": worker.model_profile,
                        },
                        {
                            "name": "PROJECT_HERMES_MODEL",
                            "value": worker.model,
                        },
                        {
                            "name": "PROJECT_HERMES_MODEL_PROVIDER",
                            "value": worker.model_provider,
                        },
                        {
                            "name": "PROJECT_HERMES_PROVIDER_ENDPOINT",
                            "value": worker.provider_endpoint,
                        },
                        {
                            "name": "PROJECT_HERMES_PROVIDER_API_KEY_ENV",
                            "value": worker.provider_api_key_env,
                        },
                        {
                            "name": "PROJECT_HERMES_PROVIDER_WIRE_API",
                            "value": worker.provider_wire_api,
                        },
                        *(
                            [
                                {
                                    "name": ("PROJECT_HERMES_MODEL_REASONING_EFFORT"),
                                    "value": worker.reasoning_effort,
                                }
                            ]
                            if worker.reasoning_effort is not None
                            else []
                        ),
                        *(
                            [
                                {
                                    "name": ("PROJECT_HERMES_MODEL_CONTEXT_WINDOW"),
                                    "value": str(worker.context_window),
                                }
                            ]
                            if worker.context_window is not None
                            else []
                        ),
                        *(
                            [
                                {
                                    "name": ("PROJECT_HERMES_HANDOFF_MODEL_PROFILE"),
                                    "value": worker.handoff_model_profile,
                                },
                                {
                                    "name": "PROJECT_HERMES_HANDOFF_MODEL",
                                    "value": worker.handoff_model,
                                },
                                {
                                    "name": "PROJECT_HERMES_HANDOFF_MODEL_PROVIDER",
                                    "value": worker.handoff_model_provider,
                                },
                                {
                                    "name": "PROJECT_HERMES_HANDOFF_PROVIDER_ENDPOINT",
                                    "value": worker.handoff_provider_endpoint,
                                },
                                {
                                    "name": (
                                        "PROJECT_HERMES_HANDOFF_PROVIDER_API_KEY_ENV"
                                    ),
                                    "value": worker.handoff_provider_api_key_env,
                                },
                                {
                                    "name": (
                                        "PROJECT_HERMES_HANDOFF_PROVIDER_WIRE_API"
                                    ),
                                    "value": worker.handoff_provider_wire_api,
                                },
                            ]
                            if worker.handoff_model is not None
                            else []
                        ),
                        {
                            "name": ("PROJECT_HERMES_CODEX_REQUEST_MAX_RETRIES"),
                            "value": str(self.config.codex_request_max_retries),
                        },
                        {
                            "name": ("PROJECT_HERMES_CODEX_STREAM_MAX_RETRIES"),
                            "value": str(self.config.codex_stream_max_retries),
                        },
                        {
                            "name": ("PROJECT_HERMES_CODEX_CAPACITY_RESUME_ATTEMPTS"),
                            "value": str(self.config.codex_capacity_resume_attempts),
                        },
                        {
                            "name": (
                                "PROJECT_HERMES_CODEX_CAPACITY_HANDOFF_AFTER_ATTEMPTS"
                            ),
                            "value": str(
                                self.config.codex_capacity_handoff_after_attempts
                            ),
                        },
                        {
                            "name": ("PROJECT_HERMES_CODEX_CAPACITY_BACKOFF_SECONDS"),
                            "value": str(self.config.codex_capacity_backoff_seconds),
                        },
                        *[
                            {
                                "name": environment_name,
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": self.config.model_secret_name,
                                        "key": secret_key,
                                    }
                                },
                            }
                            for environment_name, secret_key in (
                                credential_secret_keys.items()
                            )
                        ],
                        {"name": "HTTPS_PROXY", "value": proxy_url},
                        {"name": "HTTP_PROXY", "value": proxy_url},
                        {"name": "HOME", "value": "/runtime/home"},
                        {"name": "CODEX_HOME", "value": "/runtime/codex"},
                        {"name": "GIT_CONFIG_NOSYSTEM", "value": "1"},
                        {"name": "GIT_TERMINAL_PROMPT", "value": "0"},
                        {
                            "name": "HERMES_DISABLE_LAZY_INSTALLS",
                            "value": "1",
                        },
                        {
                            "name": "PYTHONDONTWRITEBYTECODE",
                            "value": "1",
                        },
                    ],
                    "resources": {
                        "requests": resource_requests,
                        "limits": resource_limits,
                    },
                    "securityContext": security_context,
                    "volumeMounts": [
                        *common_mounts,
                        {"name": "workspace", "mountPath": "/workspace"},
                        {"name": "outputs", "mountPath": "/outputs"},
                        {
                            "name": artifact_archive_volume_name,
                            "mountPath": "/artifacts",
                            "subPath": archive_subpath,
                        },
                    ],
                }
            ],
            "volumes": [
                *pvc_volumes,
                {"name": "runtime", "emptyDir": {"sizeLimit": "2Gi"}},
                {"name": "workspace", "emptyDir": {"sizeLimit": "8Gi"}},
                {"name": "outputs", "emptyDir": {"sizeLimit": "2Gi"}},
                {
                    "name": "tmp",
                    "emptyDir": {
                        "sizeLimit": self.config.worker_tmp_size_limit,
                    },
                },
            ],
        }
        if self.config.image_pull_secrets:
            pod_spec["imagePullSecrets"] = [
                {"name": name} for name in self.config.image_pull_secrets
            ]
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": self.config.namespace,
                "labels": labels,
                "annotations": {
                    "project-hermes.io/execution-id": execution_id,
                    "project-hermes.io/request-id": request.request_id,
                    "project-hermes.io/task-id": request.task_id,
                    "project-hermes.io/release-digest": self.release_digest,
                    "project-hermes.io/image-digest": request.image_digest,
                    "project-hermes.io/source-bundle-digest": (
                        worker.source_bundle_digest
                    ),
                    **(
                        {
                            "project-hermes.io/repository-skill": (
                                worker.repository_skill_name
                            ),
                            "project-hermes.io/repository-skill-digest": (
                                worker.repository_skill_digest
                            ),
                        }
                        if worker.repository_skill_name is not None
                        else {}
                    ),
                },
            },
            "spec": {
                "activeDeadlineSeconds": timeout_seconds,
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": (self.config.ttl_seconds_after_finished),
                "template": {
                    "metadata": {
                        "labels": labels,
                        "annotations": {
                            "project-hermes.io/release-digest": (self.release_digest),
                            "project-hermes.io/image-digest": (request.image_digest),
                        },
                    },
                    "spec": pod_spec,
                },
            },
        }

    def _worker_metadata(
        self,
        request: ExecutionRequest,
    ) -> CodexWorkerMetadata:
        raw = request.metadata.get(_CODEX_WORKER_METADATA_KEY)
        if not isinstance(raw, dict):
            raise ValueError("execution metadata requires a codex_worker object")
        return CodexWorkerMetadata.model_validate(raw)

    def _create_network_policy(
        self,
        manifest: dict[str, Any],
        execution_id: str,
    ) -> None:
        name = str(manifest["metadata"]["name"])
        try:
            self.api.create_network_policy(
                self.config.namespace,
                manifest,
            )
        except KubernetesApiError as exc:
            if exc.status_code != 409:
                raise
            existing = self.api.get_network_policy(
                self.config.namespace,
                name,
            )
            annotations = (
                existing.get("metadata", {}).get("annotations", {})
                if existing is not None
                else {}
            )
            if annotations.get("project-hermes.io/execution-id") != execution_id:
                raise RuntimeError(
                    "an unrelated NetworkPolicy already uses the "
                    "deterministic worker policy name"
                ) from exc

    def _create_or_recover_job(
        self,
        job_name: str,
        execution_id: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return self.api.create_job(self.config.namespace, manifest)
        except Exception:
            existing = self.api.get_job(self.config.namespace, job_name)
            annotations = (
                existing.get("metadata", {}).get("annotations", {})
                if existing is not None
                else {}
            )
            if annotations.get("project-hermes.io/execution-id") == execution_id:
                return existing
            if existing is not None:
                raise RuntimeError(
                    "an unrelated Kubernetes Job already uses the "
                    "deterministic execution name"
                )
            raise

    def _reclaim_policy_after_failed_start(
        self,
        job_name: str,
        execution_id: str,
    ) -> bool:
        existing = self.api.get_job(self.config.namespace, job_name)
        annotations = (
            existing.get("metadata", {}).get("annotations", {})
            if existing is not None
            else {}
        )
        if annotations.get("project-hermes.io/execution-id") == execution_id:
            return False
        self.api.delete_network_policy(
            self.config.namespace,
            _network_policy_name(job_name),
        )
        return True

    def _prepare_archive_scope(self, execution_id: str) -> Path:
        if not _ARCHIVE_IDENTIFIER_RE.fullmatch(execution_id):
            raise ValueError("execution_id is unsafe for archive storage")
        root = self.config.artifact_archive_root
        if root.is_symlink():
            raise ValueError("artifact archive root cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True)
        scope = root.resolve() / "execution-scopes" / execution_id
        scope.mkdir(parents=True, mode=0o700, exist_ok=True)
        if scope.is_symlink():
            raise ValueError("artifact archive scope cannot be a symlink")
        os.chmod(scope, 0o700)
        return scope

    def _verify_archive(
        self,
        job: dict[str, Any],
        execution_id: str,
    ) -> WorkerArtifactArchive:
        metadata = job.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("Job metadata is missing")
        annotations = metadata.get("annotations")
        if not isinstance(annotations, dict):
            raise ValueError("Job archive annotations are missing")
        annotated_execution_id = str(
            annotations.get("project-hermes.io/execution-id") or ""
        )
        if annotated_execution_id != execution_id:
            raise ValueError("Job execution annotation differs from expected execution")
        task_id = str(annotations.get("project-hermes.io/task-id") or "")
        release_digest = str(annotations.get("project-hermes.io/release-digest") or "")
        image_digest = str(annotations.get("project-hermes.io/image-digest") or "")
        source_digest = str(
            annotations.get("project-hermes.io/source-bundle-digest") or ""
        )
        if not _RELEASE_DIGEST_RE.fullmatch(release_digest):
            raise ValueError("Job release annotation is not a SHA-256 digest")
        scope = (
            self.config.artifact_archive_root.resolve()
            / "execution-scopes"
            / execution_id
        )
        return verify_worker_artifact_archive(
            scope,
            execution_id=execution_id,
            task_id=task_id,
            release_digest=release_digest,
            image_digest=image_digest,
            source_bundle_digest=source_digest,
        )


def _execution_label(execution_id: str) -> str:
    return hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:16]


def _worker_failure_kind(
    archive_root: Path,
    archive: WorkerArtifactArchive,
) -> str | None:
    """Classify the final Codex turn without trusting Kubernetes prose."""

    if archive.outcome != "failed":
        return None
    stdout_entry = next(
        (entry for entry in archive.entries if entry.name == "stdout.log"),
        None,
    )
    if stdout_entry is None:
        return None
    resolved_root = archive_root.resolve()
    stdout_path = (resolved_root / stdout_entry.object_path).resolve()
    if (
        archive_root.is_symlink()
        or stdout_path.is_symlink()
        or not stdout_path.is_file()
        or not stdout_path.is_relative_to(resolved_root)
    ):
        return None
    final_turn: tuple[str, str | None] | None = None
    capacity_failure_seen = False
    capacity_result_event_seen = False
    try:
        with stdout_path.open("rb") as stream:
            size = stdout_path.stat().st_size
            if size > _FAILURE_CLASSIFICATION_TAIL_BYTES:
                stream.seek(size - _FAILURE_CLASSIFICATION_TAIL_BYTES)
                stream.readline()
            for raw_line in stream:
                try:
                    payload = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                event_type = payload.get("type")
                if event_type == _CAPACITY_RESULT_EVENT:
                    capacity_result_event_seen = True
                    continue
                if event_type == "turn.completed":
                    final_turn = ("completed", None)
                elif event_type == "turn.failed":
                    error = payload.get("error")
                    message = error.get("message") if isinstance(error, dict) else error
                    final_turn = (
                        "failed",
                        message if isinstance(message, str) else None,
                    )
                    if isinstance(message, str) and _PROVIDER_CAPACITY_RE.search(
                        message
                    ):
                        capacity_failure_seen = True
    except OSError:
        return None
    if (
        capacity_result_event_seen
        or (
            final_turn is not None
            and final_turn[0] == "failed"
            and final_turn[1] is not None
            and _PROVIDER_CAPACITY_RE.search(final_turn[1])
        )
        or (archive.exit_code == 65 and capacity_failure_seen)
    ):
        return "provider_capacity_exhausted"
    return None


def _network_policy_name(job_name: str) -> str:
    return f"{job_name}-egress"


def _normalize_image_repository(value: str | None) -> str:
    if value is None:
        raise ValueError("image_repository is required for Kubernetes Job execution")
    parts = value.split("/")
    if len(parts) == 1:
        normalized = f"docker.io/library/{value}"
    else:
        first = parts[0]
        if "." not in first and ":" not in first and first != "localhost":
            normalized = f"docker.io/{value}"
        else:
            normalized = value
    normalized_parts = normalized.split("/")
    if (
        len(normalized_parts) < 2
        or not _REGISTRY_RE.fullmatch(normalized_parts[0])
        or any(
            not _IMAGE_COMPONENT_RE.fullmatch(component)
            for component in normalized_parts[1:]
        )
    ):
        raise ValueError("image_repository is not a valid OCI repository")
    return normalized


def _repository_is_allowed(
    repository: str,
    prefixes: list[str],
) -> bool:
    for prefix in prefixes:
        if prefix.endswith("/") and repository.startswith(prefix):
            return True
        if repository == prefix or repository.startswith(prefix + "/"):
            return True
    return False


def _job_name(execution_id: str, task_id: str) -> str:
    task = _SAFE_NAME_RE.sub("-", task_id.casefold()).strip("-") or "task"
    task = task[:30].rstrip("-")
    digest = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:12]
    return f"ph-{task}-{digest}"


def _native_identity(value: str) -> tuple[str, str]:
    namespace, separator, name = value.partition("/")
    if not separator or not namespace or not name or "/" in name:
        raise ValueError("invalid Kubernetes Job native identity")
    return namespace, name


def _parse_kubernetes_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _last_exit_code(pods: list[dict[str, Any]]) -> int | None:
    exit_codes: list[int] = []
    for pod in pods:
        statuses = pod.get("status", {}).get("containerStatuses", [])
        for status in statuses:
            terminated = status.get("state", {}).get("terminated")
            if isinstance(terminated, dict) and isinstance(
                terminated.get("exitCode"),
                int,
            ):
                exit_codes.append(terminated["exitCode"])
    return exit_codes[-1] if exit_codes else None


def _api_error_detail(raw: bytes) -> str:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")[:500]
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("reason") or payload)[:500]
    return str(payload)[:500]
