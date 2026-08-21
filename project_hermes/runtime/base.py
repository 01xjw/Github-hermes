"""AgentRuntime service-provider interface."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Iterable, Literal, Protocol, runtime_checkable

from pydantic import Field

from project_hermes.config import ModelReasoningMode
from project_hermes.models import ProjectRole, StrictModel, utc_now


class RuntimeStatus(StrEnum):
    """Runtime session states independent from a specific agent framework."""

    STARTING = "STARTING"
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


class RuntimeModelRoute(StrictModel):
    """Resolved, credential-free model identity for one runtime session."""

    schema_version: Literal["runtime-model-route.v1"] = "runtime-model-route.v1"
    profile: str
    model: str
    model_provider: str
    provider_endpoint: str
    provider_api_key_env: str
    provider_wire_api: Literal["responses", "chat"]
    reasoning_mode: ModelReasoningMode = ModelReasoningMode.PROVIDER_DEFAULT
    reasoning_effort: str | None = None
    context_window: int | None = Field(default=None, ge=1, le=10_000_000)


class RuntimeRequest(StrictModel):
    """Framework-neutral request to start or resume an agent."""

    schema_version: Literal["runtime-request.v1"] = "runtime-request.v1"
    request_id: str
    task_id: str
    role: ProjectRole
    prompt: str
    cwd: str | None = None
    session_id: str | None = None
    model: str | None = None
    model_route: RuntimeModelRoute | None = None
    runtime_generation: int = Field(default=0, ge=0, le=1)
    review_cycle: int | None = Field(default=None, ge=1)
    context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeHandle(StrictModel):
    """Serializable runtime identity."""

    schema_version: Literal["runtime-handle.v1"] = "runtime-handle.v1"
    runtime_name: str
    session_id: str
    task_id: str
    status: RuntimeStatus
    model_route: RuntimeModelRoute | None = None
    runtime_generation: int = Field(default=0, ge=0, le=1)
    worktree_path: str | None = None
    native_thread_id: str | None = None
    transport_ref: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RuntimeEvent(StrictModel):
    """Normalized runtime event."""

    schema_version: Literal["runtime-event.v1"] = "runtime-event.v1"
    event_id: str
    session_id: str
    sequence: int = Field(ge=1)
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class RuntimeResult(StrictModel):
    """Normalized result for the most recent turn."""

    schema_version: Literal["runtime-result.v1"] = "runtime-result.v1"
    session_id: str
    status: RuntimeStatus
    final_response: str | None = None
    native_thread_id: str | None = None
    native_turn_id: str | None = None
    model_route: RuntimeModelRoute | None = None
    runtime_generation: int = Field(default=0, ge=0, le=1)
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)


@runtime_checkable
class AgentRuntime(Protocol):
    """Service-provider interface implemented by Hermes, Codex, or adapters."""

    name: str

    def start(self, request: RuntimeRequest) -> RuntimeHandle:
        """Create a session and run its first turn."""

    def resume(
        self, handle: RuntimeHandle, request: RuntimeRequest
    ) -> RuntimeHandle:
        """Run another turn on an existing native session."""

    def reconnect(
        self,
        handle: RuntimeHandle,
        request: RuntimeRequest,
    ) -> RuntimeHandle:
        """Rebuild adapter-local state without running a turn."""

    def cancel(self, handle: RuntimeHandle) -> RuntimeHandle:
        """Request cancellation of the active turn."""

    def events(
        self, handle: RuntimeHandle, *, after_sequence: int = 0
    ) -> Iterable[RuntimeEvent]:
        """Read normalized events after the supplied sequence."""

    def result(self, handle: RuntimeHandle) -> RuntimeResult | None:
        """Read the latest result without mutating the session."""

    def close(self, handle: RuntimeHandle) -> RuntimeHandle:
        """Release runtime resources while preserving durable identity."""
