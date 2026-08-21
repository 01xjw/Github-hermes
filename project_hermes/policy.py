"""Fail-closed action authorization for the outer control loop."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from project_hermes.models import IssueTask, ProjectRole, StrictModel
from project_hermes.work_graph import ActionKind, ActionRequest


class PolicyDecision(StrictModel):
    """Auditable authorization outcome."""

    schema_version: Literal["policy-decision.v1"] = "policy-decision.v1"
    allowed: bool
    reason: str
    policy: str = "project-hermes.default.v1"


class PolicyContext(StrictModel):
    """State required to authorize one request."""

    task: IssueTask
    repository_roots: dict[str, Path] = Field(default_factory=dict)
    worktree_roots: dict[str, Path] = Field(default_factory=dict)
    approved_goal_revision_ids: set[str] = Field(default_factory=set)
    approved_repository_expansions: dict[str, set[str]] = Field(
        default_factory=dict
    )
    knowledge_commit_verified: bool = False
    operator_publish_approved: bool = False
    actor_session_id: str | None = None


_ROLE_ACTIONS: dict[ProjectRole, frozenset[ActionKind]] = {
    ProjectRole.CONTROL_PLANE: frozenset(ActionKind),
    ProjectRole.PROJECT_HERMES: frozenset(
        {
            ActionKind.READ_REPOSITORY,
            ActionKind.CREATE_NODE,
            ActionKind.REQUEST_EXECUTION,
            ActionKind.READ_EVIDENCE,
            ActionKind.REQUEST_REVIEW,
            ActionKind.REQUEST_APPROVAL,
        }
    ),
    ProjectRole.CODEX: frozenset(
        {
            ActionKind.READ_REPOSITORY,
            ActionKind.WRITE_WORKTREE,
            ActionKind.REQUEST_EXECUTION,
            ActionKind.READ_EVIDENCE,
            ActionKind.SUBMIT_EVIDENCE,
            ActionKind.REQUEST_REVIEW,
            ActionKind.RESOLVE_FINDING,
        }
    ),
    ProjectRole.RUNNER: frozenset(
        {
            ActionKind.READ_REPOSITORY,
            ActionKind.READ_EVIDENCE,
            ActionKind.SUBMIT_EVIDENCE,
        }
    ),
    ProjectRole.REVIEWER: frozenset(
        {
            ActionKind.READ_REPOSITORY,
            ActionKind.READ_EVIDENCE,
            ActionKind.SUBMIT_EVIDENCE,
            ActionKind.SUBMIT_REVIEW,
        }
    ),
    ProjectRole.CURATOR: frozenset(
        {
            ActionKind.READ_EVIDENCE,
            ActionKind.COMMIT_KNOWLEDGE,
            ActionKind.CLEANUP_TASK,
        }
    ),
    ProjectRole.OPERATOR: frozenset(
        {
            ActionKind.READ_REPOSITORY,
            ActionKind.READ_EVIDENCE,
            ActionKind.REQUEST_APPROVAL,
            ActionKind.PUBLISH_PULL_REQUEST,
        }
    ),
}


class PolicyEngine:
    """Authorize role, repository, path, and terminal-operation boundaries."""

    def authorize(
        self, request: ActionRequest, context: PolicyContext
    ) -> PolicyDecision:
        """Evaluate one action request without side effects."""

        if request.task_id != context.task.task_id:
            return self._deny("request belongs to a different task")

        allowed_actions = _ROLE_ACTIONS.get(request.requested_by, frozenset())
        if request.action not in allowed_actions:
            return self._deny(
                f"role {request.requested_by.value} cannot perform "
                f"{request.action.value}"
            )

        repository_decision = self._authorize_repository(request, context)
        if repository_decision is not None:
            return repository_decision

        if (
            request.action is ActionKind.READ_REPOSITORY
            and request.path is not None
        ):
            path_decision = self._authorize_repository_path(request, context)
            if path_decision is not None:
                return path_decision

        if request.action is ActionKind.WRITE_WORKTREE:
            path_decision = self._authorize_worktree_path(request, context)
            if path_decision is not None:
                return path_decision

        permissions = context.task.permissions
        if (
            request.action is ActionKind.REQUEST_EXECUTION
            and not permissions.may_request_execution
        ):
            return self._deny("task permission budget forbids execution")
        if (
            request.action is ActionKind.REQUEST_REVIEW
            and not permissions.may_request_review
        ):
            return self._deny("task permission budget forbids review requests")
        if (
            bool(request.payload.get("network_access"))
            and not permissions.may_access_network
        ):
            return self._deny("task permission budget forbids network access")

        if (
            request.action is ActionKind.PUBLISH_PULL_REQUEST
            and not permissions.may_publish
        ):
            return self._deny("task permission budget forbids publication")
        if (
            request.action is ActionKind.PUBLISH_PULL_REQUEST
            and not context.operator_publish_approved
        ):
            return self._deny("pull request publication requires operator approval")

        if request.action is ActionKind.CLEANUP_TASK:
            if not context.knowledge_commit_verified:
                return self._deny(
                    "task cleanup requires a verified knowledge commit"
                )
            if not request.payload.get("knowledge_commit_id"):
                return self._deny(
                    "task cleanup must name the verified knowledge commit"
                )

        if request.goal_revision_id is not None:
            if request.goal_revision_id not in context.approved_goal_revision_ids:
                return self._deny("goal revision is not approved")

        return PolicyDecision(allowed=True, reason="request satisfies policy")

    def require(
        self, request: ActionRequest, context: PolicyContext
    ) -> PolicyDecision:
        """Authorize a request or raise ``PermissionError``."""

        decision = self.authorize(request, context)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        return decision

    @staticmethod
    def _authorize_repository(
        request: ActionRequest,
        context: PolicyContext,
    ) -> PolicyDecision | None:
        repository_actions = {
            ActionKind.READ_REPOSITORY,
            ActionKind.WRITE_WORKTREE,
            ActionKind.REQUEST_EXECUTION,
            ActionKind.REQUEST_REVIEW,
            ActionKind.SUBMIT_EVIDENCE,
        }
        if request.action not in repository_actions:
            return None
        if request.repository is None:
            return PolicyEngine._deny("repository is required for this action")

        permissions = context.task.permissions
        if request.repository not in permissions.readable_repositories:
            if request.goal_revision_id is None:
                return PolicyEngine._deny(
                    "repository is outside the locked readable scope"
                )
            approved_expansions = context.approved_repository_expansions.get(
                request.goal_revision_id,
                set(),
            )
            if request.repository not in approved_expansions:
                return PolicyEngine._deny(
                    "approved goal revision does not authorize this repository"
                )

        if (
            request.action is ActionKind.WRITE_WORKTREE
            and request.repository not in permissions.writable_repositories
        ):
            return PolicyEngine._deny(
                "repository is outside the locked writable scope"
            )
        return None

    @staticmethod
    def _authorize_repository_path(
        request: ActionRequest,
        context: PolicyContext,
    ) -> PolicyDecision | None:
        if request.repository is None or request.path is None:
            return PolicyEngine._deny(
                "repository reads require repository and path"
            )
        root = context.repository_roots.get(request.repository)
        if root is None:
            return PolicyEngine._deny(
                "no controller repository root exists for this repository"
            )

        candidate = Path(request.path)
        if not candidate.is_absolute():
            return PolicyEngine._deny("repository read paths must be absolute")
        if not candidate.resolve(strict=False).is_relative_to(root.resolve()):
            return PolicyEngine._deny("read path escapes the repository root")
        return None

    @staticmethod
    def _authorize_worktree_path(
        request: ActionRequest,
        context: PolicyContext,
    ) -> PolicyDecision | None:
        if request.repository is None or request.path is None:
            return PolicyEngine._deny(
                "worktree writes require repository and path"
            )
        root = context.worktree_roots.get(request.repository)
        if root is None:
            return PolicyEngine._deny(
                "no exclusive worktree lease exists for the repository"
            )

        candidate = Path(request.path)
        if not candidate.is_absolute():
            return PolicyEngine._deny("worktree write paths must be absolute")
        root_resolved = root.resolve()
        candidate_resolved = candidate.resolve(strict=False)
        if not candidate_resolved.is_relative_to(root_resolved):
            return PolicyEngine._deny(
                "write path escapes the leased worktree"
            )
        return None

    @staticmethod
    def _deny(reason: str) -> PolicyDecision:
        return PolicyDecision(allowed=False, reason=reason)
