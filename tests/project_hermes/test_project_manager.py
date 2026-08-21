from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

import pytest

from project_hermes.assurance import CompletionLayer, ReviewRole
from project_hermes.assurance_store import SqliteAssuranceStore
from project_hermes.config import ProjectHermesConfig
from project_hermes.controller import ProjectHermesController
from project_hermes.issue_launcher import _worker_prompt
from project_hermes.polling_store import (
    PollingCandidate,
    PollingRepository,
    PollingRun,
    PollingRunStatus,
    SqlitePollingStore,
    WorkPlan,
    WorkResourceRequirements,
    WorkStatus,
)
from project_hermes.project_manager import (
    MainHermesProjectManager,
    ProjectActionKind,
    _action_from_result,
)
from project_hermes.publication import (
    InternalCandidateLockState,
    InternalPullRequestCandidate,
    InternalPullRequestCommit,
    InternalPullRequestFile,
    InternalPullRequestReview,
    SqliteInternalPullRequestCandidateStore,
)
from project_hermes.repository_skills import RepositorySkillReference
from project_hermes.runtime.base import (
    RuntimeEvent,
    RuntimeHandle,
    RuntimeRequest,
    RuntimeResult,
    RuntimeStatus,
)
from project_hermes.store import SqliteRunStore
from project_hermes.work_graph import LifecycleStatus


NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)


def test_polling_store_migrates_capacity_retry_projection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-polling.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE work_items (
                work_item_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                task_id TEXT,
                run_id TEXT,
                execution_id TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO work_items (
                work_item_id, status, task_id, run_id, execution_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "work-legacy",
                WorkStatus.RUNNING.value,
                "task-legacy",
                "run-legacy",
                "execution-legacy",
                NOW.isoformat(),
            ),
        )

    SqlitePollingStore(path)

    with sqlite3.connect(path) as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(work_items)")
        }
        attempt, retry_at = connection.execute(
            """
            SELECT execution_attempt, retry_not_before
            FROM work_items WHERE work_item_id = ?
            """,
            ("work-legacy",),
        ).fetchone()
    assert {"execution_attempt", "retry_not_before"} <= columns
    assert attempt == 1
    assert retry_at is None


class _Runtime:
    name = "hermes"

    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output
        self.requests: list[RuntimeRequest] = []
        self.handle: RuntimeHandle | None = None

    def start(self, request: RuntimeRequest) -> RuntimeHandle:
        self.requests.append(request)
        self.handle = RuntimeHandle(
            runtime_name=self.name,
            session_id=request.session_id or "project-manager-session",
            task_id=request.task_id,
            status=RuntimeStatus.IDLE,
            model_route=request.model_route,
        )
        return self.handle

    def resume(
        self,
        handle: RuntimeHandle,
        request: RuntimeRequest,
    ) -> RuntimeHandle:
        self.requests.append(request)
        self.handle = handle.model_copy(update={"status": RuntimeStatus.IDLE})
        return self.handle

    def reconnect(
        self,
        handle: RuntimeHandle,
        request: RuntimeRequest,
    ) -> RuntimeHandle:
        del request
        if handle.status is RuntimeStatus.STARTING:
            raise KeyError(handle.session_id)
        return handle

    def events(
        self,
        handle: RuntimeHandle,
        *,
        after_sequence: int = 0,
    ) -> Iterable[RuntimeEvent]:
        del handle, after_sequence
        return ()

    def result(self, handle: RuntimeHandle) -> RuntimeResult:
        return RuntimeResult(
            session_id=handle.session_id,
            status=RuntimeStatus.COMPLETED,
            output=self.output,
        )


class _StartFailureRuntime(_Runtime):
    def __init__(self) -> None:
        super().__init__({})
        self.start_calls = 0

    def start(self, request: RuntimeRequest) -> RuntimeHandle:
        self.start_calls += 1
        raise RuntimeError("HTTP 429: provider capacity exhausted")


class _Candidates:
    def count(self) -> int:
        return 0

    def list_candidates(self, *, limit: int, offset: int) -> list[Any]:
        del limit, offset
        return []


class _Unused:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected collaborator call: {name}")


class _Launcher:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, str]] = []

    def launch(self, task: Any, run_id: str) -> Any:
        self.calls.append((task, run_id))
        return SimpleNamespace(execution_id=f"execution-{len(self.calls)}")


class _FailingController:
    def create_task_run(self, task: Any, *, run_id: str) -> Any:
        del task, run_id
        raise ValueError("controller rejected the immutable task")


class _ExecutionProjection:
    def __init__(self, *, failure_kind: str) -> None:
        self.record = SimpleNamespace(
            observation=SimpleNamespace(
                result={"failure_kind": failure_kind},
            )
        )
        self.store = self

    def get(self, execution_id: str) -> Any:
        assert execution_id
        return self.record

    def reconcile_all(self) -> list[Any]:
        return []


def _save_reviewed_candidate(
    store: SqliteInternalPullRequestCandidateStore,
    *,
    task_id: str,
    candidate_id: str,
    completion_verdict: str = "MORE_EVIDENCE_REQUIRED",
    minimal_diff_verdict: str = "APPROVE",
) -> InternalPullRequestCandidate:
    candidate = InternalPullRequestCandidate(
        candidate_id=candidate_id,
        task_id=task_id,
        title="Repair the reviewed boundary",
        body="The Worker produced a bounded internal candidate.",
        repository="acme/alpha",
        base_ref="main",
        base_sha="a" * 40,
        head_ref=f"project-hermes/{task_id}",
        head_sha="b" * 40,
        commits=[
            InternalPullRequestCommit(
                sha="b" * 40,
                message="Repair the reviewed boundary",
                author_name="ProjectHermes Worker",
                authored_at=NOW,
            )
        ],
        files=[
            InternalPullRequestFile(
                path="src/boundary.py",
                status="M",
                added=1,
                removed=1,
                diff="@@ -1 +1 @@\n-old\n+new\n",
                necessity="Repairs the selected Issue boundary.",
            )
        ],
        lock_state=InternalCandidateLockState.APPROVAL_PENDING,
        created_at=NOW,
        updated_at=NOW,
    )
    store.save(candidate)
    verdicts = {
        ReviewRole.COMPLETION_AUDITOR: completion_verdict,
        ReviewRole.MINIMAL_DIFF_REVIEWER: minimal_diff_verdict,
    }
    for index, role in enumerate(ReviewRole, start=1):
        verdict = verdicts[role]
        store.append_review(
            candidate.candidate_id,
            InternalPullRequestReview(
                review_id=f"review-{candidate_id}-{index}",
                role=role.value,
                reviewer=f"minimax/session-{candidate_id}-{index}",
                verdict=verdict,
                summary=(
                    "Run the frozen regression and retain its output."
                    if verdict != "APPROVE"
                    else "The production diff is necessary and minimal."
                ),
                findings=(
                    ["Add bounded regression evidence to checks."]
                    if verdict != "APPROVE"
                    else []
                ),
                submitted_at=NOW + timedelta(seconds=index),
            ),
            expected_input_digest=candidate.review_input_digest(),
        )
    return store.get(candidate.candidate_id)


