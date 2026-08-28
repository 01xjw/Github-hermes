"""Mountable authenticated FastAPI routes for the v2 control plane."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, TypeVar, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import Field, field_validator, model_validator

from project_hermes.accounting import (
    AccountingStore,
    summarize_accounting,
)
from project_hermes.assurance import ReviewRole
from project_hermes.assurance_store import AssuranceStore
from project_hermes.controller import ProjectHermesController
from project_hermes.models import (
    GoalRevision,
    IssueTask,
    ProjectRole,
    StrictModel,
)
from project_hermes.polling_store import (
    EnvironmentStatus,
    SqlitePollingStore,
    WorkItem,
    WorkStatus,
)
from project_hermes.publication import (
    InternalCandidateCheckStatus,
    InternalCandidateLockState,
    InternalPullRequestCandidate,
    InternalPullRequestCandidateStore,
)
from project_hermes.redaction import redact_text
from project_hermes.store import RunStore
from project_hermes.work_graph import (
    ActionRequest,
    LifecycleStatus,
    WorkNode,
)


class ApiPrincipal(StrictModel):
    """Authenticated identity supplied by the existing control plane."""

    subject: str
    role: ProjectRole


class ActionContext(StrictModel):
    """Server-owned action context that never comes from agent payloads."""

    repository_roots: dict[str, Path] = Field(default_factory=dict)
    worktree_roots: dict[str, Path] = Field(default_factory=dict)
    approved_revisions: list[GoalRevision] = Field(default_factory=list)
    knowledge_commit_verified: bool = False
    operator_publish_approved: bool = False


class CreateRunPayload(StrictModel):
    schema_version: Literal["create-run-request.v1"] = "create-run-request.v1"
    task: IssueTask
    run_id: str | None = None


class ClaimNodePayload(StrictModel):
    schema_version: Literal["claim-node-request.v1"] = (
        "claim-node-request.v1"
    )
    role: ProjectRole
    lease_seconds: int = Field(default=300, ge=15, le=3600)


class TransitionNodePayload(StrictModel):
    schema_version: Literal["transition-node-request.v1"] = (
        "transition-node-request.v1"
    )
    target: LifecycleStatus
    output: dict[str, Any] | None = None


class RetryWorkItemPayload(StrictModel):
    """Audited operator request to retry one supported Work boundary."""

    schema_version: Literal["retry-work-item-request.v1"] = (
        "retry-work-item-request.v1"
    )
    reason: str = Field(min_length=1, max_length=1000)


class RescreenIssuePayload(StrictModel):
    """Audited operator request to append one screening revision."""

    schema_version: Literal["rescreen-issue-request.v1"] = (
        "rescreen-issue-request.v1"
    )
    reason: str = Field(min_length=1, max_length=1000)
    probe_evidence: list[str] = Field(min_length=1, max_length=20)

    @field_validator("probe_evidence")
    @classmethod
    def normalize_probe_evidence(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values))
        if any(not value for value in normalized):
            raise ValueError("probe evidence cannot contain empty entries")
        if any(len(value) > 2000 for value in normalized):
            raise ValueError("probe evidence entry exceeds 2000 characters")
        return normalized


class PublishInternalPullRequestPayload(StrictModel):
    """Explicit operator confirmation for one immutable publication."""

    schema_version: Literal["publish-internal-pr-request.v1"] = (
        "publish-internal-pr-request.v1"
    )
    confirmed_lock_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    confirm: Literal[True]


class PullRequestStatusReference(StrictModel):
    """One stable dashboard key mapped to a GitHub PR number or branch."""

    key: str = Field(min_length=1, max_length=200)
    repository: str = Field(
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
    )
    number: int | None = Field(default=None, ge=1)
    head_ref: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def exactly_one_identity(self) -> "PullRequestStatusReference":
        if (self.number is None) == (self.head_ref is None):
            raise ValueError("provide exactly one of number or head_ref")
        if self.head_ref is not None and (
            self.head_ref.startswith(("-", ".", "/"))
            or self.head_ref.endswith((".", "/"))
            or ".." in self.head_ref
            or "@{" in self.head_ref
            or any(
                character.isspace() or character in "~^:?*[\\"
                for character in self.head_ref
            )
        ):
            raise ValueError("head_ref is not a safe Git branch name")
        return self


class PullRequestStatusPayload(StrictModel):
    schema_version: Literal["pull-request-status-request.v1"] = (
        "pull-request-status-request.v1"
    )
    references: list[PullRequestStatusReference] = Field(
        min_length=1,
        max_length=100,
    )

    @field_validator("references")
    @classmethod
    def unique_reference_keys(
        cls,
        values: list[PullRequestStatusReference],
    ) -> list[PullRequestStatusReference]:
        keys = [value.key for value in values]
        if len(keys) != len(set(keys)):
            raise ValueError("pull request status keys must be unique")
        return values


PrincipalDependency = Callable[
    [Request],
    ApiPrincipal | Awaitable[ApiPrincipal],
]
ActionContextProvider = Callable[
    [str],
    ActionContext | Awaitable[ActionContext],
]
T = TypeVar("T")
PollingTrigger = Callable[[], Any]
SupervisorStatusProvider = Callable[[], Any]
SupervisorHealthProvider = Callable[[], dict[str, object]]
IssueSelector = Callable[[str, str], WorkItem]
IssueRescreener = Callable[[str, str, str, list[str]], Any]
WorkItemRetrier = Callable[[str, str, str], WorkItem]
CandidatePublisher = Callable[[InternalPullRequestCandidate, str], Any]
PullRequestStatusResolver = Callable[[list[dict[str, Any]]], Any]
ScreeningDecisionQuery = Literal["SELECT", "DEFER", "REJECT", "PENDING"]


def build_project_hermes_router(
    controller: ProjectHermesController,
    store: RunStore,
    *,
    authenticate: PrincipalDependency,
    action_context: ActionContextProvider,
    accounting: AccountingStore | None = None,
    assurance: AssuranceStore | None = None,
    candidates: InternalPullRequestCandidateStore | None = None,
    polling: SqlitePollingStore | None = None,
    polling_trigger: PollingTrigger | None = None,
    supervisor_status: SupervisorStatusProvider | None = None,
    supervisor_health: SupervisorHealthProvider | None = None,
    issue_selector: IssueSelector | None = None,
    issue_rescreener: IssueRescreener | None = None,
    work_item_retrier: WorkItemRetrier | None = None,
    candidate_publisher: CandidatePublisher | None = None,
    pull_request_status_resolver: PullRequestStatusResolver | None = None,
    operator_selection_required: bool = False,
    screening_selection_required: bool = False,
    polling_window_days: int = 30,
) -> APIRouter:
    """Build routes that require host-provided authentication and context."""

    router = APIRouter(prefix="/v2/project-hermes", tags=["project-hermes"])

    async def principal(request: Request) -> ApiPrincipal:
        return await _resolve(authenticate(request))

    async def context(run_id: str) -> ActionContext:
        return await _resolve(action_context(run_id))

    @router.get("/health")
    async def get_project_hermes_health(
        response: Response,
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, object]:
        del actor
        if supervisor_health is None:
            response.status_code = 503
            return {
                "ok": False,
                "reasons": ["ProjectHermes supervisor is not configured"],
            }
        payload = supervisor_health()
        if not bool(payload.get("ok")):
            response.status_code = 503
        return payload

    @router.get("/polling")
    async def get_polling_overview(
        repository_id: int | None = Query(default=None, ge=1),
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        del actor
        polling_store = _require_polling_store(polling)
        payload = polling_store.overview(repository_id=repository_id)
        if supervisor_status is not None:
            status = supervisor_status()
            dump = getattr(status, "model_dump", None)
            payload["supervisor"] = (
                dump(mode="json") if callable(dump) else status
            )
        else:
            payload["supervisor"] = None
        payload["operator_selection_required"] = (
            operator_selection_required
        )
        payload["screening_selection_required"] = (
            screening_selection_required
        )
        payload["polling_window_days"] = polling_window_days
        return payload

    @router.post("/github/pull-request-statuses")
    async def get_pull_request_statuses(
        payload: PullRequestStatusPayload,
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        del actor
        if pull_request_status_resolver is None:
            raise HTTPException(
                status_code=503,
                detail="GitHub pull request status is not configured",
            )
        try:
            statuses = await asyncio.to_thread(
                pull_request_status_resolver,
                [reference.model_dump() for reference in payload.references],
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=502,
                detail=redact_text(str(exc)),
            ) from exc
        return {"statuses": _serialize(statuses)}

    @router.post("/polling/run", status_code=201)
    async def run_polling_now(
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        if actor.role not in {
            ProjectRole.CONTROL_PLANE,
            ProjectRole.OPERATOR,
        }:
            raise HTTPException(
                status_code=403,
                detail="principal cannot trigger polling",
            )
        if polling_trigger is None:
            raise HTTPException(
                status_code=503,
                detail="polling trigger is not configured",
            )
        try:
            result = await asyncio.to_thread(polling_trigger)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail=redact_text(str(exc)),
            ) from exc
        dump = getattr(result, "model_dump", None)
        return dump(mode="json") if callable(dump) else _serialize(result)

    @router.get("/polling/runs")
    async def list_polling_runs(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        del actor
        polling_store = _require_polling_store(polling)
        items = polling_store.list_runs(limit=limit, offset=offset)
        return {
            "runs": [item.model_dump(mode="json") for item in items],
            "limit": limit,
            "offset": offset,
        }

    @router.get("/polling/runs/{run_id}/repositories")
    async def list_polling_run_repositories(
        run_id: str,
        repository_id: int | None = Query(default=None, ge=1),
        repository: str | None = Query(default=None),
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        del actor
        polling_store = _require_polling_store(polling)
        try:
            items = polling_store.list_run_repositories(
                run_id,
                repository_id=repository_id,
                repository=repository,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "repositories": [
                item.model_dump(mode="json") for item in items
            ],
            "total": len(items),
        }

    @router.get("/polling/repositories")
    async def list_polling_repositories(
        repository_id: int | None = Query(default=None, ge=1),
        repository: str | None = Query(default=None),
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        del actor
        polling_store = _require_polling_store(polling)
        items = polling_store.list_repositories(
            repository_id=repository_id,
            repository=repository,
        )
        return {
            "repositories": [
                item.model_dump(mode="json") for item in items
            ],
            "total": len(items),
        }

    @router.get("/polling/candidates")
    async def list_polling_candidates(
        repository_id: int | None = Query(default=None, ge=1),
        repository: str | None = Query(default=None),
        eligible: bool | None = Query(default=None),
        run_id: str | None = Query(default=None),
        search: str | None = Query(default=None, max_length=200),
        status: list[WorkStatus] | None = Query(default=None),
        screening_decision: list[ScreeningDecisionQuery] | None = Query(
            default=None
        ),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        del actor
        polling_store = _require_polling_store(polling)
        items = polling_store.list_candidates(
            repository_id=repository_id,
            repository=repository,
            eligible=eligible,
            run_id=run_id,
            search=search,
            work_statuses=status,
            screening_decisions=screening_decision,
            limit=limit,
            offset=offset,
        )
        return {
            "candidates": [item.model_dump(mode="json") for item in items],
            "total": polling_store.count_candidates(
                repository_id=repository_id,
                repository=repository,
                eligible=eligible,
                run_id=run_id,
                search=search,
                work_statuses=status,
                screening_decisions=screening_decision,
            ),
            "limit": limit,
            "offset": offset,
        }

    @router.get("/issues")
    async def list_issues(
        repository_id: int | None = Query(default=None, ge=1),
        repository: str | None = Query(default=None),
        eligible: bool | None = Query(default=None),
        run_id: str | None = Query(default=None),
        search: str | None = Query(default=None, max_length=200),
        status: list[WorkStatus] | None = Query(default=None),
        screening_decision: list[ScreeningDecisionQuery] | None = Query(
            default=None
        ),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        """Return the live Issue feed with its optional Work projection."""

        del actor
        polling_store = _require_polling_store(polling)
        items = polling_store.list_issue_feed(
            repository_id=repository_id,
            repository=repository,
            eligible=eligible,
            run_id=run_id,
            search=search,
            work_statuses=status,
            screening_decisions=screening_decision,
            limit=limit,
            offset=offset,
        )
        return {
            "issues": [
                {
                    "candidate": candidate.model_dump(mode="json"),
                    "work_item": (
                        _work_item_payload(work_item)
                        if work_item is not None
                        else None
                    ),
                    "screening": (
                        screening.model_dump(mode="json")
                        if screening is not None
                        else None
                    ),
                }
                for candidate, work_item, screening in items
            ],
            "total": polling_store.count_candidates(
                repository_id=repository_id,
                repository=repository,
                eligible=eligible,
                run_id=run_id,
                search=search,
                work_statuses=status,
                screening_decisions=screening_decision,
            ),
            "limit": limit,
            "offset": offset,
        }

    @router.get("/issues/{candidate_id}")
    async def get_issue(
        candidate_id: str,
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        del actor
        polling_store = _require_polling_store(polling)
        try:
            return _issue_detail_payload(
                polling_store,
                candidate_id,
                operator_selection_required=operator_selection_required,
                screening_selection_required=screening_selection_required,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/issues/{candidate_id}/select", status_code=201)
    async def select_issue(
        candidate_id: str,
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        if actor.role not in {
            ProjectRole.CONTROL_PLANE,
            ProjectRole.OPERATOR,
        }:
            raise HTTPException(
                status_code=403,
                detail="principal cannot select Issues for Work",
            )
        if not operator_selection_required or issue_selector is None:
            raise HTTPException(
                status_code=409,
                detail="operator Issue selection is not enabled",
            )
        try:
            await asyncio.to_thread(
                issue_selector,
                candidate_id,
                actor.subject,
            )
            polling_store = _require_polling_store(polling)
            return _issue_detail_payload(
                polling_store,
                candidate_id,
                operator_selection_required=True,
                screening_selection_required=screening_selection_required,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail=redact_text(str(exc)),
            ) from exc

    @router.post("/issues/{candidate_id}/rescreen", status_code=201)
    async def rescreen_issue(
        candidate_id: str,
        payload: RescreenIssuePayload,
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        if actor.role not in {
            ProjectRole.CONTROL_PLANE,
            ProjectRole.OPERATOR,
        }:
            raise HTTPException(
                status_code=403,
                detail="principal cannot request Issue re-screening",
            )
        if issue_rescreener is None:
            raise HTTPException(
                status_code=409,
                detail="operator Issue re-screening is not enabled",
            )
        try:
            await asyncio.to_thread(
                issue_rescreener,
                candidate_id,
                actor.subject,
                payload.reason,
                payload.probe_evidence,
            )
            polling_store = _require_polling_store(polling)
            return _issue_detail_payload(
                polling_store,
                candidate_id,
                operator_selection_required=operator_selection_required,
                screening_selection_required=screening_selection_required,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail=redact_text(str(exc)),
            ) from exc

    @router.get("/work-items")
    async def list_work_items(
        status: list[WorkStatus] | None = Query(default=None),
        repository_id: int | None = Query(default=None, ge=1),
        repository: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        del actor
        polling_store = _require_polling_store(polling)
        items = polling_store.list_work_items(
            statuses=status,
            repository_id=repository_id,
            repository=repository,
            limit=limit,
            offset=offset,
        )
        return {
            "work_items": [_work_item_payload(item) for item in items],
            "counts": polling_store.work_counts(
                repository_id=repository_id,
                repository=repository,
            ),
            "total": polling_store.count_work_items(
                statuses=status,
                repository_id=repository_id,
                repository=repository,
            ),
            "limit": limit,
            "offset": offset,
        }

    @router.get("/work-items/{work_item_id}")
    async def get_work_item(
        work_item_id: str,
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        del actor
        polling_store = _require_polling_store(polling)
        try:
            item = polling_store.get_work_item(work_item_id)
            candidate = polling_store.get_candidate(item.candidate_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        events = polling_store.list_work_events(
            work_item_id_value=work_item_id,
            limit=500,
        )
        screening = polling_store.get_current_screening(
            candidate.candidate_id
        )
        return {
            "work_item": _work_item_payload(item),
            "candidate": candidate.model_dump(mode="json"),
            "screening": (
                screening.model_dump(mode="json")
                if screening is not None
                else None
            ),
            "events": [event.model_dump(mode="json") for event in events],
        }

    @router.post("/work-items/{work_item_id}/retry")
    async def retry_work_item(
        work_item_id: str,
        payload: RetryWorkItemPayload,
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        if actor.role not in {
            ProjectRole.CONTROL_PLANE,
            ProjectRole.OPERATOR,
        }:
            raise HTTPException(
                status_code=403,
                detail="principal cannot retry Work",
            )
        if work_item_retrier is None:
            raise HTTPException(
                status_code=503,
                detail="Work retry is not configured",
            )
        try:
            item = await asyncio.to_thread(
                work_item_retrier,
                work_item_id,
                payload.reason,
                actor.subject,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail=redact_text(str(exc)),
            ) from exc
        return {"work_item": _work_item_payload(item)}

    @router.post("/runs", status_code=201)
    async def create_run(
        payload: CreateRunPayload,
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        if actor.role not in {
            ProjectRole.CONTROL_PLANE,
            ProjectRole.OPERATOR,
        }:
            raise HTTPException(
                status_code=403,
                detail="principal cannot create task runs",
            )
        run = controller.create_task_run(
            payload.task,
            run_id=payload.run_id,
        )
        return run.model_dump(mode="json")

    @router.get("/runs/{run_id}")
    async def get_run(
        run_id: str,
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        del actor
        try:
            run = store.get_run(run_id)
            graph = store.load_graph(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "run": run.model_dump(mode="json"),
            "graph": graph.model_dump(mode="json"),
        }

    @router.get("/runs/{run_id}/events")
    async def list_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        del actor
        try:
            events = store.list_events(run_id, after_sequence=after)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "events": [
                event.model_dump(mode="json") for event in events
            ]
        }

    @router.get("/tasks/{task_id}/accounting")
    async def get_task_accounting(
        task_id: str,
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        del actor
        if accounting is None:
            raise HTTPException(
                status_code=503,
                detail="ProjectHermes accounting is not configured",
            )
        try:
            return task_accounting_payload(
                task_id,
                store=store,
                accounting=accounting,
                assurance=assurance,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/pull-request-candidates")
    async def list_pull_request_candidates(
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        del actor
        candidate_store = _require_candidate_store(candidates)
        items = candidate_store.list_candidates(
            limit=limit,
            offset=offset,
        )
        return {
            "candidates": [
                _candidate_summary_payload(candidate) for candidate in items
            ],
            "total": candidate_store.count(),
            "limit": limit,
            "offset": offset,
        }

    @router.get("/pull-request-candidates/{candidate_id}")
    async def get_pull_request_candidate(
        candidate_id: str,
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        del actor
        candidate_store = _require_candidate_store(candidates)
        try:
            candidate = candidate_store.get(candidate_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _candidate_detail_payload(candidate)

    @router.get("/pull-request-candidates/{candidate_id}/files/diff")
    async def get_pull_request_candidate_file_diff(
        candidate_id: str,
        path: str = Query(min_length=1),
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        del actor
        candidate_store = _require_candidate_store(candidates)
        try:
            candidate = candidate_store.get(candidate_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        file = next(
            (item for item in candidate.files if item.path == path),
            None,
        )
        if file is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown candidate file: {path}",
            )
        return {
            "candidate_id": candidate.candidate_id,
            "file": file.model_dump(mode="json", exclude={"diff"}),
            "diff": file.diff,
        }

    @router.post(
        "/pull-request-candidates/{candidate_id}/publish",
        status_code=201,
    )
    async def publish_pull_request_candidate(
        candidate_id: str,
        payload: PublishInternalPullRequestPayload,
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        if actor.role not in {
            ProjectRole.CONTROL_PLANE,
            ProjectRole.OPERATOR,
        }:
            raise HTTPException(
                status_code=403,
                detail="principal cannot publish pull requests",
            )
        if candidate_publisher is None:
            raise HTTPException(
                status_code=503,
                detail="ProjectHermes Draft PR publication is not configured",
            )
        candidate_store = _require_candidate_store(candidates)
        try:
            candidate = candidate_store.get(candidate_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if (
            candidate.lock_state
            is not InternalCandidateLockState.IMMUTABLE_APPROVED
            or candidate.lock_digest is None
        ):
            raise HTTPException(
                status_code=409,
                detail="candidate must be approved and immutable before publication",
            )
        if payload.confirmed_lock_digest != candidate.lock_digest:
            raise HTTPException(
                status_code=409,
                detail="candidate approval changed; review the locked candidate again",
            )
        try:
            result = await asyncio.to_thread(
                candidate_publisher,
                candidate,
                actor.subject,
            )
        except (PermissionError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail=redact_text(str(exc)),
            ) from exc
        return _serialize(result)

    @router.post("/runs/{run_id}/actions")
    async def submit_action(
        run_id: str,
        payload: ActionRequest,
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        if payload.requested_by is not actor.role:
            raise HTTPException(
                status_code=403,
                detail="action role does not match authenticated principal",
            )
        try:
            owned = await context(run_id)
            decision, result = controller.submit_action(
                run_id,
                payload,
                repository_roots=owned.repository_roots,
                worktree_roots=owned.worktree_roots,
                approved_revisions=owned.approved_revisions,
                knowledge_commit_verified=(
                    owned.knowledge_commit_verified
                ),
                operator_publish_approved=(
                    owned.operator_publish_approved
                ),
                actor_session_id=actor.subject,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail=redact_text(str(exc)),
            ) from exc
        return {
            "decision": decision.model_dump(mode="json"),
            "result": _serialize(result),
        }

    @router.post("/runs/{run_id}/nodes/claim")
    async def claim_node(
        run_id: str,
        payload: ClaimNodePayload,
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any] | None:
        if actor.role is not payload.role:
            raise HTTPException(
                status_code=403,
                detail="claim role does not match authenticated principal",
            )
        try:
            node = store.claim_ready_node(
                run_id,
                owner_token=actor.subject,
                role=actor.role,
                lease_seconds=payload.lease_seconds,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return node.model_dump(mode="json") if node else None

    @router.post("/runs/{run_id}/nodes/{node_id}/transition")
    async def transition_node(
        run_id: str,
        node_id: str,
        payload: TransitionNodePayload,
        actor: ApiPrincipal = Depends(principal),
    ) -> dict[str, Any]:
        try:
            current = store.load_graph(run_id).nodes[node_id]
            if (
                actor.role is not ProjectRole.CONTROL_PLANE
                and current.owner_token != actor.subject
            ):
                raise HTTPException(
                    status_code=403,
                    detail="principal does not own this node lease",
                )
            node = store.transition_node(
                run_id,
                node_id,
                payload.target,
                owner_token=(
                    current.owner_token
                    if actor.role is ProjectRole.CONTROL_PLANE
                    else actor.subject
                ),
                output=payload.output,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return node.model_dump(mode="json")

    return router


def task_accounting_payload(
    task_id: str,
    *,
    store: RunStore,
    accounting: AccountingStore,
    assurance: AssuranceStore | None = None,
) -> dict[str, Any]:
    """Build the shared API/Kanban accounting payload for one task."""

    run = store.get_run_for_task(task_id)
    records = accounting.list_records(task_id, run_id=run.run_id)
    summary = summarize_accounting(
        task_id=task_id,
        run_id=run.run_id,
        task_started_at=run.created_at,
        task_completed_at=run.completed_at,
        records=records,
    )
    review_cycles: list[dict[str, Any]] = []
    escalation: dict[str, Any] | None = None
    if assurance is not None:
        reviews = assurance.review_history(
            task_id,
            goal_revision=run.goal_revision,
        )
        review_cycles = [
            {
                "review_id": review.review_id,
                "review_cycle": review.review_cycle,
                "verdict": review.verdict.value,
                "progress": (
                    review.progress.value if review.progress else None
                ),
                "progress_explanation": review.progress_explanation,
                "assessed_goal_path_ids": review.assessed_goal_path_ids,
                "created_at": review.created_at.isoformat(),
            }
            for review in reviews
            if review.role is ReviewRole.COMPLETION_AUDITOR
        ]
        escalation = assurance.get_review_escalation(
            task_id,
            goal_revision=run.goal_revision,
        ).model_dump(mode="json")
    return {
        "summary": summary.model_dump(mode="json"),
        "review_cycles": review_cycles,
        "escalation": escalation,
    }


def _require_polling_store(
    store: SqlitePollingStore | None,
) -> SqlitePollingStore:
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="polling persistence is not configured",
        )
    return store


def _work_item_payload(item: WorkItem) -> dict[str, Any]:
    """Hide resource details until environment verification has passed."""

    payload = item.model_dump(mode="json")
    if item.environment_status is not EnvironmentStatus.VERIFIED:
        payload["resource_requirements"] = None
        payload["named_baseline"] = None
        payload["environment_verified_at"] = None
    return payload


def _issue_detail_payload(
    store: SqlitePollingStore,
    candidate_id: str,
    *,
    operator_selection_required: bool,
    screening_selection_required: bool,
) -> dict[str, Any]:
    candidate = store.get_candidate(candidate_id)
    work_item = store.get_work_item_for_candidate(candidate_id)
    events = (
        store.list_work_events(
            work_item_id_value=work_item.work_item_id,
            limit=500,
        )
        if work_item is not None
        else []
    )
    selection = store.operator_selection_state(
        candidate_id,
        require_screening_select=screening_selection_required,
    )
    screening = store.get_current_screening(candidate_id)
    screening_history = store.list_current_screening_history(candidate_id)
    return {
        "candidate": candidate.model_dump(mode="json"),
        "screening": (
            screening.model_dump(mode="json")
            if screening is not None
            else None
        ),
        "screening_history": [
            item.model_dump(mode="json") for item in screening_history
        ],
        "work_item": (
            _work_item_payload(work_item) if work_item is not None else None
        ),
        "events": [event.model_dump(mode="json") for event in events],
        "operator_selection_required": operator_selection_required,
        "screening_selection_required": screening_selection_required,
        "operator_selected": selection["selected"],
        "operator_selectable": (
            operator_selection_required and selection["selectable"]
        ),
    }


def _require_candidate_store(
    store: InternalPullRequestCandidateStore | None,
) -> InternalPullRequestCandidateStore:
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="ProjectHermes candidate persistence is not configured",
        )
    return store


def _candidate_summary_payload(
    candidate: InternalPullRequestCandidate,
) -> dict[str, Any]:
    completed_checks = sum(
        check.status is InternalCandidateCheckStatus.COMPLETED
        for check in candidate.checks
    )
    approvals = sum(
        review.verdict == "APPROVE" for review in candidate.reviews
    )
    return {
        "candidate_id": candidate.candidate_id,
        "task_id": candidate.task_id,
        "title": candidate.title,
        "repository": candidate.repository,
        "base_ref": candidate.base_ref,
        "head_ref": candidate.head_ref,
        "head_sha": candidate.head_sha,
        "lock_state": candidate.lock_state.value,
        "file_count": len(candidate.files),
        "commit_count": len(candidate.commits),
        "checks_completed": completed_checks,
        "checks_total": len(candidate.checks),
        "approvals": approvals,
        "review_count": len(candidate.reviews),
        "created_at": candidate.created_at.isoformat(),
        "updated_at": candidate.updated_at.isoformat(),
    }


def _candidate_detail_payload(
    candidate: InternalPullRequestCandidate,
) -> dict[str, Any]:
    payload = candidate.model_dump(mode="json", exclude={"files"})
    payload["files"] = [
        file.model_dump(mode="json", exclude={"diff"})
        for file in candidate.files
    ]
    if (
        candidate.lock_state
        is not InternalCandidateLockState.IMMUTABLE_APPROVED
    ):
        payload["post_lock_comparison"] = None
    return payload


async def _resolve(value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await cast(Awaitable[T], value)
    return cast(T, value)


def _serialize(value: Any) -> Any:
    if value is None:
        return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, WorkNode):
        return value.model_dump(mode="json")
    return value
