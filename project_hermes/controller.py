"""Event-driven outer-loop coordinator."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Literal, Mapping

from pydantic import Field

from project_hermes.assurance import (
    CompletionEntry,
    CompletionLayer,
    CompletionMatrix,
    CompletionStatus,
    EvidenceRecord,
    GoalPathCompletion,
    MinimalDiffVerdict,
    ReviewEscalationState,
    ReviewEscalationStatus,
    ReviewProgress,
    ReviewRecord,
    ReviewRole,
    ReviewVerdict,
)
from project_hermes.assurance_store import (
    AssuranceStore,
    CandidateAssuranceStore,
)
from project_hermes.models import (
    GoalRevision,
    IssueTask,
    ProjectRole,
    RevisionDecision,
    StrictModel,
)
from project_hermes.policy import PolicyContext, PolicyDecision, PolicyEngine
from project_hermes.publication import (
    CandidateLock,
    CandidateValidation,
    PullRequestCandidate,
)
from project_hermes.redaction import redact_data, redact_text
from project_hermes.review_resolution import ReviewResolution
from project_hermes.store import RunStore
from project_hermes.work_graph import (
    ActionKind,
    ActionRequest,
    LifecycleStatus,
    PipelineRun,
    WorkNode,
    WorkNodeKind,
)


class CandidateGateResult(StrictModel):
    """Combined completion and mandatory-review gate."""

    schema_version: Literal["candidate-gate-result.v3"] = (
        "candidate-gate-result.v3"
    )
    task_id: str
    code_diff_sha: str
    goal_revision: int
    repository_bases: dict[str, str] = Field(default_factory=dict)
    repository_diff_shas: dict[str, str] = Field(default_factory=dict)
    approved: bool
    reasons: list[str] = Field(default_factory=list)


class ReviewEscalationAction(StrEnum):
    """Controller action derived from ordered completed review cycles."""

    NONE = "none"
    REQUEST_MODEL_ESCALATION = "request_model_escalation"
    BLOCK_OPERATOR_ATTENTION = "block_operator_attention"


class ReviewEscalationDecision(StrictModel):
    """Pure result of review-progress escalation evaluation."""

    schema_version: Literal["review-escalation-decision.v1"] = (
        "review-escalation-decision.v1"
    )
    action: ReviewEscalationAction = ReviewEscalationAction.NONE
    consecutive_no_progress: int = Field(default=0, ge=0)
    review_cycle: int | None = Field(default=None, ge=1)
    review_ids: list[str] = Field(default_factory=list)
    reviewer_explanation: str | None = None


ActionHandler = Callable[[IssueTask, ActionRequest, PolicyContext], Any]
EscalationHandler = Callable[
    [str, IssueTask, ReviewEscalationDecision],
    Any,
]


def evaluate_review_escalation(
    reviews: list[ReviewRecord],
    state: ReviewEscalationState,
) -> ReviewEscalationDecision:
    """Evaluate completed, fresh review cycles without side effects."""

    if state.status is ReviewEscalationStatus.BLOCKED:
        return ReviewEscalationDecision()

    by_cycle: dict[int, list[ReviewRecord]] = {}
    for review in reviews:
        if (
            review.task_id != state.task_id
            or review.goal_revision != state.goal_revision
            or review.review_cycle is None
        ):
            continue
        by_cycle.setdefault(review.review_cycle, []).append(review)

    completed: list[tuple[int, dict[ReviewRole, ReviewRecord]]] = []
    for cycle, records in sorted(by_cycle.items()):
        roles: dict[ReviewRole, ReviewRecord] = {}
        for record in records:
            if record.role in roles:
                roles = {}
                break
            roles[record.role] = record
        if set(roles) != set(ReviewRole):
            continue
        if len({record.code_diff_sha for record in roles.values()}) != 1:
            continue
        if len({record.packet_digest for record in roles.values()}) != 1:
            continue
        if any(
            record.verdict is ReviewVerdict.MORE_EVIDENCE_REQUIRED
            for record in roles.values()
        ):
            continue
        completed.append((cycle, roles))

    if state.status is ReviewEscalationStatus.REQUESTED:
        for cycle, roles in completed:
            if cycle <= (state.trigger_review_cycle or 0):
                continue
            if all(
                record.verdict is ReviewVerdict.APPROVE
                for record in roles.values()
            ):
                return ReviewEscalationDecision()
            auditor = roles[ReviewRole.COMPLETION_AUDITOR]
            return ReviewEscalationDecision(
                action=ReviewEscalationAction.BLOCK_OPERATOR_ATTENTION,
                review_cycle=cycle,
                review_ids=[
                    record.review_id for record in roles.values()
                ],
                reviewer_explanation=(
                    auditor.progress_explanation or auditor.summary
                ),
            )
        return ReviewEscalationDecision()

    streak: list[tuple[int, dict[ReviewRole, ReviewRecord]]] = []
    for cycle, roles in completed:
        auditor = roles[ReviewRole.COMPLETION_AUDITOR]
        if all(
            record.verdict is ReviewVerdict.APPROVE
            for record in roles.values()
        ) or auditor.progress is ReviewProgress.IMPROVED:
            streak = []
            continue
        if auditor.progress not in {
            ReviewProgress.NO_PROGRESS,
            ReviewProgress.REGRESSED,
        }:
            continue
        streak.append((cycle, roles))
        if len(streak) > 2:
            streak.pop(0)
        if len(streak) == 2:
            latest_cycle, latest_roles = streak[-1]
            return ReviewEscalationDecision(
                action=ReviewEscalationAction.REQUEST_MODEL_ESCALATION,
                consecutive_no_progress=2,
                review_cycle=latest_cycle,
                review_ids=[
                    record.review_id
                    for _, cycle_roles in streak
                    for record in cycle_roles.values()
                ],
                reviewer_explanation=latest_roles[
                    ReviewRole.COMPLETION_AUDITOR
                ].progress_explanation,
            )
    return ReviewEscalationDecision(
        consecutive_no_progress=len(streak),
    )


class ProjectHermesController:
    """Policy-check agent requests and update durable projections."""

    def __init__(
        self,
        run_store: RunStore,
        assurance_store: AssuranceStore,
        *,
        candidate_assurance_store: CandidateAssuranceStore | None = None,
        policy: PolicyEngine | None = None,
        action_handlers: Mapping[ActionKind, ActionHandler] | None = None,
        escalation_handler: EscalationHandler | None = None,
    ) -> None:
        self.run_store = run_store
        self.assurance_store = assurance_store
        self.candidate_assurance_store = candidate_assurance_store
        self.policy = policy or PolicyEngine()
        self._action_handlers = dict(action_handlers or {})
        self._escalation_handler = escalation_handler
        self._lock = RLock()

    def register_action_handler(
        self,
        action: ActionKind,
        handler: ActionHandler,
        *,
        replace: bool = False,
    ) -> None:
        """Register one outer-loop capability implementation."""

        if action in {
            ActionKind.CREATE_NODE,
            ActionKind.SUBMIT_EVIDENCE,
            ActionKind.SUBMIT_REVIEW,
            ActionKind.RESOLVE_FINDING,
        }:
            raise ValueError(f"{action.value} has a built-in handler")
        with self._lock:
            if action in self._action_handlers and not replace:
                raise ValueError(
                    f"action handler is already registered: {action.value}"
                )
            self._action_handlers[action] = handler

    def create_task_run(
        self,
        task: IssueTask,
        *,
        run_id: str | None = None,
    ) -> PipelineRun:
        """Persist a locked task and initialize its completion matrix."""

        task_payload = task.model_dump(mode="json")
        if redact_data(task_payload) != task_payload:
            raise ValueError("task contract contains credential-shaped data")
        goal_paths: dict[str, GoalPathCompletion] = {}
        for goal in task.goals:
            try:
                required_layers = [
                    CompletionLayer(value)
                    for value in goal.required_completion_layers
                ]
            except ValueError as exc:
                raise ValueError(
                    f"goal path {goal.path_id} names an unknown "
                    "completion layer"
                ) from exc
            goal_paths[goal.path_id] = GoalPathCompletion(
                path_id=goal.path_id,
                repository=goal.repository,
                required_layers=required_layers,
            )
        matrix = CompletionMatrix(
            task_id=task.task_id,
            required_layers=[],
            goal_paths=goal_paths,
        )
        run = self.run_store.create_run(task, run_id=run_id)
        self.assurance_store.put_completion_matrix(
            matrix
        )
        return run

    def submit_action(
        self,
        run_id: str,
        request: ActionRequest,
        *,
        repository_roots: dict[str, Path] | None = None,
        worktree_roots: dict[str, Path] | None = None,
        approved_revisions: list[GoalRevision] | None = None,
        knowledge_commit_verified: bool = False,
        operator_publish_approved: bool = False,
        actor_session_id: str | None = None,
    ) -> tuple[PolicyDecision, Any | None]:
        """Authorize and apply one untrusted agent request."""

        task = self.run_store.get_task(run_id)
        if (
            actor_session_id is not None
            and request.requested_by is not ProjectRole.CONTROL_PLANE
            and request.node_id is not None
            and request.action is not ActionKind.CREATE_NODE
        ):
            try:
                node = self.run_store.load_graph(run_id).nodes[
                    request.node_id
                ]
            except KeyError:
                return self._deny_action(
                    run_id,
                    request,
                    "action references an unknown work node",
                )
            if node.owner_token != actor_session_id:
                return self._deny_action(
                    run_id,
                    request,
                    "authenticated actor does not own the work node lease",
                )
        approved = [
            revision
            for revision in approved_revisions or []
            if revision.decision is RevisionDecision.APPROVED
        ]
        approved_revision_ids = {
            revision.revision_id for revision in approved
        }
        context = PolicyContext(
            task=task,
            repository_roots=repository_roots or {},
            worktree_roots=worktree_roots or {},
            approved_goal_revision_ids=approved_revision_ids,
            approved_repository_expansions={
                revision.revision_id: _repository_expansions(revision)
                for revision in approved
            },
            knowledge_commit_verified=knowledge_commit_verified,
            operator_publish_approved=operator_publish_approved,
            actor_session_id=actor_session_id,
        )
        decision = self.policy.authorize(request, context)
        self.run_store.record_event(
            run_id,
            "action.authorized" if decision.allowed else "action.denied",
            {
                "request_id": request.request_id,
                "requested_by": request.requested_by.value,
                "action": request.action.value,
                "reason": decision.reason,
            },
            node_id=request.node_id,
        )
        if not decision.allowed:
            return decision, None

        result: Any | None = None
        if request.action is ActionKind.CREATE_NODE:
            node = WorkNode.model_validate(request.payload)
            if node.run_id != run_id:
                raise ValueError("work node belongs to another run")
            if (
                node.status is not LifecycleStatus.DISCOVERED
                or node.owner_token is not None
                or node.lease_expires_at is not None
                or node.output
            ):
                raise ValueError(
                    "new work nodes must start unowned and discovered"
                )
            allowed_roles = {
                ProjectRole.PROJECT_HERMES: {
                    ProjectRole.CODEX,
                    ProjectRole.RUNNER,
                    ProjectRole.REVIEWER,
                    ProjectRole.CURATOR,
                },
            }.get(request.requested_by)
            if (
                allowed_roles is not None
                and node.requested_role not in allowed_roles
            ):
                raise PermissionError(
                    "requester cannot assign the work node role"
                )
            result = self.run_store.add_node(node)
        elif request.action is ActionKind.SUBMIT_EVIDENCE:
            evidence = EvidenceRecord.model_validate(request.payload)
            if evidence.task_id != task.task_id:
                raise ValueError("evidence belongs to a different task")
            if evidence.producer_role is not request.requested_by:
                raise ValueError(
                    "evidence producer role differs from the requester"
                )
            if evidence.node_id != request.node_id:
                raise ValueError("evidence belongs to another work node")
            if evidence.repository != request.repository:
                raise ValueError("evidence repository differs from the action")
            goals = {goal.path_id: goal for goal in task.goals}
            goal = goals.get(evidence.path_id)
            if goal is None or goal.repository != evidence.repository:
                raise ValueError(
                    "evidence is outside the locked goal responsibility"
                )
            if evidence.goal_revision != task.revision:
                raise ValueError("evidence uses a stale goal revision")
            evidence_payload = evidence.model_dump(
                mode="json",
                exclude_computed_fields=True,
            )
            if redact_data(evidence_payload) != evidence_payload:
                raise ValueError(
                    "evidence contains credential-shaped data"
                )
            result = self.assurance_store.put_evidence(evidence)
        elif request.action is ActionKind.SUBMIT_REVIEW:
            review = ReviewRecord.model_validate(request.payload)
            if review.task_id != task.task_id:
                raise ValueError("review belongs to a different task")
            if review.goal_revision != task.revision:
                raise ValueError("review uses a stale goal revision")
            if request.node_id is None:
                raise ValueError("review must belong to a review work node")
            review_node = self.run_store.load_graph(run_id).nodes[
                request.node_id
            ]
            if (
                review_node.kind is not WorkNodeKind.REVIEW
                or review_node.requested_role is not ProjectRole.REVIEWER
                or review_node.payload.get("review_role")
                != review.role.value
            ):
                raise ValueError(
                    "review role differs from the assigned review node"
                )
            if (
                actor_session_id is None
                or review.reviewer_session_id != actor_session_id
            ):
                raise ValueError(
                    "reviewer session differs from the authenticated actor"
                )
            packet = self.assurance_store.get_review_packet(review.packet_id)
            if (
                packet.task_id != task.task_id
                or packet.goal_revision != task.revision
                or packet.code_diff_sha != review.code_diff_sha
                or packet.content_hash != review.packet_digest
                or not set(review.evidence_ids).issubset(packet.evidence_ids)
            ):
                raise ValueError(
                    "review does not match its frozen packet, candidate "
                    "diff, goal revision, or evidence"
                )
            if review.role is ReviewRole.COMPLETION_AUDITOR:
                expected_goal_paths = {goal.path_id for goal in task.goals}
                if set(review.assessed_goal_path_ids) != expected_goal_paths:
                    raise ValueError(
                        "completion-auditor progress must assess every "
                        "frozen issue goal path"
                    )
            review_payload = review.model_dump(mode="json")
            if redact_data(review_payload) != review_payload:
                raise ValueError(
                    "review contains credential-shaped data"
                )
            result = self.assurance_store.add_review(review)
            self._record_review_escalation(run_id, task)
        elif request.action is ActionKind.RESOLVE_FINDING:
            resolution = ReviewResolution.model_validate(request.payload)
            if resolution.task_id != task.task_id:
                raise ValueError("review resolution belongs to another task")
            if resolution.goal_revision != task.revision:
                raise ValueError("review resolution uses a stale goal revision")
            if (
                actor_session_id is None
                or resolution.resolved_by_session_id != actor_session_id
            ):
                raise ValueError(
                    "resolution actor differs from the authenticated session"
                )
            resolution_payload = resolution.model_dump(mode="json")
            if redact_data(resolution_payload) != resolution_payload:
                raise ValueError(
                    "review resolution contains credential-shaped data"
                )
            result = self.assurance_store.add_review_resolution(resolution)
        else:
            with self._lock:
                handler = self._action_handlers.get(request.action)
            if handler is None:
                denied = PolicyDecision(
                    allowed=False,
                    reason=(
                        f"no controller handler is registered for "
                        f"{request.action.value}"
                    ),
                    policy="project-hermes.execution.v1",
                )
                self.run_store.record_event(
                    run_id,
                    "action.unhandled",
                    {
                        "request_id": request.request_id,
                        "action": request.action.value,
                        "reason": denied.reason,
                    },
                    node_id=request.node_id,
                )
                return denied, None
            try:
                result = handler(task, request, context)
            except Exception as exc:
                self.run_store.record_event(
                    run_id,
                    "action.failed",
                    {
                        "request_id": request.request_id,
                        "action": request.action.value,
                        "error": redact_text(str(exc)),
                    },
                    node_id=request.node_id,
                )
                raise
        self.run_store.record_event(
            run_id,
            "action.applied",
            {
                "request_id": request.request_id,
                "action": request.action.value,
            },
            node_id=request.node_id,
        )
        return decision, result

    def _record_review_escalation(
        self,
        run_id: str,
        task: IssueTask,
    ) -> None:
        reviews = self.assurance_store.review_history(
            task.task_id,
            goal_revision=task.revision,
        )
        state = self.assurance_store.get_review_escalation(
            task.task_id,
            goal_revision=task.revision,
        )
        decision = evaluate_review_escalation(reviews, state)
        if decision.action is ReviewEscalationAction.NONE:
            return

        explanation = decision.reviewer_explanation or (
            "The completion auditor did not provide an escalation explanation."
        )
        status = (
            ReviewEscalationStatus.REQUESTED
            if decision.action
            is ReviewEscalationAction.REQUEST_MODEL_ESCALATION
            else ReviewEscalationStatus.BLOCKED
        )
        updated = ReviewEscalationState(
            task_id=task.task_id,
            goal_revision=task.revision,
            status=status,
            trigger_review_cycle=decision.review_cycle,
            trigger_review_ids=decision.review_ids,
            reviewer_explanation=explanation,
        )
        self.assurance_store.put_review_escalation(updated)

        if status is ReviewEscalationStatus.BLOCKED:
            self.run_store.record_event(
                run_id,
                "review.operator_blocked",
                {
                    "goal_revision": task.revision,
                    "review_cycle": decision.review_cycle,
                    "review_ids": decision.review_ids,
                    "reviewer_explanation": explanation,
                    "reason": (
                        "the escalated Codex model did not pass its next "
                        "completed review cycle"
                    ),
                },
            )
            self.run_store.complete_run(
                run_id,
                status=LifecycleStatus.BLOCKED,
            )
            return

        self.run_store.record_event(
            run_id,
            "review.model_escalation_requested",
            {
                "goal_revision": task.revision,
                "review_cycle": decision.review_cycle,
                "review_ids": decision.review_ids,
                "target_profile": "codex-escalated",
                "reviewer_explanation": explanation,
            },
        )
        if self._escalation_handler is None:
            return
        try:
            self._escalation_handler(run_id, task, decision)
        except Exception as exc:
            failure = redact_text(str(exc))
            blocked = updated.model_copy(
                update={"status": ReviewEscalationStatus.BLOCKED}
            )
            self.assurance_store.put_review_escalation(blocked)
            self.run_store.record_event(
                run_id,
                "review.operator_blocked",
                {
                    "goal_revision": task.revision,
                    "review_cycle": decision.review_cycle,
                    "review_ids": decision.review_ids,
                    "reviewer_explanation": explanation,
                    "reason": "automatic Codex reprovisioning failed",
                    "error": failure,
                },
            )
            self.run_store.complete_run(
                run_id,
                status=LifecycleStatus.BLOCKED,
            )
            return
        self.run_store.record_event(
            run_id,
            "review.model_escalated",
            {
                "goal_revision": task.revision,
                "review_cycle": decision.review_cycle,
                "target_profile": "codex-escalated",
            },
        )

    def _deny_action(
        self,
        run_id: str,
        request: ActionRequest,
        reason: str,
    ) -> tuple[PolicyDecision, None]:
        decision = PolicyDecision(allowed=False, reason=reason)
        self.run_store.record_event(
            run_id,
            "action.denied",
            {
                "request_id": request.request_id,
                "requested_by": request.requested_by.value,
                "action": request.action.value,
                "reason": reason,
            },
            node_id=request.node_id,
        )
        return decision, None

    def record_completion(
        self,
        task_id: str,
        entry: CompletionEntry,
        *,
        path_id: str | None = None,
    ) -> CompletionMatrix:
        """Record a completion result only after verifying every evidence ref."""

        matrix = self.assurance_store.get_completion_matrix(task_id)
        repository: str | None = None
        if matrix.goal_paths:
            if path_id is None or path_id not in matrix.goal_paths:
                raise ValueError(
                    "path_id must name a locked completion goal"
                )
            repository = matrix.goal_paths[path_id].repository
        if entry.status is CompletionStatus.SATISFIED:
            for evidence_id in entry.evidence_ids:
                evidence = self.assurance_store.get_evidence(evidence_id)
                if (
                    evidence.task_id != task_id
                    or evidence.completion_layer is not entry.layer
                    or evidence.code_diff_sha != entry.code_diff_sha
                    or evidence.goal_revision != entry.goal_revision
                    or (
                        path_id is not None
                        and evidence.path_id != path_id
                    )
                    or (
                        repository is not None
                        and evidence.repository != repository
                    )
                ):
                    raise ValueError(
                        f"evidence cannot satisfy this completion entry: "
                        f"{evidence_id}"
                    )
        matrix.update(entry, path_id=path_id)
        return self.assurance_store.put_completion_matrix(matrix)

    def candidate_gate(
        self,
        task_id: str,
        *,
        code_diff_sha: str,
        goal_revision: int,
        repository_bases: dict[str, str] | None = None,
        implementer_session_id: str | None = None,
        complete_run: bool = True,
    ) -> CandidateGateResult:
        """Require fresh completion evidence and two independent approvals."""

        reasons: list[str] = []
        repository_diff_shas: dict[str, str] = {}
        repository_bases = repository_bases or {}
        matrix = self.assurance_store.get_completion_matrix(task_id)
        required_repositories: set[str] = set()
        if matrix.goal_paths:
            required_repositories = {
                path.repository for path in matrix.goal_paths.values()
            }
            missing_bases = required_repositories - set(repository_bases)
            if missing_bases:
                reasons.append(
                    "candidate is missing repository base commits: "
                    + ", ".join(sorted(missing_bases))
                )
        if not matrix.can_close(
            code_diff_sha=code_diff_sha,
            goal_revision=goal_revision,
        ):
            reasons.append(
                "the completion matrix is incomplete, stale, or not satisfied"
            )
        reasons.extend(
            self._validate_completion_evidence(
                matrix,
                task_id=task_id,
                code_diff_sha=code_diff_sha,
                goal_revision=goal_revision,
                repository_bases=repository_bases,
            )
        )

        gate = self.assurance_store.review_gate(
            task_id,
            code_diff_sha=code_diff_sha,
            goal_revision=goal_revision,
        )
        review_ok, review_reasons = gate.evaluate()
        if not review_ok:
            reasons.extend(review_reasons)
        completion_evidence_ids = {
            evidence_id
            for _, _, entry in matrix.evidence_bindings()
            for evidence_id in entry.evidence_ids
        }
        for review in gate.reviews:
            try:
                packet = self.assurance_store.get_review_packet(
                    review.packet_id
                )
            except KeyError:
                reasons.append(
                    f"review packet does not exist: {review.packet_id}"
                )
            else:
                if (
                    packet.content_hash != review.packet_digest
                    or packet.completion_matrix_digest
                    != matrix.fingerprint()
                    or set(packet.repository_inputs)
                    != required_repositories
                    or (
                        packet.repository in repository_bases
                        and packet.base_sha
                        != repository_bases[packet.repository]
                    )
                ):
                    reasons.append(
                        f"review packet is stale or invalid: "
                        f"{review.packet_id}"
                    )
                if not completion_evidence_ids.issubset(
                    packet.evidence_ids
                ):
                    reasons.append(
                        "review packet omits completion evidence: "
                        f"{review.packet_id}"
                    )
                for repository, review_input in (
                    packet.repository_inputs.items()
                ):
                    existing_diff = repository_diff_shas.get(repository)
                    if (
                        existing_diff is not None
                        and existing_diff != review_input.code_diff_sha
                    ):
                        reasons.append(
                            "review packets disagree on repository diff: "
                            f"{repository}"
                        )
                    repository_diff_shas[repository] = (
                        review_input.code_diff_sha
                    )
                    if (
                        repository in repository_bases
                        and review_input.base_sha
                        != repository_bases[repository]
                    ):
                        reasons.append(
                            "review packet has a stale repository base: "
                            f"{review.packet_id}:{repository}"
                        )
                for evidence_id in packet.evidence_ids:
                    try:
                        packet_evidence = (
                            self.assurance_store.get_evidence(evidence_id)
                        )
                    except KeyError:
                        reasons.append(
                            "review packet evidence does not exist: "
                            f"{evidence_id}"
                        )
                        continue
                    review_input = packet.repository_inputs.get(
                        packet_evidence.repository
                    )
                    if (
                        packet_evidence.task_id != task_id
                        or packet_evidence.code_diff_sha != code_diff_sha
                        or packet_evidence.goal_revision != goal_revision
                        or review_input is None
                        or packet_evidence.base_sha
                        != review_input.base_sha
                    ):
                        reasons.append(
                            "review packet evidence is stale or invalid: "
                            f"{evidence_id}"
                        )
            for evidence_id in review.evidence_ids:
                try:
                    evidence = self.assurance_store.get_evidence(evidence_id)
                except KeyError:
                    reasons.append(
                        f"review evidence does not exist: {evidence_id}"
                    )
                    continue
                if (
                    evidence.task_id != task_id
                    or evidence.code_diff_sha != code_diff_sha
                    or evidence.goal_revision != goal_revision
                    or evidence.producer_role is not ProjectRole.REVIEWER
                    or (
                        evidence.repository in repository_bases
                        and evidence.base_sha
                        != repository_bases[evidence.repository]
                    )
                ):
                    reasons.append(
                        f"review evidence is stale or invalid: {evidence_id}"
                    )

        if set(repository_diff_shas) != required_repositories:
            reasons.append(
                "review packet does not bind every repository diff"
            )

        reviewer_sessions = [
            review.reviewer_session_id for review in gate.reviews
        ]
        if len(reviewer_sessions) != len(set(reviewer_sessions)):
            reasons.append("mandatory review roles must use distinct sessions")
        if (
            implementer_session_id is not None
            and implementer_session_id in reviewer_sessions
        ):
            reasons.append("the implementer cannot approve its own candidate")

        result = CandidateGateResult(
            task_id=task_id,
            code_diff_sha=code_diff_sha,
            goal_revision=goal_revision,
            repository_bases=repository_bases,
            repository_diff_shas=repository_diff_shas,
            approved=not reasons,
            reasons=list(dict.fromkeys(reasons)),
        )
        if result.approved and complete_run:
            run = self.run_store.get_run_for_task(task_id)
            if run.goal_revision != goal_revision:
                raise ValueError(
                    "candidate gate goal revision differs from the task run"
                )
            self.run_store.complete_run(
                run.run_id,
                status=LifecycleStatus.COMPLETED,
            )
        return result

    def lock_candidate(
        self,
        candidate: PullRequestCandidate,
        validation: CandidateValidation,
        *,
        repository_bases: dict[str, str],
        implementer_session_id: str,
        lock_id: str | None = None,
    ) -> CandidateLock:
        """Persist the exact candidate only after every reveal gate passes."""

        store = self.candidate_assurance_store
        if store is None:
            raise RuntimeError("candidate assurance persistence is not configured")
        candidate_digest = candidate.fingerprint()
        if validation.candidate_digest != candidate_digest:
            raise PermissionError("validation belongs to another candidate digest")
        if (
            not validation.successful
            or not _evidence_reports_success(validation.results)
        ):
            raise PermissionError("candidate validation did not succeed")
        validation_payload = validation.model_dump(mode="json")
        if redact_data(validation_payload) != validation_payload:
            raise ValueError("candidate validation contains credential-shaped data")
        if candidate.minimal_diff_assessment_digest is None:
            raise PermissionError("candidate lacks a minimal-diff assessment")
        assessment = store.get_minimal_diff_assessment(
            candidate.minimal_diff_assessment_digest
        )
        if assessment.verdict is not MinimalDiffVerdict.APPROVE:
            raise PermissionError(
                "minimal-diff reviewer rejected avoidable candidate edits"
            )
        if (
            assessment.task_id != candidate.task_id
            or assessment.repository != candidate.repository
            or assessment.code_diff_sha != candidate.code_diff_sha
            or assessment.goal_revision != candidate.goal_revision
            or assessment.content_hash
            != candidate.minimal_diff_assessment_digest
            or [item.path for item in assessment.candidate_files]
            != candidate.files
        ):
            raise PermissionError(
                "candidate differs from its frozen minimal-diff assessment"
            )
        if assessment.reviewer_session_id == implementer_session_id:
            raise PermissionError(
                "the implementer cannot perform the minimal-diff assessment"
            )
        if candidate.checks is None or set(candidate.checks.evidence_ids) != set(
            assessment.correctness_evidence_ids
        ):
            raise PermissionError(
                "candidate Checks omit frozen correctness evidence"
            )
        if set(validation.evidence_ids) != set(
            assessment.correctness_evidence_ids
        ):
            raise PermissionError(
                "validation must bind every assessed correctness evidence record"
            )

        expected_overlay = assessment.local_test_overlay
        candidate_overlay = candidate.checks.local_test_overlay
        if expected_overlay != candidate_overlay:
            raise PermissionError(
                "candidate Checks differ from the local test overlay"
            )
        if expected_overlay is None:
            if validation.local_test_overlay_digest is not None:
                raise PermissionError(
                    "validation names an unexpected local test overlay"
                )
        else:
            overlay = store.get_test_overlay(expected_overlay.overlay_id)
            if (
                overlay.task_id != candidate.task_id
                or overlay.code_diff_sha != candidate.code_diff_sha
                or overlay.summary() != expected_overlay
                or validation.local_test_overlay_digest
                != expected_overlay.content_digest
            ):
                raise PermissionError(
                    "local test overlay evidence is stale or mismatched"
                )

        matrix = self.assurance_store.get_completion_matrix(candidate.task_id)
        completion_evidence_ids = {
            evidence_id
            for _, _, entry in matrix.evidence_bindings()
            for evidence_id in entry.evidence_ids
        }
        if not set(validation.evidence_ids).issubset(completion_evidence_ids):
            raise PermissionError(
                "validation evidence is outside the completion matrix"
            )
        for evidence_id in validation.evidence_ids:
            evidence = self.assurance_store.get_evidence(evidence_id)
            if (
                evidence.task_id != candidate.task_id
                or evidence.code_diff_sha != candidate.code_diff_sha
                or evidence.goal_revision != candidate.goal_revision
                or not _evidence_reports_success(evidence.result)
            ):
                raise PermissionError(
                    f"validation evidence is stale or unsuccessful: {evidence_id}"
                )

        gate_result = self.candidate_gate(
            candidate.task_id,
            code_diff_sha=candidate.code_diff_sha,
            goal_revision=candidate.goal_revision,
            repository_bases=repository_bases,
            implementer_session_id=implementer_session_id,
            complete_run=False,
        )
        if not gate_result.approved:
            raise PermissionError(
                "candidate assurance gate rejected lock: "
                + "; ".join(gate_result.reasons)
            )
        review_gate = self.assurance_store.review_gate(
            candidate.task_id,
            code_diff_sha=candidate.code_diff_sha,
            goal_revision=candidate.goal_revision,
        )
        reviews = {review.role: review for review in review_gate.reviews}
        minimal_review = reviews[ReviewRole.MINIMAL_DIFF_REVIEWER]
        if assessment.reviewer_session_id != minimal_review.reviewer_session_id:
            raise PermissionError(
                "minimal-diff assessment was not authored by its reviewer"
            )
        lock = CandidateLock(
            lock_id=lock_id or f"lock-{candidate.candidate_id}",
            task_id=candidate.task_id,
            candidate_id=candidate.candidate_id,
            candidate_digest=candidate_digest,
            code_diff_sha=candidate.code_diff_sha,
            goal_revision=candidate.goal_revision,
            minimal_diff_assessment_digest=assessment.content_hash,
            validation=validation,
            review_approvals={
                role: review.review_id for role, review in reviews.items()
            },
        )
        persisted = store.put_candidate_lock(lock)
        run = self.run_store.get_run_for_task(candidate.task_id)
        if run.goal_revision != candidate.goal_revision:
            raise ValueError(
                "candidate lock goal revision differs from the task run"
            )
        self.run_store.complete_run(
            run.run_id,
            status=LifecycleStatus.COMPLETED,
        )
        return persisted

    def _validate_completion_evidence(
        self,
        matrix: CompletionMatrix,
        *,
        task_id: str,
        code_diff_sha: str,
        goal_revision: int,
        repository_bases: dict[str, str],
    ) -> list[str]:
        reasons: list[str] = []
        for path_id, repository, entry in matrix.evidence_bindings():
            if not entry.evidence_ids:
                continue
            for evidence_id in entry.evidence_ids:
                try:
                    evidence = self.assurance_store.get_evidence(evidence_id)
                except KeyError:
                    reasons.append(
                        f"completion evidence does not exist: {evidence_id}"
                    )
                    continue
                if (
                    evidence.task_id != task_id
                    or evidence.completion_layer is not entry.layer
                    or evidence.code_diff_sha != code_diff_sha
                    or evidence.goal_revision != goal_revision
                    or (
                        evidence.repository in repository_bases
                        and evidence.base_sha
                        != repository_bases[evidence.repository]
                    )
                    or (
                        path_id is not None
                        and evidence.path_id != path_id
                    )
                    or (
                        repository is not None
                        and evidence.repository != repository
                    )
                ):
                    reasons.append(
                        f"completion evidence is stale or invalid: "
                        f"{evidence_id}"
                    )
        return reasons


def _repository_expansions(revision: GoalRevision) -> set[str]:
    """Extract explicitly named repository additions from a goal revision."""

    repositories: set[str] = set()
    changes = revision.proposed_changes
    for key in (
        "readable_repositories",
        "repository_allowlist",
        "repositories",
    ):
        values = changes.get(key, [])
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str):
                repositories.add(value)
            elif isinstance(value, dict):
                repository = value.get("repository")
                if isinstance(repository, str):
                    repositories.add(repository)
    return repositories


def _evidence_reports_success(result: dict[str, Any]) -> bool:
    """Recognize explicit successful validation outcomes without guessing."""

    if result.get("exit_code") == 0:
        return True
    return any(
        result.get(key) is True
        for key in ("passed", "success", "fix_confirmed")
    )