def _seed_store(path: Path) -> tuple[SqlitePollingStore, str]:
    store = SqlitePollingStore(path)
    store.configure_task(enabled=True, interval_seconds=900, now=NOW)
    run = PollingRun(
        run_id="poll-1",
        status=PollingRunStatus.RUNNING,
        cutoff=NOW,
        repositories_requested=1,
        started_at=NOW,
    )
    store.begin_run(run)
    store.upsert_repository(
        PollingRepository(
            repository_id=101,
            repository="acme/alpha",
            default_branch="main",
            html_url="https://github.com/acme/alpha",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    candidate = PollingCandidate(
        candidate_id="candidate-1",
        repository_id=101,
        repository="acme/alpha",
        issue_number=17,
        issue_url="https://github.com/acme/alpha/issues/17",
        title="Fix the boundary regression",
        body="The reproducer fails at the upper boundary.",
        labels=["bug"],
        author="reporter",
        comments=3,
        evidence_score=5,
        eligible=True,
        created_at=NOW,
        updated_at=NOW,
        first_seen_at=NOW,
        last_seen_at=NOW,
        latest_run_id=run.run_id,
    )
    _candidate, queued = store.save_candidate(candidate)
    assert queued
    work = store.list_work_items()[0]
    store.verify_work_environment(
        work.work_item_id,
        named_baseline="main@" + "a" * 40,
        resources=WorkResourceRequirements(
            cpu_request="2",
            cpu_limit="4",
            memory_request="4Gi",
            memory_limit="8Gi",
            gpu_count=1,
            gpu_architecture="gfx942",
            worker_model="worker-model",
        ),
        now=NOW,
    )
    return store, work.work_item_id


def _manager(
    tmp_path: Path,
    runtime: _Runtime,
) -> tuple[MainHermesProjectManager, SqlitePollingStore, str]:
    instructions = tmp_path / "AGENT.md"
    instructions.write_text(
        "Choose exactly one auditable Work action per turn.",
        encoding="utf-8",
    )
    store, work_item_id = _seed_store(tmp_path / "polling.db")
    config = ProjectHermesConfig(
        project_root=tmp_path,
        polling={
            "sqlite_path": store.path,
            "agent_instructions_path": instructions,
            "repositories": [{"repository": "acme/alpha"}],
            "max_active_work_items": 6,
            "max_global_jobs": 6,
        },
        codex={"enabled": False, "runtime_root": tmp_path / "runtime"},
    )
    manager = MainHermesProjectManager(
        store,
        runtime,
        _Unused(),
        _Unused(),
        config,
        baseline_resolver=_Unused(),
        launcher=_Unused(),
        candidates=_Candidates(),
    )
    return manager, store, work_item_id


def _planned_manager(
    tmp_path: Path,
    runtime: _Runtime,
    *,
    candidates: Any | None = None,
    max_review_execution_attempts: int = 3,
) -> tuple[
    MainHermesProjectManager,
    SqlitePollingStore,
    str,
    ProjectHermesController,
    _Launcher,
]:
    instructions = tmp_path / "AGENT.md"
    instructions.write_text(
        "Advance planned Work whenever worker capacity is available.",
        encoding="utf-8",
    )
    store, work_item_id = _seed_store(tmp_path / "polling.db")
    store.plan_work_item(
        work_item_id,
        WorkPlan(
            summary="Repair and validate the upper boundary.",
            steps=["Add a regression test.", "Fix the boundary."],
            acceptance_criteria=["The regression test passes."],
        ),
        now=NOW,
    )
    config = ProjectHermesConfig(
        project_root=tmp_path,
        polling={
            "sqlite_path": store.path,
            "agent_instructions_path": instructions,
            "repositories": [{"repository": "acme/alpha"}],
            "max_active_work_items": 6,
            "max_global_jobs": 6,
            "max_review_execution_attempts": max_review_execution_attempts,
        },
        codex={"enabled": False, "runtime_root": tmp_path / "runtime"},
    )
    runs = SqliteRunStore(tmp_path / "runs.db")
    controller = ProjectHermesController(
        runs,
        SqliteAssuranceStore(tmp_path / "assurance.db"),
    )
    launcher = _Launcher()
    manager = MainHermesProjectManager(
        store,
        runtime,
        controller,
        runs,
        config,
        baseline_resolver=_Unused(),
        launcher=launcher,
        candidates=candidates or _Candidates(),
    )
    return manager, store, work_item_id, controller, launcher


def _add_work(
    store: SqlitePollingStore,
    number: int,
    *,
    planned: bool,
    gpu_count: int = 1,
) -> str:
    candidate = PollingCandidate(
        candidate_id=f"candidate-{number}",
        repository_id=101,
        repository="acme/alpha",
        issue_number=number,
        issue_url=f"https://github.com/acme/alpha/issues/{number}",
        title=f"Repair boundary {number}",
        body="The reproducer demonstrates a bounded portable failure.",
        labels=["bug"],
        author="reporter",
        comments=2,
        evidence_score=5,
        eligible=True,
        created_at=NOW,
        updated_at=NOW,
        first_seen_at=NOW,
        last_seen_at=NOW,
        latest_run_id="poll-1",
    )
    _stored, queued = store.save_candidate(candidate)
    assert queued
    work = store.get_work_item_for_candidate(candidate.candidate_id)
    assert work is not None
    store.verify_work_environment(
        work.work_item_id,
        named_baseline="main@" + "a" * 40,
        resources=WorkResourceRequirements(
            cpu_request="2",
            cpu_limit="4",
            memory_request="4Gi",
            memory_limit="8Gi",
            gpu_count=gpu_count,
            gpu_architecture="gfx1100" if gpu_count else None,
            worker_model="worker-model",
        ),
        now=NOW,
    )
    if planned:
        store.plan_work_item(
            work.work_item_id,
            WorkPlan(
                summary=f"Repair and validate boundary {number}.",
                steps=["Add a regression test.", "Fix the boundary."],
                acceptance_criteria=["The focused regression passes."],
            ),
            now=NOW,
        )
    return work.work_item_id


def test_main_hermes_commits_one_plan_action_and_keeps_six_lane_capacity(
    tmp_path: Path,
) -> None:
    runtime = _Runtime({})
    manager, store, work_item_id = _manager(tmp_path, runtime)
    runtime.output = {
        "project_actions": [
            {
                "action": "plan",
                "work_item_id": work_item_id,
                "reason": "The Issue contains a bounded reproducer.",
                "plan": {
                    "summary": "Repair and validate the upper boundary.",
                    "steps": ["Add a regression test.", "Fix the boundary."],
                    "acceptance_criteria": ["The regression test passes."],
                    "risks": ["The adjacent lower boundary may regress."],
                },
            }
        ]
    }

    result = manager.advance(force=True, reconcile=False)

    assert result is not None
    assert result.action.action.value == "plan"
    assert store.get_work_item(work_item_id).status is WorkStatus.PLANNING
    assert manager._snapshot()["capacity"] == {
        "active": 0,
        "maximum": 6,
        "gpus": {"active": 0, "maximum": 6},
    }
    assert "Choose exactly one auditable" in runtime.requests[0].prompt


def test_main_hermes_planned_work_creates_a_valid_controller_task(
    tmp_path: Path,
) -> None:
    manager, store, work_item_id = _manager(tmp_path, _Runtime({}))
    planned = store.plan_work_item(
        work_item_id,
        WorkPlan(
            summary="Repair and validate the upper boundary.",
            steps=["Add a regression test.", "Fix the boundary."],
            acceptance_criteria=["The regression test passes."],
        ),
        now=NOW,
    )
    candidate = store.get_candidate(planned.candidate_id)
    task = manager._issue_task(
        planned,
        candidate,
        planned.named_baseline or "main@" + "a" * 40,
    )
    controller = ProjectHermesController(
        SqliteRunStore(tmp_path / "runs.db"),
        SqliteAssuranceStore(tmp_path / "assurance.db"),
    )

    run = controller.create_task_run(task, run_id="work-run-1")

    assert run.run_id == "work-run-1"
    assert task.goals[0].required_completion_layers == [
        CompletionLayer.IMPLEMENTED.value,
        CompletionLayer.GATE_VERIFIED.value,
        CompletionLayer.CI_REVIEWED.value,
    ]

    skill = RepositorySkillReference(
        repository="acme/alpha",
        name="solve-acme-alpha",
        digest="b" * 64,
    )
    prompt = _worker_prompt(
        task,
        execution_id_placeholder="execution-from-environment",
        repository="acme/alpha",
        baseline_ref="main",
        baseline_sha="a" * 40,
        source_candidate_digest="c" * 64,
        repository_skill=skill,
    )
    assert "$solve-acme-alpha" in prompt
    assert "Do not load a Skill for another repository" in prompt
    assert f"head_ref exactly to project-hermes/{task.task_id}" in prompt
    assert "files[] is the production-only review projection" in prompt


def test_main_hermes_scheduler_starts_planned_work_without_model_turn(
    tmp_path: Path,
) -> None:
    runtime = _Runtime({
        "project_actions": [
            {"action": "wait", "reason": "The controller will dispatch it."}
        ]
    })
    manager, store, work_item_id, _controller, launcher = _planned_manager(
        tmp_path,
        runtime,
    )
    _instructions, instructions_digest = manager._instructions()
    state = manager._state(instructions_digest)
    store.put_manager_state(
        state.model_copy(
            update={
                "last_error": "HTTP 429: Rate limit exceeded.",
                "updated_at": NOW,
            }
        ),
        expected_version=state.version,
    )

    result = manager.advance(force=True, reconcile=False)

    assert result is not None
    assert result.action.action is ProjectActionKind.START
    assert result.action.work_item_id == work_item_id
    assert result.outcome["ok"] is True
    assert store.get_work_item(work_item_id).status is WorkStatus.RUNNING
    assert len(launcher.calls) == 1
    assert runtime.requests == []
    assert store.get_manager_state().last_error is None


def test_main_hermes_scheduler_selects_oldest_planned_projection() -> None:
    snapshot = {
        "capacity": {
            "active": 0,
            "maximum": 6,
            "gpus": {"active": 0, "maximum": 6},
        },
        "work_items": [
            {"work_item_id": "work-oldest", "status": "planning", "plan": {}},
            {"work_item_id": "work-newer", "status": "planning", "plan": {}},
        ],
    }

    action = MainHermesProjectManager._scheduled_start(snapshot)

    assert action is not None
    assert action.action is ProjectActionKind.START
    assert action.work_item_id == "work-oldest"
    snapshot["capacity"]["active"] = 6
    assert MainHermesProjectManager._scheduled_start(snapshot) is None


def test_scheduler_waits_for_multi_gpu_work_but_starts_cpu_work() -> None:
    snapshot = {
        "capacity": {
            "active": 2,
            "maximum": 6,
            "gpus": {"active": 6, "maximum": 6},
        },
        "work_items": [
            {
                "work_item_id": "work-waiting-for-gpus",
                "status": "planning",
                "plan": {},
                "resource_requirements": {"gpu_count": 2},
            },
            {
                "work_item_id": "work-cpu",
                "status": "planning",
                "plan": {},
                "resource_requirements": {"gpu_count": 0},
            },
        ],
    }

    action = MainHermesProjectManager._scheduled_start(snapshot)

    assert action is not None
    assert action.action is ProjectActionKind.START
    assert action.work_item_id == "work-cpu"


def test_scheduler_leaves_temporarily_unavailable_multi_gpu_work_planned() -> None:
    snapshot = {
        "capacity": {
            "active": 2,
            "maximum": 6,
            "gpus": {"active": 4, "maximum": 6},
        },
        "work_items": [
            {
                "work_item_id": "work-four-gpu",
                "status": "planning",
                "plan": {},
                "resource_requirements": {"gpu_count": 4},
            }
        ],
    }

    assert MainHermesProjectManager._scheduled_start(snapshot) is None


def test_scheduler_blocks_work_larger_than_global_gpu_capacity() -> None:
    snapshot = {
        "capacity": {
            "active": 0,
            "maximum": 6,
            "gpus": {"active": 0, "maximum": 6},
        },
        "work_items": [
            {
                "work_item_id": "work-seven-gpu",
                "status": "planning",
                "plan": {},
                "resource_requirements": {"gpu_count": 7},
            }
        ],
    }

    action = MainHermesProjectManager._scheduled_start(snapshot)

    assert action is not None
    assert action.action is ProjectActionKind.BLOCK
    assert action.work_item_id == "work-seven-gpu"
    assert "requires 7 GPUs" in action.reason
    assert "at most 6" in action.reason


def test_main_hermes_scheduler_blocks_plan_beyond_verified_gpu_lane() -> None:
    snapshot = {
        "capacity": {"active": 0, "maximum": 6},
        "work_items": [
            {
                "work_item_id": "work-impossible-hardware",
                "status": "planning",
                "plan": {
                    "steps": ["Implement a focused local correction."],
                    "acceptance_criteria": ["Validate graph capture on 8x MI308X."],
                },
                "resource_requirements": {
                    "gpu_count": 1,
                    "gpu_architecture": "gfx1100",
                },
            }
        ],
    }

    action = MainHermesProjectManager._scheduled_start(snapshot)

    assert action is not None
    assert action.action is ProjectActionKind.BLOCK
    assert action.work_item_id == "work-impossible-hardware"
    assert "require 8 GPUs" in action.reason
    assert "provides 1" in action.reason


def test_main_hermes_scheduler_blocks_external_github_plan() -> None:
    snapshot = {
        "capacity": {"active": 0, "maximum": 6},
        "work_items": [
            {
                "work_item_id": "work-comment-only",
                "status": "planning",
                "plan": {
                    "steps": ["Post a response comment on the GitHub Issue."],
                    "acceptance_criteria": ["The response is posted on Issue #123."],
                },
                "resource_requirements": {
                    "gpu_count": 1,
                    "gpu_architecture": "gfx1100",
                },
            }
        ],
    }

    action = MainHermesProjectManager._scheduled_start(snapshot)

    assert action is not None
    assert action.action is ProjectActionKind.BLOCK
    assert action.work_item_id == "work-comment-only"
    assert "external GitHub action" in action.reason


def test_main_hermes_scheduler_allows_explicit_no_github_boundary() -> None:
    snapshot = {
        "capacity": {"active": 0, "maximum": 6},
        "work_items": [
            {
                "work_item_id": "work-local-only",
                "status": "planning",
                "plan": {
                    "steps": [
                        "Submit the local diff without pushing branches or "
                        "commenting on the GitHub issue."
                    ],
                    "acceptance_criteria": [
                        "The focused test passes with no GitHub publishing, "
                        "push, or external service change."
                    ],
                },
                "resource_requirements": {
                    "gpu_count": 1,
                    "gpu_architecture": "gfx1100",
                },
            }
        ],
    }

    action = MainHermesProjectManager._scheduled_start(snapshot)

    assert action is not None
    assert action.action is ProjectActionKind.START
    assert action.work_item_id == "work-local-only"


def test_six_planned_issues_launch_as_six_independent_workers(
    tmp_path: Path,
) -> None:
    runtime = _Runtime({})
    manager, store, _first, _controller, launcher = _planned_manager(
        tmp_path,
        runtime,
    )
    for number in range(18, 23):
        _add_work(store, number, planned=True)

    advances = [manager.advance(force=True, reconcile=False) for _index in range(6)]

    assert all(item is not None for item in advances)
    assert all(
        item.action.action is ProjectActionKind.START
        for item in advances
        if item is not None
    )
    assert runtime.requests == []
    assert len(launcher.calls) == 6
    assert len({task.task_id for task, _run_id in launcher.calls}) == 6
    assert len({run_id for _task, run_id in launcher.calls}) == 6
    assert store.active_work_count() == 6
    assert store.active_work_gpu_count() == 6
    assert store.work_counts()[WorkStatus.RUNNING.value] == 6


def test_running_and_review_work_do_not_spend_a_main_hermes_turn(
    tmp_path: Path,
) -> None:
    runtime = _Runtime({})
    manager, store, work_item_id, _controller, launcher = _planned_manager(
        tmp_path,
        runtime,
    )

    started = manager.advance(force=True, reconcile=False)
    waiting = manager.advance(force=True, reconcile=False)
    store.transition_work_item(
        work_item_id,
        WorkStatus.REVIEW,
        current_step="Independent reviewers own the candidate.",
        internal_candidate_id="internal-candidate-1",
        now=NOW,
    )
    review_waiting = manager.advance(force=True, reconcile=False)

    assert started is not None
    assert started.action.action is ProjectActionKind.START
    assert waiting is None
    assert review_waiting is None
    assert store.get_work_item(work_item_id).status is WorkStatus.REVIEW
    assert store.active_work_count() == 0
    assert len(launcher.calls) == 1
    assert runtime.requests == []
    assert manager._snapshot()["work_items"] == []


def test_nonunanimous_review_returns_bounded_feedback_to_fresh_worker(
    tmp_path: Path,
) -> None:
    candidates = SqliteInternalPullRequestCandidateStore(
        tmp_path / "candidates.db"
    )
    manager, store, work_item_id, _controller, launcher = _planned_manager(
        tmp_path,
        _Runtime({}),
        candidates=candidates,
    )
    first = manager.advance(force=True, reconcile=False)
    assert first is not None
    running = store.get_work_item(work_item_id)
    assert running.task_id is not None
    reviewed = _save_reviewed_candidate(
        candidates,
        task_id=running.task_id,
        candidate_id="internal-review-revision",
    )
    store.transition_work_item(
        work_item_id,
        WorkStatus.REVIEW,
        current_step="Internal candidate is awaiting review.",
        internal_candidate_id=reviewed.candidate_id,
        now=NOW + timedelta(seconds=3),
    )

    changed = manager.reconcile()
    revision = store.get_work_item(work_item_id)

    assert changed == [work_item_id]
    assert revision.status is WorkStatus.PLANNING
    assert revision.plan is not None
    assert revision.internal_candidate_id == reviewed.candidate_id
    assert revision.task_id is None
    assert revision.run_id is None
    assert revision.execution_id is None
    assert candidates.get(reviewed.candidate_id) == reviewed
    event = next(
        item
        for item in store.list_work_events(work_item_id_value=work_item_id)
        if item.event_type == "work.review_revision_requested"
    )
    assert event.payload["reviewed_candidate_id"] == reviewed.candidate_id
    assert event.payload["review_feedback"]["reviews"][0]["verdict"] == (
        "MORE_EVIDENCE_REQUIRED"
    )

    restarted = manager.advance(force=True, reconcile=False)
    assert restarted is not None
    assert restarted.action.action is ProjectActionKind.START
    assert len(launcher.calls) == 2
    revised_task = launcher.calls[-1][0]
    feedback = revised_task.metadata["review_feedback"]
    assert feedback["source_candidate_id"] == reviewed.candidate_id
    assert feedback["reviewed_execution_attempt"] == 1
    assert feedback["reviewed_candidate_attempt"] == 1
    assert "Add bounded regression evidence" in feedback["reviews"][0][
        "findings"
    ][0]
    assert store.get_work_item(work_item_id).execution_attempt == 2


def test_failed_execution_does_not_spend_review_candidate_budget(
    tmp_path: Path,
) -> None:
    candidates = SqliteInternalPullRequestCandidateStore(
        tmp_path / "candidates.db"
    )
    manager, store, work_item_id, _controller, launcher = _planned_manager(
        tmp_path,
        _Runtime({}),
        candidates=candidates,
        max_review_execution_attempts=2,
    )
    assert manager.advance(force=True, reconcile=False) is not None
    store.transition_work_item(
        work_item_id,
        WorkStatus.FAILED,
        current_step="Controller run ended FAILED.",
        error="Controller run ended FAILED.",
        now=NOW + timedelta(seconds=1),
    )
    store.retry_failed_execution_work_item(
        work_item_id,
        reason="The previous provider rejected the request before review.",
        requested_by="operator-17",
        now=NOW + timedelta(seconds=2),
    )
    assert manager.advance(force=True, reconcile=False) is not None
    running = store.get_work_item(work_item_id)
    assert running.execution_attempt == 2
    assert running.task_id is not None
    reviewed = _save_reviewed_candidate(
        candidates,
        task_id=running.task_id,
        candidate_id="internal-after-provider-failure",
    )
    store.transition_work_item(
        work_item_id,
        WorkStatus.REVIEW,
        current_step="Internal candidate is awaiting review.",
        internal_candidate_id=reviewed.candidate_id,
        now=NOW + timedelta(seconds=3),
    )

    changed = manager.reconcile()
    revision = store.get_work_item(work_item_id)

    assert changed == [work_item_id]
    assert revision.status is WorkStatus.PLANNING
    assert revision.execution_attempt == 2
    assert store.count_work_events(
        work_item_id_value=work_item_id,
        event_type="work.review",
    ) == 1
    assert len(launcher.calls) == 2


def test_nonunanimous_review_blocks_after_bounded_candidate_budget(
    tmp_path: Path,
) -> None:
    candidates = SqliteInternalPullRequestCandidateStore(
        tmp_path / "candidates.db"
    )
    manager, store, work_item_id, _controller, launcher = _planned_manager(
        tmp_path,
        _Runtime({}),
        candidates=candidates,
        max_review_execution_attempts=1,
    )
    assert manager.advance(force=True, reconcile=False) is not None
    running = store.get_work_item(work_item_id)
    assert running.task_id is not None
    reviewed = _save_reviewed_candidate(
        candidates,
        task_id=running.task_id,
        candidate_id="internal-review-exhausted",
    )
    store.transition_work_item(
        work_item_id,
        WorkStatus.REVIEW,
        current_step="Internal candidate is awaiting review.",
        internal_candidate_id=reviewed.candidate_id,
        now=NOW + timedelta(seconds=3),
    )

    changed = manager.reconcile()
    blocked = store.get_work_item(work_item_id)

    assert changed == [work_item_id]
    assert blocked.status is WorkStatus.BLOCKED
    assert blocked.internal_candidate_id == reviewed.candidate_id
    assert blocked.blocked_reason == (
        "The candidate remained non-unanimous after 1 reviewed Worker "
        "candidate attempts across 1 total executions."
    )
    assert len(launcher.calls) == 1
    with pytest.raises(
        ValueError,
        match="independent-review candidate budget is genuinely exhausted",
    ):
        store.retry_miscounted_review_budget_work_item(
            work_item_id,
            reason="Attempt to exceed the real candidate budget.",
            requested_by="operator-17",
            max_review_attempts=1,
        )


def test_operator_can_retry_false_positive_github_policy_block(
    tmp_path: Path,
) -> None:
    manager, store, work_item_id, _controller, _launcher = _planned_manager(
        tmp_path,
        _Runtime({}),
    )
    reason = (
        "Committed plan requires an external GitHub action, but workers may "
        "only produce and verify local repository changes."
    )
    store.transition_work_item(
        work_item_id,
        WorkStatus.BLOCKED,
        current_step="Main Hermes blocked this Work item.",
        reason=reason,
        now=NOW,
    )

    retrying = store.retry_policy_blocked_work_item(
        work_item_id,
        reason="Confirmed the plan only prohibits remote actions.",
        requested_by="operator-17",
        now=NOW + timedelta(seconds=1),
    )

    assert retrying.status is WorkStatus.PLANNING
    assert retrying.plan is not None
    assert retrying.blocked_reason is None
    assert retrying.task_id is None
    event = next(
        item
        for item in store.list_work_events(
            work_item_id_value=work_item_id,
            limit=100,
        )
        if item.event_type == "work.policy_block_retry_requested"
    )
    assert event.payload["requested_by"] == "operator-17"
    restarted = manager.advance(force=True, reconcile=False)
    assert restarted is not None
    assert restarted.action.action is ProjectActionKind.START


def test_provider_capacity_failure_preserves_plan_and_retries_without_manager_turn(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    clock = {"now": NOW}
    monkeypatch.setattr(
        "project_hermes.project_manager.utc_now",
        lambda: clock["now"],
    )
    monkeypatch.setattr(
        "project_hermes.polling_store.utc_now",
        lambda: clock["now"],
    )
    runtime = _Runtime({})
    manager, store, work_item_id, _controller, launcher = _planned_manager(
        tmp_path,
        runtime,
    )
    started = manager.advance(force=True, reconcile=False)
    assert started is not None
    first = store.get_work_item(work_item_id)
    assert first.run_id is not None
    assert first.task_id is not None
    assert first.execution_attempt == 1
    manager.runs.complete_run(first.run_id, status=LifecycleStatus.FAILED)
    manager.execution = _ExecutionProjection(failure_kind="provider_capacity_exhausted")  # type: ignore[assignment]

    changed = manager.reconcile()

    retrying = store.get_work_item(work_item_id)
    assert changed == [work_item_id]
    assert retrying.status is WorkStatus.PLANNING
    assert retrying.plan == first.plan
    assert retrying.execution_attempt == 1
    assert retrying.retry_not_before == NOW + timedelta(seconds=300)
    assert retrying.task_id is None
    assert retrying.run_id is None
    assert retrying.execution_id is None
    assert launcher.calls and len(launcher.calls) == 1

    cooling = manager.advance(force=True, reconcile=False)

    assert cooling is None
    assert runtime.requests == []
    assert len(launcher.calls) == 1
    assert any(
        event.event_type == "work.capacity_retry_scheduled"
        for event in store.list_work_events(
            work_item_id_value=work_item_id,
            limit=100,
        )
    )

    clock["now"] = NOW + timedelta(seconds=301)
    restarted = manager.advance(force=True, reconcile=False)

    assert restarted is not None
    assert restarted.action.action is ProjectActionKind.START
    second = store.get_work_item(work_item_id)
    assert second.status is WorkStatus.RUNNING
    assert second.execution_attempt == 2
    assert second.retry_not_before is None
    assert second.task_id is not None and second.task_id != first.task_id
    assert len(launcher.calls) == 2


def test_operator_can_retry_only_controller_cancelled_committed_work(
    tmp_path: Path,
) -> None:
    manager, store, work_item_id, _controller, _launcher = _planned_manager(
        tmp_path,
        _Runtime({}),
    )
    started = manager.advance(force=True, reconcile=False)
    assert started is not None
    running = store.get_work_item(work_item_id)
    store.transition_work_item(
        work_item_id,
        WorkStatus.BLOCKED,
        current_step="Controller run ended CANCELLED.",
        reason="Controller run ended CANCELLED.",
        now=NOW,
    )
    assert running.resource_requirements is not None
    refreshed_resources = running.resource_requirements.model_copy(
        update={"worker_model": "qwen3.8-max"}
    )

    retrying = store.retry_cancelled_work_item(
        work_item_id,
        reason="Verified prior artifacts and requested one clean retry.",
        resources=refreshed_resources,
        now=NOW + timedelta(seconds=1),
    )

    assert retrying.status is WorkStatus.PLANNING
    assert retrying.plan == running.plan
    assert retrying.execution_attempt == running.execution_attempt
    assert retrying.task_id is None
    assert retrying.run_id is None
    assert retrying.execution_id is None
    assert retrying.blocked_reason is None
    assert retrying.completed_at is None
    assert retrying.resource_requirements is not None
    assert retrying.resource_requirements.worker_model == "qwen3.8-max"
    event = next(
        item
        for item in store.list_work_events(
            work_item_id_value=work_item_id,
            limit=100,
        )
        if item.event_type == "work.cancelled_execution_retry_requested"
    )
    assert event.event_type == "work.cancelled_execution_retry_requested"
    assert event.payload["execution_id"] == running.execution_id


def test_operator_can_retry_pre_start_worker_admission_failure(
    tmp_path: Path,
) -> None:
    manager, store, work_item_id, _controller, launcher = _planned_manager(
        tmp_path,
        _Runtime({}),
    )
    planned = store.get_work_item(work_item_id)
    failed = store.transition_work_item(
        work_item_id,
        WorkStatus.FAILED,
        current_step="Worker launch failed.",
        error="Kubernetes Job quota rejected the admission.",
        now=NOW,
    )

    retrying = store.retry_failed_launch_work_item(
        work_item_id,
        reason="Released a completed installer Job and verified free quota.",
        now=NOW + timedelta(seconds=1),
    )

    assert failed.execution_attempt == planned.execution_attempt == 0
    assert retrying.status is WorkStatus.PLANNING
    assert retrying.plan == planned.plan
    assert retrying.execution_attempt == 1
    assert retrying.task_id is None
    assert retrying.run_id is None
    assert retrying.execution_id is None
    assert retrying.last_error is None
    assert retrying.completed_at is None
    event = next(
        item
        for item in store.list_work_events(
            work_item_id_value=work_item_id,
            limit=100,
        )
        if item.event_type == "work.failed_launch_retry_requested"
    )
    assert event.payload["execution_attempt"] == 1
    assert "quota" in event.payload["launch_error"]

    restarted = manager.advance(force=True, reconcile=False)

    assert restarted is not None
    running = store.get_work_item(work_item_id)
    assert running.status is WorkStatus.RUNNING
    assert running.execution_attempt == 2
    assert len(launcher.calls) == 1
    assert launcher.calls[0][0].metadata["execution_attempt"] == 2


def test_operator_can_retry_controller_failed_committed_work(
    tmp_path: Path,
) -> None:
    manager, store, work_item_id, _controller, launcher = _planned_manager(
        tmp_path,
        _Runtime({}),
    )
    started = manager.advance(force=True, reconcile=False)
    assert started is not None
    running = store.get_work_item(work_item_id)
    failed = store.transition_work_item(
        work_item_id,
        WorkStatus.FAILED,
        current_step="Controller run ended FAILED.",
        error="Controller run ended FAILED.",
        now=NOW,
    )

    retrying = store.retry_failed_execution_work_item(
        work_item_id,
        reason="Verified an execution-scoped temporary-volume eviction.",
        requested_by="operator-17",
        now=NOW + timedelta(seconds=1),
    )

    assert failed.execution_attempt == running.execution_attempt == 1
    assert retrying.status is WorkStatus.PLANNING
    assert retrying.plan == running.plan
    assert retrying.execution_attempt == running.execution_attempt
    assert retrying.task_id is None
    assert retrying.run_id is None
    assert retrying.execution_id is None
    assert retrying.last_error is None
    assert retrying.completed_at is None
    event = next(
        item
        for item in store.list_work_events(
            work_item_id_value=work_item_id,
            limit=100,
        )
        if item.event_type == "work.failed_execution_retry_requested"
    )
    assert event.payload["execution_id"] == running.execution_id
    assert event.payload["requested_by"] == "operator-17"

    restarted = manager.advance(force=True, reconcile=False)

    assert restarted is not None
    second = store.get_work_item(work_item_id)
    assert second.status is WorkStatus.RUNNING
    assert second.execution_attempt == 2
    assert second.execution_id != running.execution_id
    assert len(launcher.calls) == 2


def test_full_worker_capacity_does_not_plan_or_wait_with_main_hermes(
    tmp_path: Path,
) -> None:
    runtime = _Runtime({})
    manager, store, _first, _controller, launcher = _planned_manager(
        tmp_path,
        runtime,
    )
    for number in range(18, 23):
        _add_work(store, number, planned=True)
    for _index in range(6):
        manager.advance(force=True, reconcile=False)
    queued_work_id = _add_work(store, 23, planned=False)

    waiting = manager.advance(force=True, reconcile=False)

    assert waiting is None
    assert store.active_work_count() == 6
    assert store.get_work_item(queued_work_id).status is WorkStatus.QUEUED
    assert len(launcher.calls) == 6
    assert runtime.requests == []


def test_each_main_hermes_issue_decision_uses_a_fresh_session(
    tmp_path: Path,
) -> None:
    runtime = _Runtime({})
    manager, store, first_work_id = _manager(tmp_path, runtime)
    second_work_id = _add_work(store, 18, planned=False)
    runtime.output = {
        "project_actions": [
            {
                "action": "plan",
                "work_item_id": first_work_id,
                "reason": "The first Issue is actionable.",
                "plan": {
                    "summary": "Repair the first boundary.",
                    "steps": ["Reproduce.", "Fix and validate."],
                    "acceptance_criteria": ["The focused test passes."],
                },
            }
        ]
    }
    first = manager.advance(force=True, reconcile=False)
    assert first is not None
    store.transition_work_item(
        first_work_id,
        WorkStatus.BLOCKED,
        current_step="Fixture completed the first decision.",
        reason="Move to the next independent decision.",
        now=NOW,
    )
    runtime.output = {
        "project_actions": [
            {
                "action": "plan",
                "work_item_id": second_work_id,
                "reason": "The second Issue is independently actionable.",
                "plan": {
                    "summary": "Repair the second boundary.",
                    "steps": ["Reproduce.", "Fix and validate."],
                    "acceptance_criteria": ["The focused test passes."],
                },
            }
        ]
    }

    second = manager.advance(force=True, reconcile=False)

    assert second is not None
    assert len(runtime.requests) == 2
    assert runtime.requests[0].session_id != runtime.requests[1].session_id
    assert [request.metadata["request_kind"] for request in runtime.requests] == [
        "bootstrap",
        "bootstrap",
    ]
    assert all(
        "fresh decision conversation" in request.prompt for request in runtime.requests
    )


def test_task_fingerprint_bypasses_a_legacy_orphan_run(
    tmp_path: Path,
) -> None:
    runtime = _Runtime({})
    manager, store, work_item_id, controller, launcher = _planned_manager(
        tmp_path,
        runtime,
    )
    planned = store.get_work_item(work_item_id)
    candidate = store.get_candidate(planned.candidate_id)
    task = manager._issue_task(
        planned,
        candidate,
        planned.named_baseline or "main@" + "a" * 40,
    )
    legacy_task_id = f"issue-{work_item_id}"
    controller.create_task_run(
        task.model_copy(update={"task_id": legacy_task_id}),
        run_id=f"work-run-{work_item_id}",
    )
    runtime.output = {
        "project_actions": [
            {
                "action": "start",
                "work_item_id": work_item_id,
                "reason": "Resume through a new immutable task identity.",
            }
        ]
    }

    result = manager.advance(force=True, reconcile=False)

    assert result is not None
    assert result.outcome["ok"] is True
    started = store.get_work_item(work_item_id)
    assert started.status is WorkStatus.RUNNING
    assert started.task_id is not None
    assert started.task_id != legacy_task_id
    assert started.run_id != f"work-run-{work_item_id}"
    assert launcher.calls[0][0].task_id == started.task_id


def test_controller_start_rejection_does_not_poison_planning_queue(
    tmp_path: Path,
) -> None:
    runtime = _Runtime({})
    manager, store, work_item_id, _controller, _launcher = _planned_manager(
        tmp_path,
        runtime,
    )
    manager.controller = _FailingController()  # type: ignore[assignment]
    runtime.output = {
        "project_actions": [
            {
                "action": "start",
                "work_item_id": work_item_id,
                "reason": "Advance the committed plan.",
            }
        ]
    }

    result = manager.advance(force=True, reconcile=False)

    assert result is not None
    assert result.outcome["ok"] is False
    failed = store.get_work_item(work_item_id)
    assert failed.status is WorkStatus.FAILED
    assert failed.last_error == "controller rejected the immutable task"


def test_invalid_main_hermes_output_is_persisted_as_manager_error(
    tmp_path: Path,
) -> None:
    runtime = _Runtime({"project_actions": []})
    manager, store, _work_item_id = _manager(tmp_path, runtime)

    with pytest.raises(ValueError, match="exactly one action"):
        manager.advance(force=True, reconcile=False)

    state = store.get_manager_state()
    assert state is not None
    assert state.last_error == "project_actions must contain exactly one action"
    assert any(
        event.event_type == "manager.turn_failed"
        for event in store.list_work_events(limit=100)
    )


def test_main_hermes_retries_protocol_errors_before_normal_idle_interval(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    clock = {"now": NOW}
    monkeypatch.setattr(
        "project_hermes.project_manager.utc_now",
        lambda: clock["now"],
    )
    runtime = _Runtime({"project_actions": []})
    manager, store, _work_item_id = _manager(tmp_path, runtime)

    with pytest.raises(ValueError, match="exactly one action"):
        manager.advance(force=True, reconcile=False)

    clock["now"] = NOW + timedelta(seconds=29)
    assert manager.advance(force=False, reconcile=False) is None

    runtime.output = {
        "project_actions": [
            {"action": "wait", "reason": "Await the next durable change."}
        ]
    }
    clock["now"] = NOW + timedelta(seconds=30)
    result = manager.advance(force=False, reconcile=False)

    assert result is not None
    assert result.action.action is ProjectActionKind.WAIT
    assert len(runtime.requests) == 2
    assert store.get_manager_state().last_error is None


def test_main_hermes_retries_invalid_start_precondition_as_protocol_error(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    clock = {"now": NOW}
    monkeypatch.setattr(
        "project_hermes.project_manager.utc_now",
        lambda: clock["now"],
    )
    runtime = _Runtime({})
    manager, store, work_item_id = _manager(tmp_path, runtime)
    runtime.output = {
        "project_actions": [
            {
                "action": "start",
                "work_item_id": work_item_id,
                "reason": "Start the queued item before committing a plan.",
            }
        ]
    }

    rejected = manager.advance(force=True, reconcile=False)

    assert rejected is not None
    assert rejected.outcome == {
        "ok": False,
        "error": "Main Hermes can start only planned Work",
    }
    assert store.get_work_item(work_item_id).status is WorkStatus.QUEUED
    first_session_id = runtime.requests[0].session_id

    clock["now"] = NOW + timedelta(seconds=29)
    assert manager.advance(force=False, reconcile=False) is None

    runtime.output = {
        "project_actions": [
            {
                "action": "plan",
                "work_item_id": work_item_id,
                "reason": "Commit a plan before starting the Work item.",
                "plan": {
                    "summary": "Repair the reported regression.",
                    "steps": ["Reproduce.", "Implement and validate the fix."],
                    "acceptance_criteria": ["The focused test passes."],
                },
            }
        ]
    }
    clock["now"] = NOW + timedelta(seconds=30)
    planned = manager.advance(force=False, reconcile=False)

    assert planned is not None
    assert planned.action.action is ProjectActionKind.PLAN
    assert planned.outcome["status"] == WorkStatus.PLANNING.value
    assert runtime.requests[1].session_id != first_session_id
    assert runtime.requests[1].metadata["request_kind"] == "bootstrap"
    assert store.get_manager_state().last_error is None


def test_main_hermes_backs_off_after_a_bootstrap_provider_error(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    clock = {"now": NOW}
    monkeypatch.setattr(
        "project_hermes.project_manager.utc_now",
        lambda: clock["now"],
    )
    runtime = _StartFailureRuntime()
    manager, _store, _work_item_id = _manager(tmp_path, runtime)

    with pytest.raises(RuntimeError, match="HTTP 429"):
        manager.advance(force=True, reconcile=False)

    clock["now"] = NOW + timedelta(seconds=59)
    assert manager.advance(force=False, reconcile=False) is None
    assert runtime.start_calls == 1

    clock["now"] = NOW + timedelta(seconds=60)
    with pytest.raises(RuntimeError, match="HTTP 429"):
        manager.advance(force=False, reconcile=False)
    assert runtime.start_calls == 2


def test_main_hermes_repairs_one_missing_top_level_action_array_closer() -> None:
    result = RuntimeResult(
        session_id="manager-session",
        status=RuntimeStatus.COMPLETED,
        final_response=(
            "```json\n"
            '{"project_actions":[{"action":"wait","reason":'
            '"Await the next durable change."}}\n'
            "```"
        ),
        output={},
    )

    action = _action_from_result(result)

    assert action.action is ProjectActionKind.WAIT


def test_main_hermes_does_not_repair_nested_malformed_json() -> None:
    result = RuntimeResult(
        session_id="manager-session",
        status=RuntimeStatus.COMPLETED,
        final_response=(
            '{"project_actions":[{"action":"wait","reason":["malformed"}}]}'
        ),
        output={},
    )

    with pytest.raises(ValueError, match="exactly one action"):
        _action_from_result(result)


@pytest.mark.parametrize(
    ("wrapper", "kind", "work_item_id"),
    [
        (
            {
                "plan": {
                    "work_item_id": "work-1",
                    "reason": "The Issue is bounded.",
                    "plan": {
                        "summary": "Fix the boundary.",
                        "steps": ["Add a regression test.", "Fix the code."],
                        "acceptance_criteria": ["The regression test passes."],
                    },
                }
            },
            ProjectActionKind.PLAN,
            "work-1",
        ),
        (
            {"start": {"work_item_id": "work-2", "reason": "Capacity exists."}},
            ProjectActionKind.START,
            "work-2",
        ),
        (
            {"block": {"work_item_id": "work-3", "reason": "Not actionable."}},
            ProjectActionKind.BLOCK,
            "work-3",
        ),
        (
            {"wait": {"reason": "No decision-bearing Work remains."}},
            ProjectActionKind.WAIT,
            None,
        ),
    ],
)
def test_main_hermes_accepts_strict_single_key_action_wrappers(
    wrapper: dict[str, Any],
    kind: ProjectActionKind,
    work_item_id: str | None,
) -> None:
    action = _action_from_result(
        RuntimeResult(
            session_id="manager-session",
            status=RuntimeStatus.COMPLETED,
            output={"project_actions": [wrapper]},
        )
    )

    assert action.action is kind
    assert action.work_item_id == work_item_id


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (
            {"plan": {"reason": "one"}, "wait": {"reason": "two"}},
            "exactly one supported action wrapper",
        ),
        ({"launch": {"reason": "unknown"}}, "unsupported project action wrapper"),
        ({"wait": "not-an-object"}, "wait action wrapper must contain an object"),
        (
            {"wait": {"action": "wait", "reason": "ambiguous"}},
            "cannot also contain action",
        ),
    ],
)
def test_main_hermes_rejects_ambiguous_or_unknown_action_wrappers(
    entry: dict[str, Any],
    message: str,
) -> None:
    result = RuntimeResult(
        session_id="manager-session",
        status=RuntimeStatus.COMPLETED,
        output={"project_actions": [entry]},
    )

    with pytest.raises(ValueError, match=message):
        _action_from_result(result)
