"""Private Unix-socket protocol for one persistent Codex task runtime."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from project_hermes.config import (
    ModelReasoningMode,
    PINNED_CODEX_SDK_VERSION,
    is_credential_environment_name,
)
from project_hermes.models import StrictModel, utc_now

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class CodexCommandKind(StrEnum):
    """Commands accepted by a task-private daemon."""

    RUN_TURN = "run_turn"
    STATUS = "status"
    EVENTS = "events"
    RESULT = "result"
    CANCEL = "cancel"
    SHUTDOWN = "shutdown"


class CodexTaskSpec(StrictModel):
    """Immutable daemon provisioning input."""

    schema_version: Literal["codex-task-spec.v1"] = "codex-task-spec.v1"
    task_id: str
    session_id: str
    worktree_path: Path
    runtime_root: Path
    sdk_version: Literal["0.144.4"] = PINNED_CODEX_SDK_VERSION
    cli_version: Literal["0.144.4"] = PINNED_CODEX_SDK_VERSION
    model_profile: str | None = None
    model: str | None = None
    model_provider: str | None = None
    provider_endpoint: str | None = None
    provider_api_key_env: str | None = None
    provider_wire_api: Literal["responses", "chat"] = "responses"
    reasoning_mode: ModelReasoningMode = ModelReasoningMode.PROVIDER_DEFAULT
    reasoning_effort: str | None = None
    context_window: int | None = Field(default=None, ge=1, le=10_000_000)
    runtime_generation: int = Field(default=0, ge=0, le=1)
    credentials_file: Path | None = None
    network_access: bool = False
    turn_timeout_seconds: int = Field(default=1800, ge=30, le=86_400)

    @field_validator("task_id", "session_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not _TASK_ID_RE.fullmatch(value):
            raise ValueError(
                "task and session ids must use safe identifier characters"
            )
        return value

    @field_validator("provider_api_key_env")
    @classmethod
    def validate_provider_api_key_env(
        cls,
        value: str | None,
    ) -> str | None:
        if value is not None and not is_credential_environment_name(value):
            raise ValueError(
                "provider_api_key_env must use a credential-only "
                "environment name"
            )
        return value

    @model_validator(mode="after")
    def validate_provider_endpoint(self) -> "CodexTaskSpec":
        if self.provider_endpoint:
            if not self.model_provider or not self.provider_api_key_env:
                raise ValueError(
                    "custom provider endpoint requires provider name "
                    "and credential environment"
                )
            if not self.network_access:
                raise ValueError(
                    "custom provider endpoint requires network access"
                )
        return self

    def task_directory(self) -> Path:
        """Return a short, collision-resistant task state directory."""

        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", self.task_id).strip("-")[:32]
        digest = hashlib.sha256(self.task_id.encode("utf-8")).hexdigest()[:12]
        return self.runtime_root / f"{slug or 'task'}-{digest}"

    def socket_path(self) -> Path:
        return self.state_directory() / "codex.sock"

    def state_directory(self) -> Path:
        """Return isolated state for the primary or escalated daemon."""

        if self.runtime_generation == 0:
            return self.task_directory()
        return self.task_directory() / f"reprovision-{self.runtime_generation}"

    def codex_home(self) -> Path:
        return self.state_directory() / "codex-home"

    def sqlite_home(self) -> Path:
        return self.state_directory() / "sqlite"

    def metadata_path(self) -> Path:
        return self.state_directory() / "session.json"

    def events_path(self) -> Path:
        return self.state_directory() / "events.jsonl"

    def result_path(self) -> Path:
        return self.state_directory() / "last-result.json"

    def daemon_spec_path(self) -> Path:
        return self.state_directory() / "daemon-spec.json"


class CodexCommand(StrictModel):
    """One newline-delimited request sent over the private socket."""

    schema_version: Literal["codex-command.v1"] = "codex-command.v1"
    request_id: str
    command: CodexCommandKind
    prompt: str | None = None
    after_sequence: int = Field(default=0, ge=0)


class CodexReply(StrictModel):
    """One newline-delimited daemon response."""

    schema_version: Literal["codex-reply.v1"] = "codex-reply.v1"
    request_id: str
    ok: bool
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class CodexTaskMetadata(StrictModel):
    """Durable task-to-thread mapping used for restart resume."""

    schema_version: Literal["codex-task-metadata.v1"] = "codex-task-metadata.v1"
    task_id: str
    session_id: str
    root_thread_id: str | None = None
    active_turn_id: str | None = None
    status: str = "STARTING"
    pid: int | None = None
    sdk_version: str
    cli_version: str
    model_profile: str | None = None
    model: str | None = None
    model_provider: str | None = None
    reasoning_mode: ModelReasoningMode = ModelReasoningMode.PROVIDER_DEFAULT
    reasoning_effort: str | None = None
    context_window: int | None = Field(default=None, ge=1, le=10_000_000)
    runtime_generation: int = Field(default=0, ge=0, le=1)
    socket_path: str
    codex_home: str
    worktree_path: str
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
