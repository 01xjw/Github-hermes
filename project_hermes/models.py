"""Versioned goal and responsibility contracts for ProjectHermes."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


class StrictModel(BaseModel):
    """Base class for fail-closed external contracts."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class ProjectRole(StrEnum):
    """Security identities recognized by the control plane."""

    CONTROL_PLANE = "control_plane"
    LEGACY_CAMPAIGN_HERMES = "campaign_hermes"
    PROJECT_HERMES = "project_hermes"
    CODEX = "codex"
    RUNNER = "runner"
    REVIEWER = "reviewer"
    CURATOR = "curator"
    SCREENER = "screener"
    OPERATOR = "operator"


class TriageDecision(StrEnum):
    """Independent issue-triage outcomes."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
    DUPLICATE = "DUPLICATE"
    OBSOLETE = "OBSOLETE"


class RevisionDecision(StrEnum):
    """Approval state for a proposed goal change."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class PermissionBudget(StrictModel):
    """Explicit authority available to a project task."""

    readable_repositories: list[str] = Field(min_length=1)
    writable_repositories: list[str] = Field(default_factory=list)
    may_request_execution: bool = True
    may_request_review: bool = True
    may_publish: bool = False
    may_access_network: bool = False

    @field_validator("readable_repositories", "writable_repositories")
    @classmethod
    def validate_repositories(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(values))
        invalid = [value for value in normalized if not _REPOSITORY_RE.fullmatch(value)]
        if invalid:
            raise ValueError(
                "repository names must use owner/name form: " + ", ".join(invalid)
            )
        return normalized

    @model_validator(mode="after")
    def writable_repositories_must_be_readable(self) -> "PermissionBudget":
        unreadable = set(self.writable_repositories) - set(
            self.readable_repositories
        )
        if unreadable:
            raise ValueError(
                "writable repositories must also be readable: "
                + ", ".join(sorted(unreadable))
            )
        return self


class ResourceLimits(StrictModel):
    """Hard task limits fixed by the goal contract."""

    max_parallel_nodes: int = Field(default=7, ge=1, le=128)
    max_gpu_count: int = Field(default=8, ge=0, le=8)
    max_model_concurrency: int = Field(default=7, ge=1, le=128)
    max_artifact_bytes: int | None = Field(default=None, ge=1)


class TargetHardware(StrictModel):
    """Hardware requirements that affect acceptance evidence."""

    gpu_architectures: list[str] = Field(default_factory=list)
    minimum_gpu_count: int = Field(default=0, ge=0, le=8)
    topology: str | None = None
    environment_name: str

    @field_validator("gpu_architectures")
    @classmethod
    def normalize_architectures(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.lower() for value in values))


class GoalPath(StrictModel):
    """One independently verifiable path in the locked task scope."""

    path_id: str
    description: str = Field(min_length=1)
    repository: str
    acceptance_criteria: list[str] = Field(min_length=1)
    required_completion_layers: list[str] = Field(min_length=1)

    @field_validator("path_id")
    @classmethod
    def validate_path_id(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("path_id contains unsupported characters")
        return value

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if not _REPOSITORY_RE.fullmatch(value):
            raise ValueError("repository must use owner/name form")
        return value

    @field_validator("acceptance_criteria", "required_completion_layers")
    @classmethod
    def unique_non_empty_values(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values))
        if any(not value for value in normalized):
            raise ValueError("list entries cannot be empty")
        return normalized


class RepositoryResponsibility(StrictModel):
    """Ownership and dependency boundary for one repository."""

    schema_version: Literal["repo-responsibility.v1"] = "repo-responsibility.v1"
    repository: str
    responsibilities: list[str] = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    pull_request_order: int = Field(default=0, ge=0)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if not _REPOSITORY_RE.fullmatch(value):
            raise ValueError("repository must use owner/name form")
        return value

    @field_validator("depends_on")
    @classmethod
    def validate_dependencies(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(values))
        if any(not _REPOSITORY_RE.fullmatch(value) for value in normalized):
            raise ValueError("repository dependencies must use owner/name form")
        return normalized


