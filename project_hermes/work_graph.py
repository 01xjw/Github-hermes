"""Dynamic, event-driven work graph contracts and invariants."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from project_hermes.models import ProjectRole, StrictModel, utc_now


class LifecycleStatus(StrEnum):
    """Coarse control-plane lifecycle states.

    Technical activities such as locating, reproducing, planning, and
    validating are capabilities, not lifecycle states.
    """

    DISCOVERED = "DISCOVERED"
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    WAITING_RESOURCE = "WAITING_RESOURCE"
    WAITING_ARTIFACT = "WAITING_ARTIFACT"
    WAITING_REVIEW = "WAITING_REVIEW"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = frozenset(
    {
        LifecycleStatus.COMPLETED,
        LifecycleStatus.BLOCKED,
        LifecycleStatus.FAILED,
        LifecycleStatus.CANCELLED,
    }
)


class WorkNodeKind(StrEnum):
    """Generic node types accepted by the controller."""

    AGENT_INVOCATION = "agent_invocation"
    TOOL_CALL = "tool_call"
    EXECUTION = "execution"
    REVIEW = "review"
    APPROVAL = "approval"


class ActionKind(StrEnum):
    """Policy-checked requests an agent can submit."""

    READ_REPOSITORY = "read_repository"
    WRITE_WORKTREE = "write_worktree"
    CREATE_NODE = "create_node"
    REQUEST_EXECUTION = "request_execution"
    READ_EVIDENCE = "read_evidence"
    SUBMIT_EVIDENCE = "submit_evidence"
    REQUEST_REVIEW = "request_review"
    SUBMIT_REVIEW = "submit_review"
    RESOLVE_FINDING = "resolve_finding"
    REQUEST_APPROVAL = "request_approval"
    PUBLISH_PULL_REQUEST = "publish_pull_request"
    COMMIT_KNOWLEDGE = "commit_knowledge"
    CLEANUP_TASK = "cleanup_task"


class ActionRequest(StrictModel):
    """An untrusted request submitted to the control plane."""

    schema_version: Literal["action-request.v1"] = "action-request.v1"
    request_id: str
    task_id: str
    node_id: str | None = None
    requested_by: ProjectRole
    action: ActionKind
    capability: str | None = None
    repository: str | None = None
    path: str | None = None
    goal_revision_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class WorkNode(StrictModel):
    """One independently claimable unit of work."""

    schema_version: Literal["work-node.v1"] = "work-node.v1"
    node_id: str
    run_id: str
    kind: WorkNodeKind
    capability: str
    requested_role: ProjectRole
    status: LifecycleStatus = LifecycleStatus.DISCOVERED
    dependencies: list[str] = Field(default_factory=list)
    repository: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    owner_token: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("dependencies")
    @classmethod
    def unique_dependencies(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_lease_and_dependencies(self) -> "WorkNode":
        if self.node_id in self.dependencies:
            raise ValueError("a work node cannot depend on itself")
        if (self.owner_token is None) != (self.lease_expires_at is None):
            raise ValueError("owner_token and lease_expires_at must be set together")
        if self.status not in {LifecycleStatus.CLAIMED, LifecycleStatus.RUNNING}:
            if self.owner_token is not None:
                raise ValueError("only claimed or running nodes may hold a lease")
        return self


_ALLOWED_TRANSITIONS: dict[LifecycleStatus, frozenset[LifecycleStatus]] = {
    LifecycleStatus.DISCOVERED: frozenset(
        {
            LifecycleStatus.QUEUED,
            LifecycleStatus.BLOCKED,
            LifecycleStatus.CANCELLED,
        }
    ),
    LifecycleStatus.QUEUED: frozenset(
        {
            LifecycleStatus.CLAIMED,
            LifecycleStatus.WAITING_RESOURCE,
            LifecycleStatus.WAITING_ARTIFACT,
            LifecycleStatus.WAITING_APPROVAL,
            LifecycleStatus.BLOCKED,
            LifecycleStatus.CANCELLED,
        }
    ),
    LifecycleStatus.CLAIMED: frozenset(
        {
            LifecycleStatus.RUNNING,
            LifecycleStatus.QUEUED,
            LifecycleStatus.FAILED,
            LifecycleStatus.CANCELLED,
        }
    ),
    LifecycleStatus.RUNNING: frozenset(
        {
            LifecycleStatus.QUEUED,
            LifecycleStatus.WAITING_RESOURCE,
            LifecycleStatus.WAITING_ARTIFACT,
            LifecycleStatus.WAITING_REVIEW,
            LifecycleStatus.WAITING_APPROVAL,
            LifecycleStatus.COMPLETED,
            LifecycleStatus.BLOCKED,
            LifecycleStatus.FAILED,
            LifecycleStatus.CANCELLED,
        }
    ),
    LifecycleStatus.WAITING_RESOURCE: frozenset(
        {
            LifecycleStatus.QUEUED,
            LifecycleStatus.BLOCKED,
            LifecycleStatus.FAILED,
            LifecycleStatus.CANCELLED,
        }
    ),
    LifecycleStatus.WAITING_ARTIFACT: frozenset(
        {
            LifecycleStatus.QUEUED,
            LifecycleStatus.BLOCKED,
            LifecycleStatus.FAILED,
            LifecycleStatus.CANCELLED,
        }
    ),
    LifecycleStatus.WAITING_REVIEW: frozenset(
        {
            LifecycleStatus.QUEUED,
            LifecycleStatus.WAITING_APPROVAL,
            LifecycleStatus.COMPLETED,
            LifecycleStatus.BLOCKED,
            LifecycleStatus.FAILED,
            LifecycleStatus.CANCELLED,
        }
    ),
    LifecycleStatus.WAITING_APPROVAL: frozenset(
        {
            LifecycleStatus.QUEUED,
            LifecycleStatus.COMPLETED,
            LifecycleStatus.BLOCKED,
            LifecycleStatus.CANCELLED,
        }
    ),
    LifecycleStatus.COMPLETED: frozenset(),
    LifecycleStatus.BLOCKED: frozenset(
        {LifecycleStatus.QUEUED, LifecycleStatus.CANCELLED}
    ),
    LifecycleStatus.FAILED: frozenset(
        {LifecycleStatus.QUEUED, LifecycleStatus.CANCELLED}
    ),
    LifecycleStatus.CANCELLED: frozenset(),
}


def transition_allowed(
    current: LifecycleStatus, target: LifecycleStatus
) -> bool:
    """Return whether a lifecycle transition is permitted."""

    return target in _ALLOWED_TRANSITIONS[current]


class WorkGraph(StrictModel):
    """A persistent DAG whose technical shape is chosen by Hermes."""

    schema_version: Literal["work-graph.v1"] = "work-graph.v1"
    run_id: str
    task_id: str
    nodes: dict[str, WorkNode] = Field(default_factory=dict)
    revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_graph(self) -> "WorkGraph":
        for key, node in self.nodes.items():
            if key != node.node_id:
                raise ValueError("work graph keys must match node ids")
            if node.run_id != self.run_id:
                raise ValueError("all work nodes must belong to the graph run")
            missing = set(node.dependencies) - set(self.nodes)
            if missing:
                raise ValueError(
                    f"node {node.node_id} has unknown dependencies: "
                    + ", ".join(sorted(missing))
                )
        self._assert_acyclic()
        return self

    def _assert_acyclic(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                raise ValueError(f"work graph contains a cycle at {node_id}")
            visiting.add(node_id)
            for dependency in self.nodes[node_id].dependencies:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in self.nodes:
            visit(node_id)

    def add_node(self, node: WorkNode) -> None:
        """Add one node while preserving DAG and idempotency invariants."""

        if node.run_id != self.run_id:
            raise ValueError("node belongs to a different run")
        if node.node_id in self.nodes:
            raise ValueError(f"duplicate work node id: {node.node_id}")
        missing = set(node.dependencies) - set(self.nodes)
        if missing:
            raise ValueError(
                "dependencies must exist before the node is added: "
                + ", ".join(sorted(missing))
            )
        if node.idempotency_key is not None and any(
            existing.idempotency_key == node.idempotency_key
            for existing in self.nodes.values()
        ):
            raise ValueError(
                f"duplicate work-node idempotency key: {node.idempotency_key}"
            )
        self.nodes[node.node_id] = node
        self.revision += 1
        self.updated_at = utc_now()
        self._assert_acyclic()

    def ready_node_ids(self) -> list[str]:
        """Return queued nodes whose dependencies completed."""

        completed = {
            node_id
            for node_id, node in self.nodes.items()
            if node.status is LifecycleStatus.COMPLETED
        }
        return sorted(
            node_id
            for node_id, node in self.nodes.items()
            if node.status is LifecycleStatus.QUEUED
            and set(node.dependencies).issubset(completed)
        )

    def transition(
        self,
        node_id: str,
        target: LifecycleStatus,
        *,
        output: dict[str, Any] | None = None,
        owner_token: str | None = None,
        lease_expires_at: datetime | None = None,
    ) -> WorkNode:
        """Apply one policy-neutral lifecycle transition."""

        node = self.nodes[node_id]
        if not transition_allowed(node.status, target):
            raise ValueError(f"invalid transition: {node.status} -> {target}")

        lease_owner = owner_token
        lease_expiry = lease_expires_at
        if target not in {LifecycleStatus.CLAIMED, LifecycleStatus.RUNNING}:
            lease_owner = None
            lease_expiry = None
        if target in {LifecycleStatus.CLAIMED, LifecycleStatus.RUNNING}:
            lease_owner = owner_token or node.owner_token
            lease_expiry = lease_expires_at or node.lease_expires_at
            if lease_owner is None or lease_expiry is None:
                raise ValueError("claimed or running transitions require a lease")

        updated = node.model_copy(
            update={
                "status": target,
                "output": output if output is not None else node.output,
                "owner_token": lease_owner,
                "lease_expires_at": lease_expiry,
                "updated_at": utc_now(),
            }
        )
        self.nodes[node_id] = WorkNode.model_validate(updated.model_dump())
        self.revision += 1
        self.updated_at = utc_now()
        return self.nodes[node_id]


class PipelineRun(StrictModel):
    """Top-level v2 run projection."""

    schema_version: Literal["pipeline-run.v2"] = "pipeline-run.v2"
    run_id: str
    task_id: str
    status: LifecycleStatus = LifecycleStatus.DISCOVERED
    goal_revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class AgentSession(StrictModel):
    """Durable identity for one runtime session."""

    schema_version: Literal["agent-session.v1"] = "agent-session.v1"
    session_id: str
    task_id: str
    runtime: str
    role: ProjectRole
    status: LifecycleStatus
    root_thread_id: str | None = None
    socket_path: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentEvent(StrictModel):
    """Append-only runtime or control-plane event."""

    schema_version: Literal["agent-event.v1"] = "agent-event.v1"
    event_id: str
    run_id: str
    event_type: str
    sequence: int = Field(ge=1)
    node_id: str | None = None
    session_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
