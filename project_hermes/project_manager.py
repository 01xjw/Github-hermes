"""Long-lived Main Hermes project manager for polling-backed Work."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import Field, model_validator

from project_hermes.assurance import CompletionLayer, ReviewRole
from project_hermes.candidate_ingest import CandidateIngestor
from project_hermes.config import ProjectHermesConfig
from project_hermes.controller import ProjectHermesController
from project_hermes.execution import ExecutionCoordinator
from project_hermes.models import (
    GoalPath,
    IssueTask,
    PermissionBudget,
    ProjectRole,
    RepositoryResponsibility,
    StrictModel,
    TargetHardware,
    TriageDecision,
    utc_now,
)
from project_hermes.polling_store import (
    EnvironmentStatus,
    ProjectManagerState,
    SqlitePollingStore,
    WorkItem,
    WorkPlan,
    WorkResourceRequirements,
    WorkStatus,
)
from project_hermes.publication import (
    InternalCandidateLockState,
    InternalPullRequestCandidate,
    InternalPullRequestCandidateStore,
)
from project_hermes.redaction import redact_text
from project_hermes.repository_skills import RepositorySkillRegistry
from project_hermes.runtime.base import (
    AgentRuntime,
    RuntimeHandle,
    RuntimeModelRoute,
    RuntimeRequest,
    RuntimeResult,
    RuntimeStatus,
)
from project_hermes.store import RunStore
from project_hermes.work_graph import LifecycleStatus, TERMINAL_STATUSES


_EXTERNAL_GITHUB_ACTION_RE = re.compile(
    r"(?:\b(?:github|issue)\b.{0,80}\b(?:comment|reply|response)\b|"
    r"\b(?:comment|reply|response)\b.{0,80}\b(?:github|issue)\b|"
    r"\b(?:publish|push)\b.{0,80}\b(?:branch|change|comment|reply)\b|"
    r"\bopen\b.{0,40}\bpull request\b)",
    re.IGNORECASE,
)
_PLAN_CLAUSE_SPLIT_RE = re.compile(
    r"(?:[.;\n]+|\b(?:but|however|then)\b)",
    re.IGNORECASE,
)
_NEGATED_ACTION_RE = re.compile(
    r"\b(?:without|never|no|not|must\s+not|do\s+not|don't|"
    r"forbid(?:den)?|prohibit(?:ed)?)\b",
    re.IGNORECASE,
)
_GPU_COUNT_RE = re.compile(
    r"\b(?P<count>[1-9][0-9]*)\s*[x×]\s*"
    r"(?:amd\s+)?(?:mi[0-9][a-z0-9-]*|gpus?|cards?)\b",
    re.IGNORECASE,
)
_GPU_ARCHITECTURE_RE = re.compile(
    r"\b(?:gfx[0-9]{3,5}[a-z0-9]*|mi[0-9]{2,4}[a-z0-9]*)\b",
    re.IGNORECASE,
)
_MANAGER_PROTOCOL_ERROR_MARKERS = (
    "validation error for ProjectAction",
    "project_actions must",
    "project_actions entry",
    "project action must",
    "project action cannot",
    "unsupported project action",
    "Main Hermes returned no runtime result",
    "Main Hermes can start only planned Work",
)


def _completed_review_feedback(
    candidate: InternalPullRequestCandidate,
) -> dict[str, Any] | None:
    """Return a bounded packet only after every mandatory role reviewed."""

    reviews_by_role = {review.role: review for review in candidate.reviews}
    required_roles = [role.value for role in ReviewRole]
    if set(reviews_by_role) != set(required_roles):
        return None
    reviews: list[dict[str, Any]] = []
    for role in required_roles:
        review = reviews_by_role[role]
        reviews.append(
            {
                "role": role,
                "verdict": review.verdict,
                "summary": redact_text(review.summary).strip()[:4000],
                "findings": [
                    redact_text(finding).strip()[:1000]
                    for finding in review.findings[:20]
                    if redact_text(finding).strip()
                ],
                "review_id": review.review_id,
            }
        )
    return {
        "schema_version": "project-hermes-review-feedback.v1",
        "source_candidate_id": candidate.candidate_id,
        "reviews": reviews,
    }


class ProjectActionKind(StrEnum):
    PLAN = "plan"
    START = "start"
    BLOCK = "block"
    WAIT = "wait"


class ProjectAction(StrictModel):
    action: ProjectActionKind
    reason: str = Field(min_length=1, max_length=4000)
    work_item_id: str | None = None
    plan: WorkPlan | None = None

    @model_validator(mode="after")
    def action_has_required_fields(self) -> "ProjectAction":
        if self.action is ProjectActionKind.WAIT:
            if self.work_item_id is not None or self.plan is not None:
                raise ValueError("wait cannot name a Work item or plan")
            return self
        if not self.work_item_id:
            raise ValueError(f"{self.action.value} requires work_item_id")
        if self.action is ProjectActionKind.PLAN and self.plan is None:
            raise ValueError("plan action requires a Work plan")
        if self.action is not ProjectActionKind.PLAN and self.plan is not None:
            raise ValueError("only plan actions may include a Work plan")
        return self


class ProjectManagerAdvance(StrictModel):
    session_id: str
    action: ProjectAction
    outcome: dict[str, Any]


class IssueLaneLauncher(Protocol):
    def launch(self, task: IssueTask, run_id: str) -> Any:
        """Launch one controller-owned isolated worker."""


class BaselineResolver(Protocol):
    def resolve(self, repository: str) -> Any:
        """Resolve one configured repository to an immutable baseline."""


class MainHermesProjectManager:
    """Let Main Hermes plan and dispatch polling-enqueued Work."""

    manager_task_id = "project-hermes-manager"

    def __init__(
        self,
        store: SqlitePollingStore,
        runtime: AgentRuntime,
        controller: ProjectHermesController,
        runs: RunStore,
        config: ProjectHermesConfig,
        *,
        baseline_resolver: BaselineResolver,
        launcher: IssueLaneLauncher,
        candidates: InternalPullRequestCandidateStore,
        execution: ExecutionCoordinator | None = None,
        candidate_ingestor: CandidateIngestor | None = None,
        model_route: RuntimeModelRoute | None = None,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.controller = controller
        self.runs = runs
        self.config = config
        self.baseline_resolver = baseline_resolver
        self.launcher = launcher
        self.candidates = candidates
        self.execution = execution
        self.candidate_ingestor = candidate_ingestor
        self.model_route = model_route or RuntimeModelRoute.model_validate(
            config.hermes.model_profile.model_dump()
        )
        self.repository_skills = RepositorySkillRegistry(config.polling)
        if config.polling.require_repository_skills:
            self.repository_skills.validate_all()

    def reconcile(self) -> list[str]:
        """Project worker and internal-candidate state onto Work items."""

        if self.execution is not None:
            self.execution.reconcile_all()
        if self.candidate_ingestor is not None:
            self.candidate_ingestor.reconcile_all()
        candidates = self._internal_candidates_by_task()
        changed: list[str] = []
        for item in self.store.list_work_items(
            statuses=[WorkStatus.RUNNING, WorkStatus.REVIEW],
            limit=500,
        ):
            if item.status is WorkStatus.REVIEW:
                candidate = candidates.get(item.task_id or "")
                if (
                    candidate is not None
                    and candidate.lock_state
                    is InternalCandidateLockState.IMMUTABLE_APPROVED
                ):
                    self.store.transition_work_item(
                        item.work_item_id,
                        WorkStatus.DONE,
                        current_step=(
                            "Internal candidate passed review and is immutable."
                        ),
                        internal_candidate_id=candidate.candidate_id,
                    )
                    changed.append(item.work_item_id)
                    continue
                if candidate is None:
                    continue
                feedback = _completed_review_feedback(candidate)
                if feedback is None:
                    continue
                review_attempts = self.store.count_work_events(
                    work_item_id_value=item.work_item_id,
                    event_type="work.review",
                )
                verdicts = {
                    str(review["verdict"])
                    for review in feedback["reviews"]
                }
                if verdicts == {"APPROVE"}:
                    # The reviewer service owns the atomic immutable lock.
                    # Reconcile again after it finalizes unanimous reviews.
                    continue
                feedback["reviewed_execution_attempt"] = (
                    item.execution_attempt
                )
                feedback["reviewed_candidate_attempt"] = review_attempts
                if "REJECT" in verdicts:
                    self.store.transition_work_item(
                        item.work_item_id,
                        WorkStatus.BLOCKED,
                        current_step=(
                            "Independent review rejected the internal candidate."
                        ),
                        internal_candidate_id=candidate.candidate_id,
                        reason=(
                            "At least one mandatory independent Reviewer "
                            "returned REJECT."
                        ),
                    )
                elif (
                    review_attempts
                    >= self.config.polling.max_review_execution_attempts
                ):
                    self.store.transition_work_item(
                        item.work_item_id,
                        WorkStatus.BLOCKED,
                        current_step=(
                            "Independent review revision budget was exhausted."
                        ),
                        internal_candidate_id=candidate.candidate_id,
                        reason=(
                            "The candidate remained non-unanimous after "
                            f"{review_attempts} reviewed Worker candidate "
                            f"attempts across {item.execution_attempt} total "
                            "executions."
                        ),
                    )
                else:
                    self.store.request_review_revision(
                        item.work_item_id,
                        reviewed_candidate_id=candidate.candidate_id,
                        review_feedback=feedback,
                    )
                changed.append(item.work_item_id)
                continue
            if not item.run_id:
                self.store.transition_work_item(
                    item.work_item_id,
                    WorkStatus.FAILED,
                    current_step="Execution identity is missing.",
                    error="Running Work item has no controller run id.",
                )
                changed.append(item.work_item_id)
                continue
            run = self.runs.get_run(item.run_id)
            if run.status not in TERMINAL_STATUSES:
                continue
            if run.status is LifecycleStatus.COMPLETED:
                candidate = candidates.get(item.task_id or "")
                if candidate is None:
                    continue
                self.store.transition_work_item(
                    item.work_item_id,
                    WorkStatus.REVIEW,
                    current_step="Internal candidate is awaiting review.",
                    internal_candidate_id=candidate.candidate_id,
                )
            elif run.status in {
                LifecycleStatus.BLOCKED,
                LifecycleStatus.CANCELLED,
            }:
                self.store.transition_work_item(
                    item.work_item_id,
                    WorkStatus.BLOCKED,
                    current_step=f"Controller run ended {run.status.value}.",
                    reason=f"Controller run ended {run.status.value}.",
                )
            else:
                failure_kind = self._execution_failure_kind(item)
                if failure_kind == "provider_capacity_exhausted":
                    delay = _worker_capacity_retry_delay(
                        self.config,
                        execution_attempt=item.execution_attempt,
                    )
                    now = utc_now()
                    self.store.retry_work_item_after_capacity_failure(
                        item.work_item_id,
                        retry_not_before=now + timedelta(seconds=delay),
                        error=(
                            "Worker model provider capacity was exhausted "
                            "after bounded same-thread retries."
                        ),
                        now=now,
                    )
                else:
                    self.store.transition_work_item(
                        item.work_item_id,
                        WorkStatus.FAILED,
                        current_step=f"Controller run ended {run.status.value}.",
                        error=f"Controller run ended {run.status.value}.",
                    )
            changed.append(item.work_item_id)
        return changed

    def _execution_failure_kind(self, item: WorkItem) -> str | None:
        if self.execution is None or not item.execution_id:
            return None
        try:
            record = self.execution.store.get(item.execution_id)
        except KeyError:
            return None
        observation = record.observation
        if observation is None:
            return None
        value = observation.result.get("failure_kind")
        return value if isinstance(value, str) else None

    def advance(
        self,
        *,
        force: bool = False,
        reconcile: bool = True,
    ) -> ProjectManagerAdvance | None:
        """Run one Main Hermes turn only when durable state needs a decision."""

        if reconcile:
            self.reconcile()
        snapshot = self._snapshot()
        if not snapshot["work_items"]:
            return None
        capacity = snapshot["capacity"]
        if int(capacity["active"]) >= int(capacity["maximum"]):
            return None
        instructions, instructions_digest = self._instructions()
        state = self._state(instructions_digest)
        scheduled_action = self._scheduled_start(snapshot)
        if scheduled_action is not None:
            return self._apply_action(state, scheduled_action)
        if not any(
            item["status"] == WorkStatus.QUEUED.value
            for item in snapshot["work_items"]
        ):
            # Committed plans in provider-capacity cooldown do not require a
            # Main Hermes model turn. The deterministic scheduler wakes them
            # as soon as retry_not_before elapses.
            return None
        # Poll-run identifiers change while the continuous scanner walks the
        # repository registry, but they do not by themselves require a model
        # decision. Wake Main Hermes for Work/capacity changes instead.
        snapshot_digest = _digest(
            {
                "capacity": snapshot["capacity"],
                "work_counts": snapshot["work_counts"],
                "work_items": snapshot["work_items"],
            }
        )
        idle_for = (
            utc_now() - state.last_turn_at
            if state.last_turn_at is not None
            else None
        )
        if state.last_error is None:
            decision_interval_seconds = (
                self.config.polling.manager_idle_interval_seconds
            )
        elif _is_manager_protocol_error(state.last_error):
            decision_interval_seconds = (
                self.config.polling.manager_protocol_retry_seconds
            )
        else:
            decision_interval_seconds = (
                self.config.polling.manager_error_retry_seconds
            )
        if (
            not force
            and idle_for is not None
            and idle_for
            < timedelta(seconds=decision_interval_seconds)
            and (
                state.last_error is not None
                or state.snapshot_digest == snapshot_digest
            )
        ):
            return None
        fresh_session = self.config.polling.manager_fresh_session_per_decision
        if fresh_session and (
            state.last_turn_at is not None
            or state.last_error is not None
            or state.handle.status is not RuntimeStatus.STARTING
        ):
            state = self._replace_manager_session(state)
        starting = state.handle.status is RuntimeStatus.STARTING
        request = self._request(
            state,
            prompt=(
                self._initial_prompt(
                    instructions,
                    snapshot,
                    last_error=state.last_error,
                )
                if starting
                else self._continuation_prompt(
                    snapshot,
                    last_error=state.last_error,
                )
            ),
            request_kind="bootstrap" if starting else "continue",
        )
        try:
            if fresh_session:
                handle = self.runtime.start(request)
            elif starting:
                try:
                    connected = self.runtime.reconnect(state.handle, request)
                except (KeyError, RuntimeError):
                    handle = self.runtime.start(request)
                else:
                    handle = self.runtime.resume(connected, request)
            else:
                connected = self.runtime.reconnect(state.handle, request)
                handle = self.runtime.resume(connected, request)
        except Exception as exc:
            self._record_manager_error(state, exc)
            raise
        persisted = self._persist_runtime_turn(
            state,
            handle,
            snapshot_digest=snapshot_digest,
        )
        try:
            action = _action_from_result(self.runtime.result(handle))
            self._require_progress(action, snapshot)
        except Exception as exc:
            self._record_manager_error(persisted, exc)
            raise
        if fresh_session:
            close = getattr(self.runtime, "close", None)
            if callable(close):
                with suppress(Exception):
                    closed = close(handle)
                    persisted = self.store.put_manager_state(
                        persisted.model_copy(
                            update={"handle": closed, "updated_at": utc_now()}
                        ),
                        expected_version=persisted.version,
                    )
        return self._apply_action(persisted, action)

    @staticmethod
    def _scheduled_start(
        snapshot: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> ProjectAction | None:
        """Advance or block the oldest committed plan deterministically."""

        capacity = snapshot["capacity"]
        if int(capacity["active"]) >= int(capacity["maximum"]):
            return None
        timestamp = now or utc_now()
        for item in snapshot["work_items"]:
            if (
                item["status"] == WorkStatus.PLANNING.value
                and item.get("plan") is not None
                and _capacity_retry_ready(item, timestamp)
            ):
                blocker = _committed_plan_blocker(item)
                if blocker is not None:
                    return ProjectAction(
                        action=ProjectActionKind.BLOCK,
                        work_item_id=str(item["work_item_id"]),
                        reason=blocker,
                    )
                capacity_blocker = _permanent_gpu_capacity_blocker(
                    item,
                    capacity,
                )
                if capacity_blocker is not None:
                    return ProjectAction(
                        action=ProjectActionKind.BLOCK,
                        work_item_id=str(item["work_item_id"]),
                        reason=capacity_blocker,
                    )
                if not _temporary_gpu_capacity_available(item, capacity):
                    # A multi-GPU plan may be perfectly valid while other Work
                    # temporarily holds part of the pool. Keep it planned and
                    # inspect later entries so a CPU task (or a smaller GPU
                    # task) can still use otherwise idle worker capacity.
                    continue
                return ProjectAction(
                    action=ProjectActionKind.START,
                    work_item_id=str(item["work_item_id"]),
                    reason=(
                        "Main Hermes scheduler is advancing the oldest "
                        "committed plan while worker capacity is available."
                    ),
                )
        return None

    def _apply_action(
        self,
        state: ProjectManagerState,
        action: ProjectAction,
    ) -> ProjectManagerAdvance:
        """Apply one model-selected or deterministic manager action."""

        try:
            outcome = self._dispatch(action)
        except Exception as exc:
            detail = redact_text(str(exc)) or type(exc).__name__
            self.store.record_event(
                "manager.action_failed",
                {
                    "action": action.action.value,
                    "work_item_id": action.work_item_id,
                    "reason": detail,
                },
                work_item_id_value=action.work_item_id,
            )
            state = self.store.put_manager_state(
                state.model_copy(
                    update={"last_error": detail, "updated_at": utc_now()}
                ),
                expected_version=state.version,
            )
            outcome = {"ok": False, "error": detail}
        else:
            if state.last_error is not None:
                state = self.store.put_manager_state(
                    state.model_copy(
                        update={"last_error": None, "updated_at": utc_now()}
                    ),
                    expected_version=state.version,
                )
            self.store.record_event(
                "manager.action_applied",
                {
                    "action": action.action.value,
                    "work_item_id": action.work_item_id,
                    "reason": action.reason,
                },
                work_item_id_value=action.work_item_id,
            )
        return ProjectManagerAdvance(
            session_id=state.session_id,
            action=action,
            outcome=outcome,
        )

    def _state(self, instructions_digest: str) -> ProjectManagerState:
        state = self.store.get_manager_state()
        if state is not None and state.instructions_digest == instructions_digest:
            return state
        now = utc_now()
        session_id = self._new_manager_session_id(instructions_digest)
        replacement = ProjectManagerState(
            session_id=session_id,
            handle=RuntimeHandle(
                runtime_name=self.runtime.name,
                session_id=session_id,
                task_id=self.manager_task_id,
                status=RuntimeStatus.STARTING,
                model_route=self.model_route,
            ),
            instructions_digest=instructions_digest,
            created_at=now,
            updated_at=now,
        )
        if state is None:
            return self.store.put_manager_state(
                replacement,
                expected_version=None,
            )
        self.store.record_event(
            "manager.instructions_changed",
            {
                "old_digest": state.instructions_digest,
                "new_digest": instructions_digest,
                "new_session_id": session_id,
            },
        )
        return self.store.reset_manager_state(replacement)

    def _replace_manager_session(
        self,
        state: ProjectManagerState,
    ) -> ProjectManagerState:
        """Start the next decision in a fresh, bounded conversation."""

        now = utc_now()
        session_id = self._new_manager_session_id(state.instructions_digest)
        replacement = ProjectManagerState(
            session_id=session_id,
            handle=RuntimeHandle(
                runtime_name=self.runtime.name,
                session_id=session_id,
                task_id=self.manager_task_id,
                status=RuntimeStatus.STARTING,
                model_route=self.model_route,
            ),
            instructions_digest=state.instructions_digest,
            snapshot_digest=state.snapshot_digest,
            last_turn_at=state.last_turn_at,
            last_error=state.last_error,
            created_at=now,
            updated_at=now,
        )
        self.store.record_event(
            "manager.session_rotated",
            {
                "previous_session_id": state.session_id,
                "new_session_id": session_id,
                "reason": "fresh decision conversation",
            },
            now=now,
        )
        return self.store.reset_manager_state(replacement)

    @staticmethod
    def _new_manager_session_id(instructions_digest: str) -> str:
        return (
            f"project-manager-{instructions_digest[:12]}-"
            f"{uuid4().hex[:12]}"
        )

    def _snapshot(self) -> dict[str, Any]:
        # Running Work belongs exclusively to its isolated Codex worker, and
        # review Work belongs to the independent approval pipeline. Project
        # only queued/planning items so neither ordinary worker heartbeats nor
        # an unresolved review spend a Main Hermes model turn or context.
        statuses = [WorkStatus.QUEUED, WorkStatus.PLANNING]
        now = utc_now()
        work = sorted(
            (
                item
                for item in self.store.list_work_items(
                    statuses=statuses,
                    limit=500,
                )
                if item.retry_not_before is None
                or item.retry_not_before <= now
            ),
            key=lambda item: (item.queued_at, item.work_item_id),
        )[: self.config.polling.manager_projection_limit]
        projected: list[dict[str, Any]] = []
        for item in work:
            candidate = self.store.get_candidate(item.candidate_id)
            screening = self.store.get_current_screening(item.candidate_id)
            projected.append(
                {
                    "work_item_id": item.work_item_id,
                    "status": item.status.value,
                    "current_step": item.current_step,
                    "repository_id": item.repository_id,
                    "repository": item.repository,
                    "issue_number": item.issue_number,
                    "issue_url": item.issue_url,
                    "title": item.title,
                    "body": candidate.body[
                        : self.config.polling.manager_issue_body_char_limit
                    ],
                    "labels": candidate.labels,
                    "evidence_score": candidate.evidence_score,
                    "screening": (
                        screening.model_dump(mode="json")
                        if screening is not None
                        else None
                    ),
                    "environment_status": item.environment_status.value,
                    "resource_requirements": (
                        item.resource_requirements.model_dump(mode="json")
                        if item.environment_status
                        is EnvironmentStatus.VERIFIED
                        and item.resource_requirements is not None
                        else None
                    ),
                    "plan": (
                        item.plan.model_dump(mode="json") if item.plan else None
                    ),
                    "task_id": item.task_id,
                    "run_id": item.run_id,
                    "execution_id": item.execution_id,
                    "internal_candidate_id": item.internal_candidate_id,
                    "last_error": item.last_error,
                    "execution_attempt": item.execution_attempt,
                    "retry_not_before": (
                        item.retry_not_before.isoformat()
                        if item.retry_not_before is not None
                        else None
                    ),
                }
            )
        latest_run = self.store.latest_run()
        return {
            "polling": (
                {
                    "run_id": latest_run.run_id,
                    "status": latest_run.status.value,
                    "repositories_scanned": latest_run.repositories_scanned,
                    "repositories_failed": latest_run.repositories_failed,
                    "candidates_matched": latest_run.candidates_matched,
                    "work_items_queued": latest_run.work_items_queued,
                }
                if latest_run is not None
                else None
            ),
            "capacity": {
                "active": self.store.active_work_count(),
                "maximum": self.config.polling.max_active_work_items,
                "gpus": {
                    "active": self.store.active_work_gpu_count(),
                    "maximum": self.config.polling.max_global_gpus,
                },
            },
            "work_counts": self.store.work_counts(),
            "work_items": projected,
        }

    @staticmethod
    def _require_progress(
        action: ProjectAction,
        snapshot: dict[str, Any],
    ) -> None:
        """Reject actions that leave runnable planned Work idle."""

        capacity = snapshot["capacity"]
        if int(capacity["active"]) >= int(capacity["maximum"]):
            return
        now = utc_now()
        planned_ids = set()
        for item in snapshot["work_items"]:
            if (
                item["status"] != WorkStatus.PLANNING.value
                or item.get("plan") is None
                or not _capacity_retry_ready(item, now)
            ):
                continue
            if (
                _committed_plan_blocker(item) is not None
                or _permanent_gpu_capacity_blocker(item, capacity) is not None
                or _temporary_gpu_capacity_available(item, capacity)
            ):
                planned_ids.add(str(item["work_item_id"]))
        if not planned_ids:
            return
        if (
            action.action in {ProjectActionKind.START, ProjectActionKind.BLOCK}
            and action.work_item_id in planned_ids
        ):
            return
        visible = ", ".join(sorted(planned_ids)[:6])
        raise ValueError(
            "planned Work must advance while worker capacity is available; "
            "choose start, or block the planned item with an exact missing "
            f"capability: {visible}"
        )

    def _dispatch(self, action: ProjectAction) -> dict[str, Any]:
        if action.action is ProjectActionKind.WAIT:
            return {"ok": True, "waiting": True}
        assert action.work_item_id is not None
        item = self.store.get_work_item(action.work_item_id)
        if action.action is ProjectActionKind.PLAN:
            assert action.plan is not None
            if item.environment_status is not EnvironmentStatus.VERIFIED:
                item = self._verify_environment(item)
            planned = self.store.plan_work_item(
                item.work_item_id,
                action.plan,
            )
            return {
                "ok": True,
                "work_item_id": planned.work_item_id,
                "status": planned.status.value,
            }
        if action.action is ProjectActionKind.BLOCK:
            blocked = self.store.transition_work_item(
                item.work_item_id,
                WorkStatus.BLOCKED,
                current_step="Main Hermes blocked this Work item.",
                reason=action.reason,
            )
            return {
                "ok": True,
                "work_item_id": blocked.work_item_id,
                "status": blocked.status.value,
            }
        if action.action is ProjectActionKind.START:
            return self._start(item)
        raise AssertionError(f"unsupported project action: {action.action}")

    def _start(self, item: WorkItem) -> dict[str, Any]:
        if item.status is not WorkStatus.PLANNING or item.plan is None:
            raise ValueError("Main Hermes can start only planned Work")
        if (
            item.retry_not_before is not None
            and utc_now() < item.retry_not_before
        ):
            raise ValueError("Worker capacity retry cooldown has not elapsed")
        if self.store.active_work_count() >= self.config.polling.max_active_work_items:
            raise RuntimeError("configured active Work capacity is full")
        required_gpus = (
            item.resource_requirements.gpu_count
            if item.resource_requirements is not None
            else 0
        )
        active_gpus = self.store.active_work_gpu_count()
        maximum_gpus = self.config.polling.max_global_gpus
        if required_gpus > maximum_gpus:
            raise RuntimeError(
                f"Work requires {required_gpus} GPUs but configured global "
                f"capacity is {maximum_gpus}"
            )
        if active_gpus + required_gpus > maximum_gpus:
            raise RuntimeError(
                f"Work requires {required_gpus} GPUs but only "
                f"{max(maximum_gpus - active_gpus, 0)} are currently available"
            )
        candidate = self.store.get_candidate(item.candidate_id)
        if (
            item.environment_status is not EnvironmentStatus.VERIFIED
            or not item.named_baseline
        ):
            raise ValueError("Work environment has not been verified")
        task = self._issue_task(item, candidate, item.named_baseline)
        run_id = f"work-run-{task.task_id.removeprefix('issue-')}"
        run = None
        try:
            run = self.controller.create_task_run(task, run_id=run_id)
            launched = self.launcher.launch(task, run.run_id)
            execution_id = str(getattr(launched, "execution_id", "") or "")
            if not execution_id:
                raise ValueError("Issue launcher returned no execution identity")
        except Exception as exc:
            if run is not None and run.status not in TERMINAL_STATUSES:
                self.runs.complete_run(
                    run.run_id,
                    status=LifecycleStatus.FAILED,
                )
            self.store.transition_work_item(
                item.work_item_id,
                WorkStatus.FAILED,
                current_step="Worker launch failed.",
                error=redact_text(str(exc)),
            )
            raise
        started = self.store.start_work_item(
            item.work_item_id,
            task_id=task.task_id,
            run_id=run.run_id,
            execution_id=execution_id,
        )
        return {
            "ok": True,
            "work_item_id": started.work_item_id,
            "status": started.status.value,
            "task_id": task.task_id,
            "run_id": run.run_id,
            "execution_id": execution_id,
        }

    def _verify_environment(self, item: WorkItem) -> WorkItem:
        if not self.config.codex.enabled:
            raise RuntimeError("Codex worker runtime is disabled")
        if not self.config.kubernetes_jobs.enabled:
            raise RuntimeError("Kubernetes worker execution is disabled")
        baseline = self.baseline_resolver.resolve(item.repository)
        healthy = [device for device in self.config.gpu_devices if device.healthy]
        screening = self.store.get_current_screening(item.candidate_id)
        if screening is not None and screening.decision.value == "SELECT":
            requested = screening.required_environment
            gpu_count = requested.gpu_count
            architecture = None
            if gpu_count:
                accepted = {
                    value.casefold() for value in requested.gpu_architectures
                }
                architecture = next(
                    (
                        device.architecture
                        for device in healthy
                        if device.architecture is not None
                        and (
                            device.architecture.casefold() in accepted
                            or accepted.intersection({"amd", "rocm", "any"})
                        )
                    ),
                    None,
                )
                if architecture is None:
                    raise ValueError(
                        "screening-selected GPU architecture is unavailable"
                    )
        else:
            # Preserve the historical lane for Work admitted before screening
            # became mandatory.
            gpu_count = (
                1 if healthy and self.config.resources.max_gpu_count > 0 else 0
            )
            architecture = healthy[0].architecture if gpu_count else None
        route = self.config.codex.route(self.config.codex.primary_profile)
        resources = WorkResourceRequirements(
            cpu_request=self.config.kubernetes_jobs.cpu_request,
            cpu_limit=self.config.kubernetes_jobs.cpu_limit,
            memory_request=self.config.kubernetes_jobs.memory_request,
            memory_limit=self.config.kubernetes_jobs.memory_limit,
            gpu_count=gpu_count,
            gpu_architecture=architecture,
            worker_model=route.model,
        )
        return self.store.verify_work_environment(
            item.work_item_id,
            named_baseline=baseline.named_baseline,
            resources=resources,
        )

    def _issue_task(
        self,
        item: WorkItem,
        candidate: Any,
        named_baseline: str,
    ) -> IssueTask:
        assert item.plan is not None
        resources = item.resource_requirements
        if resources is None:
            raise ValueError("verified Work has no resource requirements")
        minimum_gpu_count = resources.gpu_count
        healthy_architectures = (
            [resources.gpu_architecture]
            if resources.gpu_count and resources.gpu_architecture
            else []
        )
        task_metadata: dict[str, Any] = {
            "source": "scheduled_polling",
            "work_item_id": item.work_item_id,
            "candidate_id": item.candidate_id,
            "repository_id": item.repository_id,
            "issue_snapshot": {
                "repository": candidate.repository,
                "number": candidate.issue_number,
                "url": candidate.issue_url,
                "title": candidate.title,
                "body": candidate.body,
                "labels": candidate.labels,
                "created_at": candidate.created_at.isoformat(),
                "updated_at": candidate.updated_at.isoformat(),
            },
            "work_plan": item.plan.model_dump(mode="json"),
            "execution_attempt": item.execution_attempt + 1,
        }
        review_feedback = self._review_feedback_for_work(item)
        if review_feedback is not None:
            task_metadata["review_feedback"] = review_feedback
        task = IssueTask(
            task_id=f"issue-{item.work_item_id}",
            issue_urls=[item.issue_url],
            named_baseline=named_baseline,
            goals=[
                GoalPath(
                    path_id="issue-resolution",
                    description=item.plan.summary,
                    repository=item.repository,
                    acceptance_criteria=item.plan.acceptance_criteria,
                    required_completion_layers=[
                        CompletionLayer.IMPLEMENTED.value,
                        CompletionLayer.GATE_VERIFIED.value,
                        CompletionLayer.CI_REVIEWED.value,
                    ],
                )
            ],
            repositories=[
                RepositoryResponsibility(
                    repository=item.repository,
                    responsibilities=[
                        "Implement the Main Hermes plan for the locked Issue."
                    ],
                )
            ],
            non_goals=[
                "Publishing or pushing changes to GitHub.",
                "Expanding beyond the selected repository and Issue.",
            ],
            must_preserve=[
                "Behavior outside the locked Issue scope.",
                "Credentials and controller-owned state boundaries.",
            ],
            target_hardware=TargetHardware(
                gpu_architectures=healthy_architectures,
                minimum_gpu_count=minimum_gpu_count,
                environment_name="project-hermes-worker",
            ),
            permissions=PermissionBudget(
                readable_repositories=[item.repository],
                writable_repositories=[item.repository],
                may_request_execution=True,
                may_request_review=True,
                may_publish=False,
                may_access_network=False,
            ),
            resource_limits=self.config.resources.model_copy(deep=True),
            triage_decision=TriageDecision.APPROVE,
            triage_evidence_refs=[
                item.issue_url,
                f"main-hermes-plan:{_digest(item.plan.model_dump(mode='json'))}",
            ],
            required_ci_checks=[],
            locked_at=item.planning_started_at or item.updated_at,
            metadata=task_metadata,
        )
        repository_skill = self.repository_skills.resolve(item.repository)
        if repository_skill is not None:
            task = task.model_copy(
                update={
                    "metadata": {
                        **task.metadata,
                        "repository_skill": repository_skill.model_dump(
                            mode="json"
                        ),
                    }
                }
            )
        identity_digest = _digest(
            task.model_dump(mode="json", exclude={"task_id"})
        )[:16]
        return task.model_copy(
            update={
                "task_id": f"issue-{item.work_item_id}-{identity_digest}"
            }
        )

    def _review_feedback_for_work(
        self,
        item: WorkItem,
    ) -> dict[str, Any] | None:
        """Load bounded feedback from the latest reviewed candidate."""

        if item.internal_candidate_id is None:
            return None
        try:
            candidate = self.candidates.get(item.internal_candidate_id)
        except KeyError as exc:
            raise ValueError(
                "Work references an unknown reviewed internal candidate"
            ) from exc
        feedback = _completed_review_feedback(candidate)
        if feedback is None or all(
            review["verdict"] == "APPROVE"
            for review in feedback["reviews"]
        ):
            return None
        return {
            **feedback,
            "reviewed_execution_attempt": item.execution_attempt,
            "reviewed_candidate_attempt": self.store.count_work_events(
                work_item_id_value=item.work_item_id,
                event_type="work.review",
            ),
            "required_action": (
                "Produce a fresh candidate that addresses every non-approval "
                "summary and finding. Re-run the requested bounded evidence; "
                "do not merely restate the previous result."
            ),
        }

    def _request(
        self,
        state: ProjectManagerState,
        *,
        prompt: str,
        request_kind: str,
    ) -> RuntimeRequest:
        return RuntimeRequest(
            request_id=(
                "project-manager-"
                + hashlib.sha256(
                    f"{request_kind}:{utc_now().isoformat()}".encode("utf-8")
                ).hexdigest()[:24]
            ),
            task_id=self.manager_task_id,
            role=ProjectRole.PROJECT_HERMES,
            prompt=prompt,
            session_id=state.session_id,
            model_route=state.handle.model_route or self.model_route,
            metadata={
                "request_kind": request_kind,
                "after_runtime_event_sequence": state.last_event_sequence,
                "operator_follow_up_allowed": False,
            },
        )

    def _persist_runtime_turn(
        self,
        state: ProjectManagerState,
        handle: RuntimeHandle,
        *,
        snapshot_digest: str,
    ) -> ProjectManagerState:
        sequence = state.last_event_sequence
        for event in self.runtime.events(
            handle,
            after_sequence=state.last_event_sequence,
        ):
            if event.session_id != state.session_id or event.sequence <= sequence:
                raise ValueError("Main Hermes runtime events are out of order")
            self.store.record_event(
                f"manager.runtime.{event.event_type}",
                {
                    "runtime_event_id": event.event_id,
                    "runtime_sequence": event.sequence,
                    "payload_keys": sorted(event.payload),
                },
                now=event.created_at,
            )
            sequence = event.sequence
        updated = state.model_copy(
            update={
                "handle": handle,
                "snapshot_digest": snapshot_digest,
                "last_event_sequence": sequence,
                "last_turn_at": utc_now(),
                "last_error": None,
                "updated_at": utc_now(),
            }
        )
        return self.store.put_manager_state(
            updated,
            expected_version=state.version,
        )

    def _record_manager_error(
        self,
        state: ProjectManagerState,
        error: Exception,
    ) -> None:
        detail = redact_text(str(error)) or type(error).__name__
        self.store.record_event(
            "manager.turn_failed",
            {"error": detail},
        )
        now = utc_now()
        self.store.put_manager_state(
            state.model_copy(
                update={
                    "last_error": detail,
                    # A failed first request has no prior turn timestamp. Record
                    # the attempt so provider backoff also applies to bootstrap
                    # failures instead of retrying on every supervisor tick.
                    "last_turn_at": now,
                    "updated_at": now,
                }
            ),
            expected_version=state.version,
        )

    def _instructions(self) -> tuple[str, str]:
        path = Path(self.config.polling.agent_instructions_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(
                f"Main Hermes AGENT.md does not exist: {path}"
            )
        if path.stat().st_size > 65_536:
            raise ValueError("Main Hermes AGENT.md exceeds 64 KiB")
        instructions = path.read_text(encoding="utf-8").strip()
        if not instructions:
            raise ValueError("Main Hermes AGENT.md is empty")
        return instructions, hashlib.sha256(
            instructions.encode("utf-8")
        ).hexdigest()

    def _internal_candidates_by_task(
        self,
    ) -> dict[str, InternalPullRequestCandidate]:
        total = self.candidates.count()
        by_task: dict[str, InternalPullRequestCandidate] = {}
        for offset in range(0, total, 200):
            for candidate in self.candidates.list_candidates(
                limit=200,
                offset=offset,
            ):
                by_task[candidate.task_id] = candidate
        return by_task

    @staticmethod
    def _initial_prompt(
        instructions: str,
        snapshot: dict[str, Any],
        *,
        last_error: str | None = None,
    ) -> str:
        feedback = (
            "The previous independent decision failed: "
            + last_error
            + "\nDo not repeat that failure.\n\n"
            if last_error
            else ""
        )
        return (
            "The following AGENT.md is your stable project-manager contract. "
            "This is a fresh decision conversation; durable state below is "
            "the only prior context. Follow the contract for this turn.\n\n"
            + instructions
            + "\n\nCurrent durable projection:\n"
            + feedback
            + json.dumps(snapshot, sort_keys=True, ensure_ascii=True)
        )

    @staticmethod
    def _continuation_prompt(
        snapshot: dict[str, Any],
        *,
        last_error: str | None = None,
    ) -> str:
        feedback = (
            "The previous turn or action was rejected: "
            + last_error
            + "\nCorrect that error in this turn.\n\n"
            if last_error
            else ""
        )
        return (
            "Continue the same Main Hermes project-manager contract. Inspect "
            "the current durable projection, choose exactly one next action, "
            "and return the required project_actions JSON.\n\n"
            + feedback
            + json.dumps(snapshot, sort_keys=True, ensure_ascii=True)
        )


def _committed_plan_blocker(item: dict[str, Any]) -> str | None:
    """Reject committed plans that cannot run inside the verified lane."""

    plan = item.get("plan")
    if not isinstance(plan, dict):
        return None
    steps = [str(value) for value in plan.get("steps") or []]
    criteria = [
        str(value) for value in plan.get("acceptance_criteria") or []
    ]
    if any(
        _requires_external_github_action(statement)
        for statement in [*steps, *criteria]
    ):
        return (
            "Committed plan requires an external GitHub action, but workers "
            "may only produce and verify local repository changes."
        )

    resources = item.get("resource_requirements")
    if not isinstance(resources, dict):
        resources = {}
    available_gpus = int(resources.get("gpu_count") or 0)
    required_gpu_counts = [
        int(match.group("count"))
        for criterion in criteria
        for match in _GPU_COUNT_RE.finditer(criterion)
    ]
    if required_gpu_counts and max(required_gpu_counts) > available_gpus:
        return (
            "Committed acceptance criteria require "
            f"{max(required_gpu_counts)} GPUs, but the verified worker lane "
            f"provides {available_gpus}."
        )

    required_architectures = {
        match.group(0).casefold()
        for criterion in criteria
        for match in _GPU_ARCHITECTURE_RE.finditer(criterion)
    }
    available_architecture = str(
        resources.get("gpu_architecture") or ""
    ).casefold()
    if required_architectures and (
        not available_architecture
        or available_architecture not in required_architectures
    ):
        required = ", ".join(sorted(required_architectures))
        available = available_architecture or "none"
        return (
            "Committed acceptance criteria require hardware "
            f"{required}, but the verified worker lane provides {available}."
        )
    return None


def _planned_gpu_count(item: dict[str, Any]) -> int:
    """Return the verified GPU requirement projected for planned Work."""

    resources = item.get("resource_requirements")
    if not isinstance(resources, dict):
        return 0
    return max(int(resources.get("gpu_count") or 0), 0)


def _projected_gpu_capacity(
    capacity: dict[str, Any],
) -> tuple[int, int] | None:
    """Read active/global GPU capacity, tolerating legacy test projections."""

    gpu_capacity = capacity.get("gpus")
    if not isinstance(gpu_capacity, dict):
        return None
    active = max(int(gpu_capacity.get("active") or 0), 0)
    maximum = max(int(gpu_capacity.get("maximum") or 0), 0)
    return active, maximum


def _permanent_gpu_capacity_blocker(
    item: dict[str, Any],
    capacity: dict[str, Any],
) -> str | None:
    """Explain a GPU request that can never fit the configured global pool."""

    projected = _projected_gpu_capacity(capacity)
    if projected is None:
        return None
    _active, maximum = projected
    required = _planned_gpu_count(item)
    if required <= maximum:
        return None
    return (
        f"Verified Work requires {required} GPUs, but configured global "
        f"capacity provides at most {maximum}."
    )


def _temporary_gpu_capacity_available(
    item: dict[str, Any],
    capacity: dict[str, Any],
) -> bool:
    """Return whether Work fits now without treating saturation as failure."""

    required = _planned_gpu_count(item)
    if required == 0:
        return True
    projected = _projected_gpu_capacity(capacity)
    if projected is None:
        return True
    active, maximum = projected
    return active + required <= maximum


def _requires_external_github_action(statement: str) -> bool:
    """Return whether one plan statement positively requires remote action.

    Main Hermes is encouraged to repeat the worker's no-publish boundary in
    plans.  A keyword-only scan therefore turns safe statements such as
    ``without pushing or commenting on GitHub`` into false blockers.  Evaluate
    punctuation- and contrast-delimited clauses independently and ignore only
    matches that are explicitly negated in the same clause.
    """

    for clause in _PLAN_CLAUSE_SPLIT_RE.split(statement):
        for match in _EXTERNAL_GITHUB_ACTION_RE.finditer(clause):
            if _NEGATED_ACTION_RE.search(clause[: match.end()]):
                continue
            return True
    return False


def _capacity_retry_ready(item: dict[str, Any], now: datetime) -> bool:
    raw = item.get("retry_not_before")
    if raw is None:
        return True
    if isinstance(raw, datetime):
        retry_at = raw
    elif isinstance(raw, str):
        try:
            retry_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
    else:
        return False
    return retry_at <= now


def _worker_capacity_retry_delay(
    config: ProjectHermesConfig,
    *,
    execution_attempt: int,
) -> int:
    base = config.polling.worker_capacity_retry_base_seconds
    maximum = config.polling.worker_capacity_retry_max_seconds
    exponent = min(max(execution_attempt - 1, 0), 20)
    return min(base * (2**exponent), maximum)


def _is_manager_protocol_error(error: str) -> bool:
    """Separate malformed model actions from provider/runtime failures."""

    return any(marker in error for marker in _MANAGER_PROTOCOL_ERROR_MARKERS)


def _action_from_result(result: RuntimeResult | None) -> ProjectAction:
    if result is None:
        raise ValueError("Main Hermes returned no runtime result")
    if result.status is RuntimeStatus.FAILED:
        raise RuntimeError(result.error or "Main Hermes turn failed")
    raw = result.output.get("project_actions")
    if raw is None and result.final_response:
        payload = _json_object(result.final_response)
        if payload is not None:
            raw = payload.get("project_actions")
    if not isinstance(raw, list) or len(raw) != 1:
        raise ValueError("project_actions must contain exactly one action")
    entry = raw[0]
    if not isinstance(entry, dict):
        raise ValueError("project_actions entry must be an object")
    return ProjectAction.model_validate(_normalize_action_payload(entry))


def _normalize_action_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept the documented flat action and one strict wrapped variant."""

    if "action" in payload:
        return payload
    if len(payload) != 1:
        raise ValueError(
            "project action must be flat or contain exactly one supported "
            "action wrapper"
        )
    raw_kind, body = next(iter(payload.items()))
    try:
        kind = ProjectActionKind(raw_kind)
    except ValueError as exc:
        raise ValueError(f"unsupported project action wrapper: {raw_kind}") from exc
    if not isinstance(body, dict):
        raise ValueError(f"{kind.value} action wrapper must contain an object")
    if "action" in body:
        raise ValueError("wrapped project action cannot also contain action")
    return {"action": kind.value, **body}


