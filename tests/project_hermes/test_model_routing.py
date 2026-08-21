from __future__ import annotations

from pathlib import Path

import pytest

from project_hermes.config import ModelReasoningMode, ProjectHermesConfig
from project_hermes.models import IssueTask, ProjectRole
from project_hermes.runtime.base import (
    RuntimeHandle,
    RuntimeModelRoute,
    RuntimeRequest,
    RuntimeStatus,
)
from project_hermes.runtime.registry import RuntimeRegistry
from project_hermes.sessions import (
    SessionCoordinator,
    SqliteRuntimeBindingStore,
)
from project_hermes.store import SqliteRunStore


class _FakeCodexRuntime:
    name = "codex-task"

    def __init__(self) -> None:
        self.requests: list[RuntimeRequest] = []

    def start(self, request: RuntimeRequest) -> RuntimeHandle:
        self.requests.append(request)
        return RuntimeHandle(
            runtime_name=self.name,
            session_id=request.session_id or "missing-session",
            task_id=request.task_id,
            status=RuntimeStatus.IDLE,
            model_route=request.model_route,
            runtime_generation=request.runtime_generation,
            worktree_path=request.cwd,
        )

    def close(self, handle: RuntimeHandle) -> RuntimeHandle:
        return handle.model_copy(update={"status": RuntimeStatus.CLOSED})

    def events(
        self,
        handle: RuntimeHandle,
        *,
        after_sequence: int = 0,
    ) -> tuple[object, ...]:
        del handle, after_sequence
        return ()

    def result(self, handle: RuntimeHandle) -> None:
        del handle
        return None


def _route(config: ProjectHermesConfig, name: str) -> RuntimeModelRoute:
    return RuntimeModelRoute.model_validate(config.codex.route(name).model_dump())


def test_default_profiles_use_deepseek_plan_codex_and_official_review() -> None:
    config = ProjectHermesConfig()

    assert config.hermes.model_profile.model == "deepseek-v4-pro"
    assert config.hermes.model_profile.model_provider == "deepseek"
    assert config.hermes.model_profile.provider_endpoint == (
        "https://api.deepseek.com/v1"
    )
    reviewer = config.hermes.route_for_review()
    assert reviewer.model == "MiniMax-M3"
    assert reviewer.model_provider == "minimax-cn"
    assert reviewer.provider_endpoint == "https://api.minimaxi.com/v1"
    assert config.hermes.route_for_worker_handoff() == config.hermes.model_profile
    primary = config.codex.route(config.codex.primary_profile)
    escalated = config.codex.route(config.codex.escalation_profile)
    assert (primary.model, primary.provider_wire_api) == (
        "deepseek-v4-pro",
        "responses",
    )
    assert (escalated.model, escalated.provider_wire_api) == (
        "deepseek-v4-pro",
        "responses",
    )
    assert primary.model_provider == escalated.model_provider == "deepseek"
    assert primary.provider_endpoint == escalated.provider_endpoint == (
        "https://api.deepseek.com/v1"
    )
    assert primary.reasoning_mode is ModelReasoningMode.PROVIDER_DEFAULT
    assert escalated.reasoning_mode is ModelReasoningMode.PROVIDER_DEFAULT
    assert primary.reasoning_effort is None
    assert escalated.reasoning_effort is None
    assert primary.context_window == escalated.context_window == 1_000_000
    assert {
        config.hermes.model_profile.provider_api_key_env,
        reviewer.provider_api_key_env,
        primary.provider_api_key_env,
        escalated.provider_api_key_env,
    } == {"DEEPSEEK_API_KEY", "MINIMAX_CN_API_KEY"}