class IssueTask(StrictModel):
    """Locked execution scope created after independent triage."""

    schema_version: Literal["issue-task.v2"] = "issue-task.v2"
    task_id: str
    # Read compatibility for tasks created before polling replaced Campaign.
    campaign_id: str | None = None
    issue_urls: list[str] = Field(min_length=1)
    named_baseline: str = Field(min_length=1)
    goals: list[GoalPath] = Field(min_length=1)
    repositories: list[RepositoryResponsibility] = Field(min_length=1)
    non_goals: list[str] = Field(default_factory=list)
    must_preserve: list[str] = Field(default_factory=list)
    target_hardware: TargetHardware
    permissions: PermissionBudget
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    triage_decision: TriageDecision
    triage_evidence_refs: list[str] = Field(default_factory=list)
    required_ci_checks: list[str] = Field(default_factory=list)
    locked_at: datetime = Field(default_factory=utc_now)
    revision: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("task_id contains unsupported characters")
        return value

    @field_validator("issue_urls")
    @classmethod
    def validate_issue_urls(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(values))
        for value in normalized:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError(
                    "issue URLs must be credential-free HTTPS URLs"
                )
        return normalized

    @field_validator("required_ci_checks")
    @classmethod
    def validate_required_ci_checks(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values))
        if any(not value for value in normalized):
            raise ValueError("required CI check names cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_locked_scope(self) -> "IssueTask":
        if self.triage_decision is not TriageDecision.APPROVE:
            raise ValueError("only independently approved issues can become tasks")
        if not self.triage_evidence_refs:
            raise ValueError("approved tasks require triage evidence")

        repository_names = [item.repository for item in self.repositories]
        if len(repository_names) != len(set(repository_names)):
            raise ValueError("repository responsibilities must be unique")
        known_repositories = set(repository_names)

        goal_ids = [goal.path_id for goal in self.goals]
        if len(goal_ids) != len(set(goal_ids)):
            raise ValueError("goal path ids must be unique")
        unknown_goal_repositories = {
            goal.repository
            for goal in self.goals
            if goal.repository not in known_repositories
        }
        if unknown_goal_repositories:
            raise ValueError(
                "goal paths reference repositories without responsibilities: "
                + ", ".join(sorted(unknown_goal_repositories))
            )

        for responsibility in self.repositories:
            unknown_dependencies = set(responsibility.depends_on) - known_repositories
            if unknown_dependencies:
                raise ValueError(
                    f"{responsibility.repository} depends on unknown repositories: "
                    + ", ".join(sorted(unknown_dependencies))
                )
        return self


class CapabilityDefinition(StrictModel):
    """A capability Hermes may invoke in any evidence-supported order."""

    capability_id: str
    description: str
    consumes: list[str] = Field(default_factory=list)
    produces: list[str] = Field(default_factory=list)
    required_role: ProjectRole

    @field_validator("capability_id")
    @classmethod
    def validate_capability_id(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("capability_id contains unsupported characters")
        return value


class CapabilityMatrix(StrictModel):
    """Capability registry without workflow-order semantics."""

    schema_version: Literal["capability-matrix.v1"] = "capability-matrix.v1"
    capabilities: list[CapabilityDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_capabilities(self) -> "CapabilityMatrix":
        identifiers = [item.capability_id for item in self.capabilities]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("capability ids must be unique")
        return self


class GoalRevision(StrictModel):
    """Explicit proposal to change a locked task scope."""

    schema_version: Literal["goal-revision.v1"] = "goal-revision.v1"
    revision_id: str
    task_id: str
    base_revision: int = Field(ge=0)
    rationale: str = Field(min_length=1)
    proposed_changes: dict[str, Any]
    decision: RevisionDecision = RevisionDecision.PENDING
    requested_by: ProjectRole
    decided_by: str | None = None
    decided_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("revision_id", "task_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("identifier contains unsupported characters")
        return value

    @model_validator(mode="after")
    def decision_has_provenance(self) -> "GoalRevision":
        if self.decision is RevisionDecision.PENDING:
            if self.decided_by is not None or self.decided_at is not None:
                raise ValueError("pending revisions cannot contain decision provenance")
        elif not self.decided_by or self.decided_at is None:
            raise ValueError("decided revisions require decided_by and decided_at")
        return self
