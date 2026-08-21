"""ProjectHermes configuration loading and security validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import Field, field_validator, model_validator

from project_hermes.models import ResourceLimits, StrictModel
from project_hermes.resources import GpuDevice

PINNED_CODEX_SDK_VERSION = "0.144.4"
SUPPORTED_CODEX_REASONING_EFFORTS = frozenset({
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
})
DEFAULT_POLLING_REPOSITORIES: tuple[str, ...] = (
    "ROCm/ROCm",
    "ROCm/composable_kernel",
    "ROCm/aiter",
    "pytorch/pytorch",
    "pytorch/vision",
    "pytorch/executorch",
    "microsoft/onnxruntime",
    "huggingface/diffusers",
    "huggingface/transformers",
    "vllm-project/vllm",
    "sgl-project/sglang",
    "ggml-org/llama.cpp",
    "flashinfer-ai/flashinfer",
    "deepseek-ai/DeepEP",
    "deepseek-ai/DeepGEMM",
    "triton-lang/triton",
    "PaddlePaddle/Paddle",
    "SemiAnalysisAI/InferenceX",
)
_CREDENTIAL_ENV_RE = re.compile(
    r"^[A-Z][A-Z0-9_]*(?:"
    r"_API_KEY|_ACCESS_KEY|_ACCESS_KEY_ID|_SECRET_ACCESS_KEY|"
    r"_SECRET_KEY|_TOKEN|_SECRET|_PASSWORD|_CREDENTIALS?"
    r")$"
)
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_KUBERNETES_RESOURCE_RE = re.compile(
    r"^(?:[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?/)?"
    r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])$"
)
_KUBERNETES_QUANTITY_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:m|[EPTGMK]i?)?$"
)
_KUBERNETES_SECRET_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ControlPlaneMode(StrEnum):
    """Supported controller persistence modes."""

    SQLITE = "sqlite"
    POSTGRES = "postgres"
    HTTP = "http"


class CredentialDeliveryMode(StrEnum):
    """Reviewed credential delivery mechanisms."""

    DISABLED = "disabled"
    FILE_MOUNT = "file_mount"


class ModelReasoningMode(StrEnum):
    """Auditable reasoning behavior requested from a model provider."""

    PROVIDER_DEFAULT = "provider_default"
    ADAPTIVE = "adaptive"
    DISABLED = "disabled"


class IssueRelevancePolicy(StrEnum):
    """Mechanical hardware relevance policy for one watched repository."""

    AMD_OR_PORTABLE = "amd_or_portable"
    AMD_NATIVE = "amd_native"
    DISABLED = "disabled"


class ModelRouteConfig(StrictModel):
    """Credential-free route for one named model profile."""

    profile: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    model_provider: str = Field(min_length=1, max_length=64)
    provider_endpoint: str
    provider_api_key_env: str
    provider_wire_api: Literal["responses", "chat"] = "chat"
    reasoning_mode: ModelReasoningMode = ModelReasoningMode.PROVIDER_DEFAULT
    reasoning_effort: str | None = Field(default=None, max_length=32)
    context_window: int | None = Field(default=None, ge=1, le=10_000_000)

    @field_validator("provider_api_key_env")
    @classmethod
    def validate_provider_api_key_env(cls, value: str) -> str:
        if not is_credential_environment_name(value):
            raise ValueError(
                "provider_api_key_env must use a credential-only environment name"
            )
        return value

    @field_validator("provider_endpoint")
    @classmethod
    def validate_provider_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("provider_endpoint must be a credential-free HTTP(S) URL")
        return value.rstrip("/")


def _digitalocean_model_route(
    *,
    profile: str,
    model: str,
    reasoning_mode: ModelReasoningMode = ModelReasoningMode.PROVIDER_DEFAULT,
    reasoning_effort: str | None = None,
    context_window: int | None = None,
    provider_wire_api: Literal["responses", "chat"] = "chat",
) -> ModelRouteConfig:
    return ModelRouteConfig(
        profile=profile,
        model=model,
        model_provider="digitalocean",
        provider_endpoint="https://inference.do-ai.run/v1",
        provider_api_key_env="MODEL_ACCESS_KEY",
        provider_wire_api=provider_wire_api,
        reasoning_mode=reasoning_mode,
        reasoning_effort=reasoning_effort,
        context_window=context_window,
    )


def _deepseek_model_route(
    *,
    profile: str,
    model: str = "deepseek-v4-pro",
    provider_wire_api: Literal["responses", "chat"] = "chat",
) -> ModelRouteConfig:
    """Build the reviewed DeepSeek official-API route."""

    return ModelRouteConfig(
        profile=profile,
        model=model,
        model_provider="deepseek",
        provider_endpoint="https://api.deepseek.com/v1",
        provider_api_key_env="DEEPSEEK_API_KEY",
        provider_wire_api=provider_wire_api,
        reasoning_mode=ModelReasoningMode.PROVIDER_DEFAULT,
        reasoning_effort=None,
        context_window=1_000_000,
    )


def _minimax_cn_model_route(
    *,
    profile: str,
    model: str = "MiniMax-M3",
) -> ModelRouteConfig:
    """Build the reviewed MiniMax China official OpenAI-compatible route."""

    return ModelRouteConfig(
        profile=profile,
        model=model,
        model_provider="minimax-cn",
        provider_endpoint="https://api.minimaxi.com/v1",
        provider_api_key_env="MINIMAX_CN_API_KEY",
        provider_wire_api="chat",
        reasoning_mode=ModelReasoningMode.PROVIDER_DEFAULT,
        reasoning_effort=None,
        context_window=1_000_000,
    )


class HermesRuntimeConfig(StrictModel):
    """Model policy for implementation coordination and review sessions."""

    session_db_path: Path = Path(".project-hermes/hermes-sessions.db")
    allowed_tools: tuple[str, ...] = ()
    loop_interval_seconds: float = Field(default=5.0, ge=0.1, le=3600)
    credentials_file: Path | None = None
    model_profile: ModelRouteConfig = Field(
        default_factory=lambda: _deepseek_model_route(profile="hermes")
    )
    reviewer_profile: ModelRouteConfig | None = Field(
        default_factory=lambda: _minimax_cn_model_route(
            profile="reviewer",
        )
    )
    worker_handoff_profile: ModelRouteConfig | None = None

    @field_validator("allowed_tools")
    @classmethod
    def validate_allowed_tools(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not value for value in values):
            raise ValueError("Hermes allowed_tools entries cannot be empty")
        if len(values) != len(set(values)):
            raise ValueError("Hermes allowed_tools entries must be unique")
        return values

    def route_for_review(self) -> ModelRouteConfig:
        """Return the reviewer route, inheriting Hermes when unspecified."""

        return self.reviewer_profile or self.model_profile

    def route_for_worker_handoff(self) -> ModelRouteConfig:
        """Return the stateless worker-continuity summarizer route."""

        return self.worker_handoff_profile or self.model_profile


class ControlPlaneConfig(StrictModel):
    """Control-plane connection and local development fallback."""

    mode: ControlPlaneMode = ControlPlaneMode.SQLITE
    database_url: str | None = None
    api_url: str | None = None
    sqlite_path: Path = Path(".project-hermes/control-plane.db")
    lease_seconds: int = Field(default=300, ge=15, le=3600)

    @model_validator(mode="after")
    def selected_mode_has_location(self) -> "ControlPlaneConfig":
        if self.mode is ControlPlaneMode.POSTGRES and not self.database_url:
            raise ValueError("postgres mode requires database_url")
        if self.mode is ControlPlaneMode.HTTP and not self.api_url:
            raise ValueError("http mode requires api_url")
        if self.database_url:
            parsed = urlsplit(self.database_url)
            try:
                parsed.port
            except ValueError as exc:
                raise ValueError("database_url contains an invalid port") from exc
            if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
                raise ValueError("database_url must be a PostgreSQL connection URL")
        if self.api_url:
            parsed = urlsplit(self.api_url)
            try:
                parsed.port
            except ValueError as exc:
                raise ValueError("api_url contains an invalid port") from exc
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("api_url must be an HTTP or HTTPS URL")
        return self


class PollingRepositoryConfig(StrictModel):
    """One configurable GitHub repository watched by the polling task."""

    repository: str
    enabled: bool = True
    include_labels: tuple[str, ...] = ()
    exclude_labels: tuple[str, ...] = ()
    minimum_evidence_score: int | None = Field(default=None, ge=0, le=1000)
    relevance_policy: IssueRelevancePolicy = IssueRelevancePolicy.AMD_OR_PORTABLE
    skill_name: str | None = None

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        parts = value.split("/")
        if (
            len(parts) != 2
            or not all(parts)
            or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)
        ):
            raise ValueError("polling repositories must use owner/name form")
        return value

    @field_validator("include_labels", "exclude_labels")
    @classmethod
    def normalize_labels(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            dict.fromkeys(value.strip().casefold() for value in values if value.strip())
        )
        return normalized

    @field_validator("skill_name")
    @classmethod
    def validate_skill_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", normalized):
            raise ValueError("polling skill_name must use lowercase skill-name form")
        return normalized

    @property
    def resolved_skill_name(self) -> str:
        if self.skill_name is not None:
            return self.skill_name
        slug = re.sub(r"[^a-z0-9]+", "-", self.repository.casefold()).strip("-")
        return f"solve-{slug}"[:64].rstrip("-")

    @model_validator(mode="after")
    def label_policies_do_not_overlap(self) -> "PollingRepositoryConfig":
        overlap = set(self.include_labels) & set(self.exclude_labels)
        if overlap:
            raise ValueError(
                "polling include_labels and exclude_labels overlap: "
                + ", ".join(sorted(overlap))
            )
        return self


def _default_polling_repositories() -> tuple[PollingRepositoryConfig, ...]:
    return tuple(
        PollingRepositoryConfig(repository=repository)
        for repository in DEFAULT_POLLING_REPOSITORIES
    )


class PollingConfig(StrictModel):
    """Schedule, repository scope, and mechanical Issue filter policy."""

    enabled: bool = True
    interval_seconds: int = Field(default=900, ge=30, le=86_400)
    rolling_window_days: int = Field(default=30, ge=1, le=365)
    repositories_per_run: int = Field(default=1, ge=1, le=100)
    repositories: tuple[PollingRepositoryConfig, ...] = Field(
        default_factory=_default_polling_repositories
    )
    sqlite_path: Path = Path(".project-hermes/polling.db")
    agent_instructions_path: Path = Path("project_hermes/AGENT.md")
    repository_skills_path: Path = Path("project_hermes/repository_skills")
    require_repository_skills: bool = False
    # Guided training mode keeps discovery and relevance filtering automatic,
    # but requires an authenticated operator to admit each Issue into Work.
    # Once admitted, Main Hermes planning, Codex execution, and Review use the
    # normal production pipeline unchanged.
    require_operator_selection: bool = False
    # A capability-free DeepSeek Subagent evaluates every frozen Issue
    # snapshot before the operator admits any Work in guided training mode.
    issue_screening_enabled: bool = False
    issue_screening_profile_path: Path = Path(
        "project_hermes/screening_profiles/environment-issue-screener"
    )
    issue_screening_batch_size: int = Field(default=12, ge=1, le=50)
    # Invalid model protocol output is discarded as one atomic batch. Each
    # retry uses a fresh screening session and receives only schema paths and
    # error types, never controller-repaired conclusions or raw invalid input.
    issue_screening_protocol_retries: int = Field(default=2, ge=0, le=5)
    issue_screening_software_capabilities: tuple[str, ...] = ()
    issue_screening_unavailable_capabilities: tuple[str, ...] = ()
    issue_screening_validation_constraints: tuple[str, ...] = ()
    issue_screening_probe_consumer: str | None = Field(
        default=None,
        max_length=500,
    )
    require_screening_select_for_operator: bool = False
    minimum_evidence_score: int = Field(default=4, ge=0, le=1000)
    exclude_labels: tuple[str, ...] = (
        "duplicate",
        "invalid",
        "wontfix",
        "question",
    )
    require_body: bool = True
    max_issue_pages_per_repository: int = Field(default=1, ge=1, le=100)
    max_candidates_per_repository: int = Field(default=30, ge=1, le=5000)
    max_candidates_per_run: int = Field(default=30, ge=1, le=5000)
    github_request_timeout_seconds: int = Field(default=60, ge=1, le=300)
    github_max_retries: int = Field(default=3, ge=0, le=10)
    git_command_timeout_seconds: int = Field(
        default=900,
        ge=30,
        le=3600,
    )
    git_fetch_attempts: int = Field(default=3, ge=1, le=10)
    git_fetch_retry_backoff_seconds: int = Field(
        default=5,
        ge=0,
        le=300,
    )
    # Keep at most one queued/planning item per worker lane. Polling resumes as
    # soon as Main Hermes dispatches an item into a running lane.
    max_pending_work_items: int = Field(default=6, ge=1, le=128)
    max_active_work_items: int = Field(default=6, ge=1, le=32)
    max_global_jobs: int = Field(default=6, ge=1, le=32)
    max_global_gpus: int = Field(default=6, ge=0, le=8)
    # A non-unanimous independent review returns the unchanged Work item to a
    # fresh Worker with bounded feedback. Count the initial execution in this
    # ceiling so review cannot create an unbounded revision loop.
    max_review_execution_attempts: int = Field(default=3, ge=1, le=10)
    manager_projection_limit: int = Field(default=6, ge=1, le=100)
    manager_issue_body_char_limit: int = Field(
        default=6000,
        ge=1000,
        le=20_000,
    )
    manager_fresh_session_per_decision: bool = True
    manager_idle_interval_seconds: int = Field(default=300, ge=30, le=3600)
    manager_error_retry_seconds: int = Field(default=60, ge=30, le=300)
    manager_protocol_retry_seconds: int = Field(default=30, ge=5, le=300)
    worker_capacity_retry_base_seconds: int = Field(
        default=300,
        ge=30,
        le=86_400,
    )
    worker_capacity_retry_max_seconds: int = Field(
        default=3600,
        ge=30,
        le=86_400,
    )

    @field_validator("repositories")
    @classmethod
    def validate_repositories(
        cls,
        values: tuple[PollingRepositoryConfig, ...],
    ) -> tuple[PollingRepositoryConfig, ...]:
        if not values:
            raise ValueError("polling requires at least one repository")
        names = [value.repository.casefold() for value in values]
        if len(names) != len(set(names)):
            raise ValueError("polling repositories must be unique")
        return values

    @field_validator("exclude_labels")
    @classmethod
    def normalize_excluded_labels(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(value.strip().casefold() for value in values if value.strip())
        )

    @field_validator(
        "issue_screening_software_capabilities",
        "issue_screening_unavailable_capabilities",
        "issue_screening_validation_constraints",
    )
    @classmethod
    def normalize_screening_capability_facts(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized = tuple(
            dict.fromkeys(value.strip() for value in values if value.strip())
        )
        if len(normalized) > 100:
            raise ValueError("Issue screening capability facts exceed 100 entries")
        if any(len(value) > 500 for value in normalized):
            raise ValueError("Issue screening capability fact exceeds 500 characters")
        return normalized

    @field_validator("issue_screening_probe_consumer")
    @classmethod
    def normalize_screening_probe_consumer(
        cls,
        value: str | None,
    ) -> str | None:
        normalized = (value or "").strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_worker_capacity_retry_window(self) -> "PollingConfig":
        if (
            self.worker_capacity_retry_max_seconds
            < self.worker_capacity_retry_base_seconds
        ):
            raise ValueError(
                "worker capacity retry maximum must be at least the base delay"
            )
        return self

    @model_validator(mode="after")
    def screening_gate_requires_guided_screening(self) -> "PollingConfig":
        if self.require_screening_select_for_operator and not (
            self.require_operator_selection and self.issue_screening_enabled
        ):
            raise ValueError(
                "screening-gated admission requires guided mode and Issue screening"
            )
        return self


class CodexTaskRuntimeConfig(StrictModel):
    """Per-task Codex app-server policy."""

    enabled: bool = False
    sdk_version: Literal["0.144.4"] = PINNED_CODEX_SDK_VERSION
    cli_version: Literal["0.144.4"] = PINNED_CODEX_SDK_VERSION
    runtime_root: Path = Path(".project-hermes/runtime")
    model: str | None = None
    model_provider: str | None = None
    provider_endpoint: str | None = None
    provider_api_key_env: str | None = None
    provider_wire_api: Literal["responses", "chat"] = "responses"
    model_profiles: dict[str, ModelRouteConfig] = Field(
        default_factory=lambda: {
            "primary": _deepseek_model_route(
                profile="codex-primary",
                provider_wire_api="responses",
            ),
            "escalated": _deepseek_model_route(
                profile="codex-escalated",
                provider_wire_api="responses",
            ),
        }
    )
    primary_profile: str = "primary"
    escalation_profile: str = "escalated"
    credential_delivery: CredentialDeliveryMode = CredentialDeliveryMode.DISABLED
    credentials_file: Path | None = None
    network_access: bool = False
    startup_timeout_seconds: int = Field(default=30, ge=1, le=300)
    turn_timeout_seconds: int = Field(default=1800, ge=30, le=86_400)

    @field_validator("provider_api_key_env")
    @classmethod
    def validate_provider_api_key_env(
        cls,
        value: str | None,
    ) -> str | None:
        if value is not None and not is_credential_environment_name(value):
            raise ValueError(
                "provider_api_key_env must use a credential-only environment name"
            )
        return value

    @model_validator(mode="after")
    def enabled_runtime_has_reviewed_credentials(self) -> "CodexTaskRuntimeConfig":
        unsupported_efforts = sorted({
            profile.reasoning_effort
            for profile in self.model_profiles.values()
            if profile.reasoning_effort is not None
            and profile.reasoning_effort not in SUPPORTED_CODEX_REASONING_EFFORTS
        })
        if unsupported_efforts:
            raise ValueError(
                "Codex reasoning_effort must be supported by the pinned CLI: "
                + ", ".join(unsupported_efforts)
            )
        if self.primary_profile not in self.model_profiles:
            raise ValueError("primary_profile must name a configured model profile")
        if self.escalation_profile not in self.model_profiles:
            raise ValueError("escalation_profile must name a configured model profile")
        profile_names = [profile.profile for profile in self.model_profiles.values()]
        if len(profile_names) != len(set(profile_names)):
            raise ValueError("Codex model profile identities must be unique")
        if self.enabled:
            if self.credential_delivery is not CredentialDeliveryMode.FILE_MOUNT:
                raise ValueError(
                    "enabled Codex runtimes require file_mount credential delivery"
                )
            if self.credentials_file is None:
                raise ValueError("enabled Codex runtimes require credentials_file")
            if not self.network_access:
                raise ValueError(
                    "enabled Codex model profiles require explicit network_access"
                )
        if (
            self.credential_delivery is CredentialDeliveryMode.FILE_MOUNT
            and self.credentials_file is None
        ):
            raise ValueError("file_mount credential delivery requires credentials_file")
        if self.provider_endpoint:
            if not self.provider_endpoint.startswith(("http://", "https://")):
                raise ValueError("provider_endpoint must use an HTTP or HTTPS URL")
            if not self.model_provider:
                raise ValueError("provider_endpoint requires a named model_provider")
            if not self.provider_api_key_env:
                raise ValueError("provider_endpoint requires provider_api_key_env")
            if not self.network_access:
                raise ValueError("provider_endpoint requires explicit network_access")
        return self

    def route(self, profile: str | None = None) -> ModelRouteConfig:
        """Resolve a configured Codex model route by policy name."""

        name = profile or self.primary_profile
        try:
            return self.model_profiles[name]
        except KeyError as exc:
            raise KeyError(f"unknown Codex model profile: {name}") from exc


class KubernetesJobConfig(StrictModel):
    """Restricted Kubernetes Job execution backend."""

    enabled: bool = False
    namespace: str = "project-hermes-jobs"
    node_name: str | None = None
    job_service_account_name: str = "project-hermes-job"
    gpu_resource_name: str = "amd.com/gpu"
    gpu_architecture_label: str = "amd.com/gpu.arch"
    allowed_image_prefixes: list[str] = Field(
        default_factory=lambda: [
            "docker.io/library/",
            "docker.io/rocm/",
        ]
    )
    image_pull_secrets: list[str] = Field(default_factory=list)
    cpu_request: str = "1"
    cpu_limit: str = "4"
    memory_request: str = "4Gi"
    memory_limit: str = "32Gi"
    worker_tmp_size_limit: str = "32Gi"
    max_timeout_seconds: int = Field(default=7200, ge=30, le=86_400)
    codex_request_max_retries: int = Field(default=12, ge=1, le=50)
    codex_stream_max_retries: int = Field(default=8, ge=1, le=50)
    codex_capacity_resume_attempts: int = Field(default=8, ge=0, le=20)
    codex_capacity_handoff_after_attempts: int = Field(
        default=2,
        ge=1,
        le=20,
    )
    codex_capacity_backoff_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
    )
    ttl_seconds_after_finished: int = Field(
        default=3600,
        ge=60,
        le=86_400,
    )
    delete_timeout_seconds: int = Field(default=60, ge=1, le=300)
    release_root: Path = Path("/app/project-hermes/release")
    active_release_state_path: Path = Path(
        "/app/project-hermes/state/hermes/release.json"
    )
    release_pvc_name: str = "github-agent-workspace"
    release_pvc_subpath_prefix: str = "releases/releases"
    source_bundle_root: Path = Path(
        "/app/project-hermes/state/controller/source-bundles"
    )
    source_bundle_pvc_name: str = "github-agent-workspace"
    source_bundle_pvc_subpath: str = "state/controller/source-bundles"
    artifact_archive_root: Path = Path(
        "/app/project-hermes/state/controller/worker-artifacts"
    )
    artifact_archive_pvc_name: str = "github-agent-workspace"
    artifact_archive_pvc_subpath: str = "state/controller/worker-artifacts"
    model_secret_name: str = "project-hermes-credentials"
    model_secret_key: str = "deepseek-api-key"
    model_proxy_service_name: str = "project-hermes-model-proxy"
    model_proxy_port: int = Field(default=3128, ge=1, le=65_535)
    max_active_jobs: Literal[8] = 8
    max_total_gpus: Literal[6] = 6

    @field_validator(
        "namespace",
        "job_service_account_name",
        "release_pvc_name",
        "source_bundle_pvc_name",
        "artifact_archive_pvc_name",
        "model_secret_name",
        "model_proxy_service_name",
        "image_pull_secrets",
    )
    @classmethod
    def validate_dns_labels(
        cls,
        value: str | list[str],
    ) -> str | list[str]:
        values = [value] if isinstance(value, str) else value
        if any(len(item) > 63 or not _DNS_LABEL_RE.fullmatch(item) for item in values):
            raise ValueError("Kubernetes names must be valid DNS labels")
        return value

    @field_validator("model_secret_key")
    @classmethod
    def validate_secret_key(cls, value: str) -> str:
        if len(value) > 253 or not _KUBERNETES_SECRET_KEY_RE.fullmatch(value):
            raise ValueError("invalid Kubernetes Secret key")
        return value

    @field_validator(
        "release_pvc_subpath_prefix",
        "source_bundle_pvc_subpath",
        "artifact_archive_pvc_subpath",
    )
    @classmethod
    def validate_pvc_subpath(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError("PVC subpaths must be confined relative paths")
        return path.as_posix()

    @field_validator("gpu_resource_name", "gpu_architecture_label")
    @classmethod
    def validate_resource_names(cls, value: str) -> str:
        if len(value) > 253 or not _KUBERNETES_RESOURCE_RE.fullmatch(value):
            raise ValueError("invalid Kubernetes resource or label name")
        return value

    @field_validator(
        "cpu_request",
        "cpu_limit",
        "memory_request",
        "memory_limit",
        "worker_tmp_size_limit",
    )
    @classmethod
    def validate_resource_quantities(cls, value: str) -> str:
        if not _KUBERNETES_QUANTITY_RE.fullmatch(value):
            raise ValueError("invalid Kubernetes resource quantity")
        return value

    @field_validator("allowed_image_prefixes")
    @classmethod
    def validate_image_prefixes(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(values))
        if not normalized:
            raise ValueError("at least one image repository prefix is required")
        if any(
            value != value.casefold()
            or "://" in value
            or "@" in value
            or any(ord(character) < 33 for character in value)
            for value in normalized
        ):
            raise ValueError("image prefixes must be lowercase OCI repository prefixes")
        return normalized

    @model_validator(mode="after")
    def limits_cover_requests(self) -> "KubernetesJobConfig":
        cpu_request = _parse_cpu(self.cpu_request)
        cpu_limit = _parse_cpu(self.cpu_limit)
        if cpu_request > cpu_limit:
            raise ValueError("cpu_request cannot exceed cpu_limit")
        memory_request = _parse_binary_quantity(self.memory_request)
        memory_limit = _parse_binary_quantity(self.memory_limit)
        if memory_request > memory_limit:
            raise ValueError("memory_request cannot exceed memory_limit")
        for name, path in (
            ("release_root", self.release_root),
            ("active_release_state_path", self.active_release_state_path),
            ("source_bundle_root", self.source_bundle_root),
            ("artifact_archive_root", self.artifact_archive_root),
        ):
            if not path.is_absolute():
                raise ValueError(f"{name} must be an absolute path")
        return self


class ProjectHermesConfig(StrictModel):
    """Top-level, versioned ProjectHermes configuration."""

    schema_version: Literal["project-hermes-config.v1"] = "project-hermes-config.v1"
    project_root: Path = Path(".")
    control_plane: ControlPlaneConfig = Field(default_factory=ControlPlaneConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    hermes: HermesRuntimeConfig = Field(default_factory=HermesRuntimeConfig)
    codex: CodexTaskRuntimeConfig = Field(default_factory=CodexTaskRuntimeConfig)
    kubernetes_jobs: KubernetesJobConfig = Field(default_factory=KubernetesJobConfig)
    resources: ResourceLimits = Field(default_factory=ResourceLimits)
    gpu_devices: list[GpuDevice] = Field(
        default_factory=lambda: [GpuDevice(gpu_id=f"gpu-{index}") for index in range(8)]
    )
    english_only: bool = True

    @model_validator(mode="after")
    def validate_gpu_pool(self) -> "ProjectHermesConfig":
        gpu_ids = [device.gpu_id for device in self.gpu_devices]
        if len(gpu_ids) != len(set(gpu_ids)):
            raise ValueError("GPU device ids must be unique")
        if len(self.gpu_devices) > 8:
            raise ValueError("ProjectHermes supports at most eight configured GPUs")
        if self.kubernetes_jobs.enabled:
            if self.resources.max_parallel_nodes > self.kubernetes_jobs.max_active_jobs:
                raise ValueError("Kubernetes execution cannot exceed eight active Jobs")
            if self.resources.max_gpu_count > self.kubernetes_jobs.max_total_gpus:
                raise ValueError("Kubernetes execution cannot exceed six active GPUs")
        return self

    def resolved(self, config_path: Path) -> "ProjectHermesConfig":
        """Resolve relative project-owned paths against the config directory."""

        base = config_path.resolve().parent

        def resolve(path: Path | None) -> Path | None:
            if path is None or path.is_absolute():
                return path
            return (base / path).resolve()

        updated = self.model_copy(deep=True)
        updated.project_root = resolve(updated.project_root) or base
        updated.control_plane.sqlite_path = (
            resolve(updated.control_plane.sqlite_path)
            or updated.control_plane.sqlite_path
        )
        updated.polling.sqlite_path = (
            resolve(updated.polling.sqlite_path) or updated.polling.sqlite_path
        )
        if not updated.polling.agent_instructions_path.is_absolute():
            updated.polling.agent_instructions_path = (
                updated.project_root / updated.polling.agent_instructions_path
            ).resolve()
        if not updated.polling.repository_skills_path.is_absolute():
            updated.polling.repository_skills_path = (
                updated.project_root / updated.polling.repository_skills_path
            ).resolve()
        updated.hermes.session_db_path = (
            resolve(updated.hermes.session_db_path) or updated.hermes.session_db_path
        )
        updated.hermes.credentials_file = resolve(updated.hermes.credentials_file)
        updated.codex.runtime_root = (
            resolve(updated.codex.runtime_root) or updated.codex.runtime_root
        )
        updated.codex.credentials_file = resolve(updated.codex.credentials_file)
        return ProjectHermesConfig.model_validate(updated.model_dump())


def load_config(path: str | Path) -> ProjectHermesConfig:
    """Load a strict YAML configuration and validate local secret handling."""

    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream) or {}
    config = ProjectHermesConfig.model_validate(raw).resolved(config_path)
    if config.hermes.credentials_file is not None:
        validate_credentials_file(
            config.hermes.credentials_file,
            project_root=config.project_root,
        )
    if config.codex.credentials_file is not None:
        validate_credentials_file(
            config.codex.credentials_file,
            project_root=config.project_root,
        )
    return config


def validate_credentials_file(path: Path, *, project_root: Path) -> None:
    """Require a private, untracked credentials YAML without reading it."""

    if path.is_symlink():
        raise ValueError("credentials_file must not be a symbolic link")
    resolved = path.resolve()
    root = project_root.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("credentials_file must be located inside project_root")
    if not resolved.is_file():
        raise ValueError(f"credentials_file does not exist: {resolved}")

    details = resolved.stat()
    mode = stat.S_IMODE(details.st_mode)
    if mode & 0o077:
        raise ValueError("credentials_file permissions must be 0600 or stricter")
    if details.st_uid != os.geteuid():
        raise ValueError("credentials_file must be owned by the current user")
    if details.st_size > 65_536:
        raise ValueError("credentials_file exceeds 64 KiB")

    relative = resolved.relative_to(root)
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--error-unmatch",
            "--",
            str(relative),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode == 0:
        raise ValueError("credentials_file must not be tracked by git")


def assert_private_directory(path: Path) -> None:
    """Create a task-private directory and force owner-only permissions."""

    if path.is_symlink():
        raise ValueError(f"private directory cannot be a symbolic link: {path}")
    path.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)


def config_fingerprint(config: ProjectHermesConfig) -> str:
    """Return a stable, secret-free configuration digest."""

    payload = config.model_dump(mode="json")
    control_plane = payload.get("control_plane", {})
    for key in ("database_url", "api_url"):
        value = control_plane.get(key)
        if value:
            control_plane[key] = _secret_free_url(str(value))
    hermes = payload.get("hermes", {})
    if hermes.get("credentials_file"):
        hermes["credentials_file"] = "<configured>"
    codex = payload.get("codex", {})
    if codex.get("credentials_file"):
        codex["credentials_file"] = "<configured>"
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def is_credential_environment_name(value: str) -> bool:
    """Return whether an environment name is limited to credential material."""

    return bool(_CREDENTIAL_ENV_RE.fullmatch(value))


def _secret_free_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((
        parsed.scheme,
        f"{hostname}{port}",
        parsed.path,
        "",
        "",
    ))


def _parse_cpu(value: str) -> float:
    if value.endswith("m"):
        return int(value[:-1]) / 1000
    return float(value)


def _parse_binary_quantity(value: str) -> float:
    suffixes = {
        "Ki": 2**10,
        "Mi": 2**20,
        "Gi": 2**30,
        "Ti": 2**40,
        "Pi": 2**50,
        "Ei": 2**60,
        "K": 10**3,
        "M": 10**6,
        "G": 10**9,
        "T": 10**12,
        "P": 10**15,
        "E": 10**18,
    }
    for suffix, multiplier in suffixes.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * multiplier
    return float(value)
