from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from project_hermes.polling_supervisor import PollingSupervisor


class _Polling:
    def __init__(self) -> None:
        self.calls = 0

    def run_if_due(self) -> object | None:
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(run_id="poll-1")
        return None


class _Manager:
    def __init__(self) -> None:
        self.reconciles = 0
        self.turns = 0

    def reconcile(self) -> list[str]:
        self.reconciles += 1
        return ["work-1"] if self.reconciles == 1 else []

    def advance(self, *, force: bool, reconcile: bool) -> object | None:
        assert reconcile is False
        self.turns += 1
        if force:
            return SimpleNamespace(action="plan")
        return None


class _ContinuousPolling:
    def __init__(self) -> None:
        self.calls = 0

    def run_if_due(self) -> object | None:
        self.calls += 1
        if self.calls <= 2:
            return SimpleNamespace(run_id=f"poll-{self.calls}")
        return None


class _IdleManager:
    def reconcile(self) -> list[str]:
        return []

    def advance(self, *, force: bool, reconcile: bool) -> object | None:
        assert force is False
        assert reconcile is False
        return None


class _BlockingManager:
    def __init__(
        self,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        self.entered = entered
        self.release = release

    def reconcile(self) -> list[str]:
        return []

    def advance(self, *, force: bool, reconcile: bool) -> object | None:
        assert force is False
        assert reconcile is False
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test manager was not released")
        return None


class _Reviewer:
    def __init__(self) -> None:
        self.calls = 0

    def review_next(self) -> object | None:
        self.calls += 1
        if self.calls <= 2:
            return SimpleNamespace(review_id=f"review-{self.calls}")
        return None


class _PollingWhileManagerBlocked:
    def __init__(
        self,
        manager_entered: threading.Event,
        second_scan: threading.Event,
    ) -> None:
        self.manager_entered = manager_entered
        self.second_scan = second_scan
        self.calls = 0

    def run_if_due(self) -> object | None:
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(run_id="poll-1")
        if self.calls == 2:
            if not self.manager_entered.wait(timeout=2):
                return None
            self.second_scan.set()
            return SimpleNamespace(run_id="poll-2")
        return None


def test_polling_supervisor_drives_polling_and_main_hermes_turns() -> None:
    async def scenario() -> tuple[_Polling, _Manager, object]:
        polling = _Polling()
        manager = _Manager()
        supervisor = PollingSupervisor(
            polling,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            loop_interval_seconds=0.01,
        )
        await supervisor.start()
        for _ in range(100):
            if manager.turns:
                break
            await asyncio.sleep(0.01)
        await supervisor.stop()
        return polling, manager, supervisor.status

    polling, manager, status = asyncio.run(scenario())

    assert polling.calls >= 1
    assert manager.reconciles >= 1
    assert manager.turns >= 1
    assert status.polling_runs == 1
    assert status.manager_turns == 1
    assert status.running is False


def test_polling_supervisor_drives_independent_candidate_reviews() -> None:
    async def scenario() -> tuple[_Reviewer, object]:
        reviewer = _Reviewer()
        supervisor = PollingSupervisor(
            _Polling(),  # type: ignore[arg-type]
            _IdleManager(),  # type: ignore[arg-type]
            loop_interval_seconds=0.01,
            reviewer=reviewer,  # type: ignore[arg-type]
            reviewer_error_retry_seconds=0.01,
        )
        await supervisor.start()
        for _ in range(100):
            if supervisor.status.reviewer_turns == 2:
                break
            await asyncio.sleep(0.01)
        await supervisor.stop()
        return reviewer, supervisor.status

    reviewer, status = asyncio.run(scenario())

    assert reviewer.calls >= 2
    assert status.reviewer_turns == 2
    assert status.last_reviewed_at is not None
    assert status.last_review_error is None


def test_polling_supervisor_immediately_continues_a_repository_pass() -> None:
    async def scenario() -> tuple[_ContinuousPolling, object]:
        polling = _ContinuousPolling()
        supervisor = PollingSupervisor(
            polling,  # type: ignore[arg-type]
            _IdleManager(),  # type: ignore[arg-type]
            loop_interval_seconds=30,
        )
        await supervisor.start()
        for _ in range(100):
            if supervisor.status.polling_runs == 2:
                break
            await asyncio.sleep(0.01)
        await supervisor.stop()
        return polling, supervisor.status

    polling, status = asyncio.run(scenario())

    assert polling.calls >= 2
    assert status.polling_runs == 2


def test_polling_continues_while_main_hermes_turn_is_blocked() -> None:
    async def scenario() -> tuple[bool, object]:
        manager_entered = threading.Event()
        manager_release = threading.Event()
        second_scan = threading.Event()
        polling = _PollingWhileManagerBlocked(manager_entered, second_scan)
        manager = _BlockingManager(manager_entered, manager_release)
        supervisor = PollingSupervisor(
            polling,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            loop_interval_seconds=30,
        )
        await supervisor.start()
        try:
            scanned_while_blocked = await asyncio.to_thread(
                second_scan.wait,
                1,
            )
        finally:
            manager_release.set()
            await supervisor.stop()
        return scanned_while_blocked, supervisor.status

    scanned_while_blocked, status = asyncio.run(scenario())

    assert scanned_while_blocked is True
    assert status.polling_runs == 2
    assert status.running is False