def test_main_hermes_can_use_a_route_separate_from_worker_handoff() -> None:
    raw = ProjectHermesConfig().model_dump(mode="python")
    raw["hermes"]["model_profile"] = {
        "profile": "hermes",
        "model": "deepseek-v4-pro",
        "model_provider": "deepseek",
        "provider_endpoint": "https://api.deepseek.com/v1",
        "provider_api_key_env": "DEEPSEEK_API_KEY",
        "provider_wire_api": "chat",
        "reasoning_mode": "provider_default",
        "reasoning_effort": None,
        "context_window": None,
    }
    raw["hermes"]["worker_handoff_profile"] = {
        "profile": "worker-handoff",
        "model": "deepseek-v4-pro",
        "model_provider": "digitalocean",
        "provider_endpoint": "https://inference.do-ai.run/v1",
        "provider_api_key_env": "MODEL_ACCESS_KEY",
        "provider_wire_api": "chat",
        "reasoning_mode": "provider_default",
        "reasoning_effort": None,
        "context_window": None,
    }

    config = ProjectHermesConfig.model_validate(raw)

    assert config.hermes.route_for_review().model_provider == "minimax-cn"
    assert config.hermes.model_profile.model_provider == "deepseek"
    handoff = config.hermes.route_for_worker_handoff()
    assert handoff.model_provider == "digitalocean"
    assert handoff.provider_api_key_env == "MODEL_ACCESS_KEY"


def test_codex_profiles_reject_effort_not_supported_by_pinned_cli() -> None:
    raw = ProjectHermesConfig().model_dump(mode="python")
    raw["codex"]["model_profiles"]["primary"]["reasoning_effort"] = "max"

    with pytest.raises(ValueError, match="supported by the pinned CLI"):
        ProjectHermesConfig.model_validate(raw)


def test_codex_reprovision_is_fresh_same_worktree_and_one_time(
    tmp_path: Path,
    issue_task: IssueTask,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    config = ProjectHermesConfig()
    runs = SqliteRunStore(tmp_path / "runs.db")
    runs.create_run(issue_task, run_id="run-1")
    bindings = SqliteRuntimeBindingStore(tmp_path / "bindings.db")
    runtime = _FakeCodexRuntime()
    registry = RuntimeRegistry()
    registry.register(runtime)
    coordinator = SessionCoordinator(registry, bindings, runs)
    primary, _ = coordinator.start(
        "run-1",
        runtime.name,
        RuntimeRequest(
            request_id="primary",
            task_id=issue_task.task_id,
            role=ProjectRole.CODEX,
            prompt="Implement the issue.",
            cwd=str(worktree),
            model_route=_route(config, config.codex.primary_profile),
        ),
    )

    escalated, _ = coordinator.reprovision_codex(
        primary.binding_id,
        RuntimeRequest(
            request_id="escalated",
            task_id=issue_task.task_id,
            role=ProjectRole.CODEX,
            prompt="Address the review.",
            cwd=str(worktree),
            model_route=_route(config, config.codex.escalation_profile),
            runtime_generation=1,
            review_cycle=2,
        ),
        locked_issue_contract=issue_task.model_dump_json(),
        review_findings=["No progress against correctness."],
        escalation_explanation="Two completed cycles made no progress.",
    )

    closed_primary = bindings.get(primary.binding_id)
    assert closed_primary.handle.status is RuntimeStatus.CLOSED
    assert escalated.handle.session_id != primary.handle.session_id
    assert escalated.handle.worktree_path == str(worktree)
    assert escalated.handle.runtime_generation == 1
    assert escalated.handle.model_route.model == "deepseek-v4-pro"
    assert "locked_issue_contract" in runtime.requests[-1].prompt
    with pytest.raises(RuntimeError, match="already closed"):
        coordinator.reprovision_codex(
            primary.binding_id,
            RuntimeRequest(
                request_id="escalated-again",
                task_id=issue_task.task_id,
                role=ProjectRole.CODEX,
                prompt="Retry escalation.",
                cwd=str(worktree),
                model_route=_route(config, config.codex.escalation_profile),
                runtime_generation=1,
                review_cycle=3,
            ),
            locked_issue_contract=issue_task.model_dump_json(),
            review_findings=["Still no progress."],
            escalation_explanation="A second escalation is forbidden.",
        )
