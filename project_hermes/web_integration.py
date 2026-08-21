"""Authenticated dashboard composition for ProjectHermes services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request

from project_hermes.accounting import (
    AccountingStore,
    open_accounting_store,
)
from project_hermes.artifacts import SqliteArtifactRegistry
from project_hermes.api import (
    ActionContext,
    ApiPrincipal,
    build_project_hermes_router,
)
from project_hermes.assurance import ReviewPacket, ReviewRole
from project_hermes.assurance_store import (
    AssuranceStore,
    open_assurance_store,
)
from project_hermes.candidate_ingest import CandidateIngestor
from project_hermes.candidate_review import InternalCandidateReviewService
from project_hermes.config import (
    ControlPlaneMode,
    ProjectHermesConfig,
    load_config,
)
from project_hermes.controller import ProjectHermesController
from project_hermes.discovery import GitHubClient
from project_hermes.execution import (
    ExecutionCoordinator,
    SqliteExecutionStore,
)
from project_hermes.handlers import (
    execution_action_handler,
    review_request_handler,
)
from project_hermes.issue_launcher import IssueLauncher
from project_hermes.issue_screening import IssueScreeningService
from project_hermes.kubernetes_jobs import (
    CodexKubernetesWorkerBackend,
    CodexWorkerKubernetesApi,
    KubernetesJobBackend,
)
from project_hermes.models import IssueTask, ProjectRole
from project_hermes.polling import IssuePollingService
from project_hermes.polling_store import (
    SqlitePollingStore,
    WorkItem,
    WorkStatus,
    open_polling_store,
)
from project_hermes.polling_supervisor import PollingSupervisor
from project_hermes.project_manager import MainHermesProjectManager
from project_hermes.publication import (
    InternalPullRequestCandidateStore,
    open_internal_pull_request_candidate_store,
)
from project_hermes.resource_managers import GpuPool
from project_hermes.release import VerifiedActiveRelease, verify_active_release
from project_hermes.repositories import GitHubRepositoryResolver
from project_hermes.runtime.codex import (
    CodexTaskRuntimeAdapter,
    TaskCodexSupervisor,
)
from project_hermes.runtime.base import RuntimeModelRoute
from project_hermes.runtime.hermes_agent import (
    HermesAgentFactory,
    build_hermes_runtime,
)
from project_hermes.runtime.registry import RuntimeRegistry
from project_hermes.sessions import (
    SessionCoordinator,
    SqliteRuntimeBindingStore,
    codex_escalation_handler,
)
from project_hermes.store import RunStore, open_run_store
from project_hermes.work_graph import (
    ActionKind,
    LifecycleStatus,
    WorkNode,
    WorkNodeKind,
)
from project_hermes.workspaces import GitWorkspaceManager


@dataclass
class ProductionWiringOverrides:
    """Explicit fake seams for production-composition tests only."""

    github_client: GitHubClient | None = None
    worker_api: CodexWorkerKubernetesApi | None = None
    hermes_runtime: Any | None = None


@dataclass
class ProjectHermesWebServices:
    """Long-lived services retained on the FastAPI application state."""

    config: ProjectHermesConfig
    runs: RunStore
    assurance: AssuranceStore
    accounting: AccountingStore
    candidates: InternalPullRequestCandidateStore
    polling: SqlitePollingStore
    controller: ProjectHermesController
    workspaces: GitWorkspaceManager
    sessions: SessionCoordinator
    execution: ExecutionCoordinator | None
    polling_service: IssuePollingService | None = None
    project_manager: MainHermesProjectManager | None = None
    supervisor: PollingSupervisor | None = None
    artifacts: SqliteArtifactRegistry | None = None
    active_release: VerifiedActiveRelease | None = None
    issue_launcher: IssueLauncher | None = None
    candidate_ingestor: CandidateIngestor | None = None
    candidate_reviewer: InternalCandidateReviewService | None = None
    issue_screener: IssueScreeningService | None = None

    def close(self) -> None:
        """Close adapters that own connection pools."""

        for service in (
            self.candidates,
            self.polling,
            self.accounting,
            self.assurance,
            self.runs,
        ):
            close = getattr(service, "close", None)
            if callable(close):
                close()


def mount_project_hermes(
    app: FastAPI,
    *,
    require_dashboard_token: Callable[[Request], None],
    config_path: str | Path | None = None,
    polling_store: SqlitePollingStore | None = None,
    test_mode: bool = False,
    wiring_overrides: ProductionWiringOverrides | None = None,
) -> ProjectHermesWebServices | None:
    """Compose and mount ProjectHermes under the dashboard auth middleware."""

    if polling_store is not None and not test_mode:
        raise ValueError(
            "polling_store injection requires the explicit test_mode seam"
        )
    if wiring_overrides is not None and test_mode:
        raise ValueError(
            "production wiring overrides cannot be combined with test_mode"
        )
    resolved_config = _resolve_config_path(config_path)
    if resolved_config is None:
        return None
    config = load_config(resolved_config)
    runs = open_run_store(config.control_plane)
    assurance = open_assurance_store(config.control_plane)
    accounting = open_accounting_store(config.control_plane)
    candidates = open_internal_pull_request_candidate_store(
        config.control_plane
    )
    polling = polling_store or open_polling_store(config.polling)
    if config.polling.require_operator_selection:
        polling.hold_unselected_work_items()

    state_root = config.codex.runtime_root.resolve().parent
    workspaces = GitWorkspaceManager(
        state_root / "workspaces",
        command_timeout_seconds=(config.polling.git_command_timeout_seconds),
        fetch_attempts=config.polling.git_fetch_attempts,
        fetch_retry_backoff_seconds=(config.polling.git_fetch_retry_backoff_seconds),
    )
    bindings = SqliteRuntimeBindingStore(state_root / "runtime-bindings.db")
    runtimes = RuntimeRegistry()
    if config.codex.enabled:
        runtimes.register(
            CodexTaskRuntimeAdapter(TaskCodexSupervisor(config))
        )
    sessions = SessionCoordinator(
        runtimes,
        bindings,
        runs,
        accounting=accounting,
    )

    artifacts: SqliteArtifactRegistry | None = None
    active_release: VerifiedActiveRelease | None = None
    issue_launcher: IssueLauncher | None = None
    candidate_ingestor: CandidateIngestor | None = None
    candidate_reviewer: InternalCandidateReviewService | None = None
    issue_screener: IssueScreeningService | None = None
    polling_service: IssuePollingService | None = None
    project_manager: MainHermesProjectManager | None = None
    supervisor: PollingSupervisor | None = None
    repository_resolver: GitHubRepositoryResolver | None = None
    github_client: GitHubClient | None = None
    if test_mode:
        execution = _build_test_execution(config, accounting)
    else:
        (
            execution,
            artifacts,
            active_release,
        ) = _build_production_execution(
            config,
            accounting,
            state_root=state_root,
            overrides=wiring_overrides,
        )
        github_client = (
            wiring_overrides.github_client
            if wiring_overrides is not None
            and wiring_overrides.github_client is not None
            else GitHubClient(
                max_retries=config.polling.github_max_retries,
                timeout_seconds=(
                    config.polling.github_request_timeout_seconds
                ),
            )
        )
        repository_resolver = GitHubRepositoryResolver(
            github_client,
            repositories=tuple(
                repository.repository
                for repository in config.polling.repositories
                if repository.enabled
            ),
        )
    handlers: dict[ActionKind, Any] = {
        ActionKind.REQUEST_REVIEW: review_request_handler(
            assurance,
            schedule=lambda packet: _schedule_reviews(
                packet,
                runs=runs,
                assurance=assurance,
                model_route=config.hermes.route_for_review().model_dump(
                    mode="json"
                ),
            ),
        )
    }
    if execution is not None:
        handlers[ActionKind.REQUEST_EXECUTION] = execution_action_handler(
            execution,
            workspace_lookup=workspaces.get,
            workspace_digest=workspaces.candidate_digest,
            run_id_lookup=lambda task_id: runs.get_run_for_task(
                task_id
            ).run_id,
        )

    escalation = (
        codex_escalation_handler(
            sessions,
            bindings,
            assurance,
            config,
        )
        if config.codex.enabled
        else None
    )
    controller = ProjectHermesController(
        runs,
        assurance,
        action_handlers=handlers,
        escalation_handler=escalation,
    )
    if not test_mode:
        assert execution is not None
        assert artifacts is not None
        assert active_release is not None
        assert repository_resolver is not None
        assert github_client is not None
        issue_launcher = IssueLauncher(
            config,
            repository_resolver,
            workspaces,
            artifacts,
            execution,
            active_release,
        )
        candidate_ingestor = CandidateIngestor(
            execution.store,
            runs,
            candidates,
            artifact_archive_root=(
                config.kubernetes_jobs.artifact_archive_root
            ),
        )
        if (
            wiring_overrides is not None
            and wiring_overrides.hermes_runtime is not None
        ):
            runtime = wiring_overrides.hermes_runtime
        else:
            agent_factory = HermesAgentFactory(config)
            runtime = build_hermes_runtime(config, factory=agent_factory)
            candidate_reviewer = InternalCandidateReviewService(
                config,
                runs,
                candidates,
                agent_factory,
            )
            if config.polling.issue_screening_enabled:
                issue_screener = IssueScreeningService(
                    config,
                    polling,
                    agent_factory,
                )
        polling_service = IssuePollingService(
            github_client,
            polling,
            config.polling,
        )
        project_manager = MainHermesProjectManager(
            polling,
            runtime,
            controller,
            runs,
            config,
            baseline_resolver=repository_resolver,
            launcher=issue_launcher,
            candidates=candidates,
            execution=execution,
            candidate_ingestor=candidate_ingestor,
            model_route=RuntimeModelRoute.model_validate(
                config.hermes.model_profile.model_dump()
            ),
        )
        supervisor = PollingSupervisor(
            polling_service,
            project_manager,
            loop_interval_seconds=config.hermes.loop_interval_seconds,
            reviewer=candidate_reviewer,
            screener=issue_screener,
            reviewer_error_retry_seconds=(
                config.polling.manager_error_retry_seconds
            ),
        )

    def authenticate(request: Request) -> ApiPrincipal:
        require_dashboard_token(request)
        raw_role = request.headers.get(
            "X-Project-Hermes-Role",
            ProjectRole.OPERATOR.value,
        )
        try:
            role = ProjectRole(raw_role)
        except ValueError as exc:
            raise HTTPException(
                status_code=403,
                detail="invalid ProjectHermes role",
            ) from exc
        subject = request.headers.get("X-Project-Hermes-Session", "").strip()
        if not subject:
            subject = _dashboard_subject(request) or "dashboard-operator"
        return ApiPrincipal(subject=subject, role=role)

    def action_context(run_id: str) -> ActionContext:
        task = runs.get_task(run_id)
        return _action_context(task, config=config, workspaces=workspaces)

    app.include_router(
        build_project_hermes_router(
            controller,
            runs,
            authenticate=authenticate,
            action_context=action_context,
            accounting=accounting,
            assurance=assurance,
            candidates=candidates,
            polling=polling,
            polling_trigger=(
                polling_service.run_now
                if polling_service is not None
                else None
            ),
            supervisor_status=(
                (lambda: supervisor.status)
                if supervisor is not None
                else None
            ),
            issue_selector=(
                lambda candidate_id, subject: polling.select_candidate_for_work(
                    candidate_id,
                    selected_by=subject,
                    max_pending_work_items=(
                        config.polling.max_pending_work_items
                    ),
                    require_screening_select=(
                        config.polling.require_screening_select_for_operator
                    ),
                )
                if config.polling.require_operator_selection
                else None
            ),
            issue_rescreener=(
                lambda candidate_id, subject, reason, evidence: (
                    issue_screener.rescreen_candidate(
                        candidate_id,
                        requested_by=subject,
                        reason=reason,
                        probe_evidence=evidence,
                    )
                )
                if issue_screener is not None
                else None
            ),
            work_item_retrier=(
                lambda work_item_id, reason, subject: _retry_work_item(
                    polling,
                    work_item_id,
                    reason=reason,
                    requested_by=subject,
                    max_review_attempts=(
                        config.polling.max_review_execution_attempts
                    ),
                )
            ),
            operator_selection_required=(
                config.polling.require_operator_selection
            ),
            screening_selection_required=(
                config.polling.require_screening_select_for_operator
            ),
        ),
        prefix="/api",
    )
    services = ProjectHermesWebServices(
        config=config,
        runs=runs,
        assurance=assurance,
        accounting=accounting,
        candidates=candidates,
        polling=polling,
        controller=controller,
        workspaces=workspaces,
        sessions=sessions,
        execution=execution,
        polling_service=polling_service,
        project_manager=project_manager,
        supervisor=supervisor,
        artifacts=artifacts,
        active_release=active_release,
        issue_launcher=issue_launcher,
        candidate_ingestor=candidate_ingestor,
        candidate_reviewer=candidate_reviewer,
        issue_screener=issue_screener,
    )
    app.state.project_hermes = services
    return services


def _retry_work_item(
    polling: SqlitePollingStore,
    work_item_id: str,
    *,
    reason: str,
    requested_by: str,
    max_review_attempts: int,
) -> WorkItem:
    """Route an operator retry only to an explicitly supported boundary."""

    current = polling.get_work_item(work_item_id)
    if current.status is WorkStatus.BLOCKED:
        if current.current_step == "Controller run ended CANCELLED.":
            return polling.retry_cancelled_work_item(
                work_item_id,
                reason=reason,
            )
        if (
            current.current_step
            == "Independent review revision budget was exhausted."
        ):
            return polling.retry_miscounted_review_budget_work_item(
                work_item_id,
                reason=reason,
                requested_by=requested_by,
                max_review_attempts=max_review_attempts,
            )
        return polling.retry_policy_blocked_work_item(
            work_item_id,
            reason=reason,
            requested_by=requested_by,
        )
    if current.status is WorkStatus.FAILED:
        if current.current_step == "Worker launch failed.":
            return polling.retry_failed_launch_work_item(
                work_item_id,
                reason=reason,
            )
        return polling.retry_failed_execution_work_item(
            work_item_id,
            reason=reason,
            requested_by=requested_by,
        )
    raise ValueError("Work item is not at a supported retry boundary")


def _resolve_config_path(value: str | Path | None) -> Path | None:
    configured = str(
        value or os.environ.get("PROJECT_HERMES_CONFIG", "")
    ).strip()
    candidates = [Path(configured)] if configured else [
        Path.cwd() / "project-hermes.yaml",
        Path(__file__).resolve().parents[1] / "project-hermes.yaml",
    ]
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file():
            return resolved
    if configured:
        raise FileNotFoundError(
            f"ProjectHermes config does not exist: {candidates[0]}"
        )
    return None


def _build_test_execution(
    config: ProjectHermesConfig,
    accounting: AccountingStore,
) -> ExecutionCoordinator | None:
    if not config.kubernetes_jobs.enabled:
        return None
    return ExecutionCoordinator(
        SqliteExecutionStore(config.control_plane.sqlite_path),
        KubernetesJobBackend(config.kubernetes_jobs),
        artifact_lookup=lambda _request_id: None,
        gpu_pool=GpuPool(
            [device.gpu_id for device in config.gpu_devices]
        ),
        accounting=accounting,
        max_jobs=config.polling.max_global_jobs,
        max_gpus=config.polling.max_global_gpus,
    )


def _build_production_execution(
    config: ProjectHermesConfig,
    accounting: AccountingStore,
    *,
    state_root: Path,
    overrides: ProductionWiringOverrides | None,
) -> tuple[
    ExecutionCoordinator,
    SqliteArtifactRegistry,
    VerifiedActiveRelease,
]:
    if config.control_plane.mode is not ControlPlaneMode.SQLITE:
        raise NotImplementedError(
            "production worker execution requires a registered non-SQLite "
            "ExecutionStore and ArtifactRegistry adapter"
        )
    if not config.codex.enabled:
        raise ValueError("production ProjectHermes requires codex.enabled")
    if not config.kubernetes_jobs.enabled:
        raise ValueError(
            "production ProjectHermes requires kubernetes_jobs.enabled"
        )
    expected_digest = os.environ.get(
        "PROJECT_HERMES_RELEASE_DIGEST",
        "",
    ).strip()
    if not expected_digest:
        raise RuntimeError(
            "PROJECT_HERMES_RELEASE_DIGEST is required for production wiring"
        )
    active_release = verify_active_release(
        config.kubernetes_jobs.release_root,
        config.kubernetes_jobs.active_release_state_path,
        expected_digest=expected_digest,
    )
    artifacts = SqliteArtifactRegistry(
        state_root / "artifact-registry.db",
        source_bundle_root=config.kubernetes_jobs.source_bundle_root,
    )
    execution_store = SqliteExecutionStore(config.control_plane.sqlite_path)
    backend = CodexKubernetesWorkerBackend(
        config.kubernetes_jobs,
        release_digest=active_release.record.active_release_digest,
        api=(
            overrides.worker_api
            if overrides is not None
            else None
        ),
    )
    execution = ExecutionCoordinator(
        execution_store,
        backend,
        artifact_lookup=artifacts.get,
        gpu_pool=GpuPool(config.gpu_devices),
        accounting=accounting,
        max_jobs=config.polling.max_global_jobs,
        max_gpus=config.polling.max_global_gpus,
    )
    return execution, artifacts, active_release


def _schedule_reviews(
    packet: ReviewPacket,
    *,
    runs: RunStore,
    assurance: AssuranceStore,
    model_route: dict[str, Any],
) -> None:
    run = runs.get_run_for_task(packet.task_id)
    history = assurance.review_history(
        packet.task_id,
        goal_revision=packet.goal_revision,
    )
    review_cycle = (
        max(
            (review.review_cycle or 0 for review in history),
            default=0,
        )
        + 1
    )
    graph = runs.load_graph(run.run_id)
    for role in ReviewRole:
        node_id = (
            f"review-r{packet.goal_revision}-c{review_cycle}-"
            f"{role.value}"
        )
        if node_id not in graph.nodes:
            node = WorkNode(
                node_id=node_id,
                run_id=run.run_id,
                kind=WorkNodeKind.REVIEW,
                capability="review-candidate",
                requested_role=ProjectRole.REVIEWER,
                payload={
                    "review_role": role.value,
                    "review_cycle": review_cycle,
                    "packet_id": packet.packet_id,
                    "packet_digest": packet.content_hash,
                    "model_route": model_route,
                },
                idempotency_key=(
                    f"review:{packet.goal_revision}:{review_cycle}:"
                    f"{role.value}"
                ),
            )
            runs.add_node(node)
            graph.nodes[node_id] = node
        current = runs.load_graph(run.run_id).nodes[node_id]
        if current.status is LifecycleStatus.DISCOVERED:
            runs.queue_node(run.run_id, node_id)


def _action_context(
    task: IssueTask,
    *,
    config: ProjectHermesConfig,
    workspaces: GitWorkspaceManager,
) -> ActionContext:
    repository_roots: dict[str, Path] = {}
    worktree_roots: dict[str, Path] = {}
    for lease in workspaces.active_for_task(task.task_id):
        repository_roots[lease.repository] = Path(lease.mirror_path)
        worktree_roots[lease.repository] = Path(lease.worktree_path)
    repositories = {item.repository for item in task.repositories}
    project_root = config.project_root.resolve()
    if len(repositories) == 1 and project_root.is_dir():
        repository_roots.setdefault(next(iter(repositories)), project_root)
    return ActionContext(
        repository_roots=repository_roots,
        worktree_roots=worktree_roots,
    )


def _dashboard_subject(request: Request) -> str | None:
    session = getattr(request.state, "session", None)
    if session is None:
        return None
    if isinstance(session, dict):
        for key in ("subject", "user_id", "id", "login"):
            value = session.get(key)
            if value:
                return str(value)
        return None
    for key in ("subject", "user_id", "id", "login"):
        value = getattr(session, key, None)
        if value:
            return str(value)
    return None