def _json_object(value: str) -> dict[str, Any] | None:
    text = value.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        candidate = text[start : end + 1]
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parsed = _repair_missing_project_actions_array_closer(candidate)
    return parsed if isinstance(parsed, dict) else None


def _repair_missing_project_actions_array_closer(
    value: str,
) -> dict[str, Any] | None:
    """Repair only one missing closer on the top-level action array.

    Some OpenAI-compatible providers occasionally end an otherwise valid
    response with ``}}}`` instead of ``}}]}``. This scanner accepts only that
    single mismatch at the final top-level object closer, then re-parses and
    verifies the exact one-action protocol. Nested or multiply malformed JSON
    remains rejected.
    """

    stack: list[str] = []
    in_string = False
    escaped = False
    for index, character in enumerate(value):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
            continue
        if character in "[{":
            stack.append(character)
            continue
        if character not in "]}":
            continue
        expected = "[" if character == "]" else "{"
        if stack and stack[-1] == expected:
            stack.pop()
            continue
        if (
            character != "}"
            or stack != ["{", "["]
            or value[index + 1 :].strip()
        ):
            return None
        repaired = value[:index] + "]" + value[index:]
        try:
            parsed = json.loads(repaired)
        except json.JSONDecodeError:
            return None
        if (
            not isinstance(parsed, dict)
            or set(parsed) != {"project_actions"}
            or not isinstance(parsed["project_actions"], list)
            or len(parsed["project_actions"]) != 1
        ):
            return None
        return parsed
    return None


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "MainHermesProjectManager",
    "ProjectAction",
    "ProjectActionKind",
    "ProjectManagerAdvance",
]
