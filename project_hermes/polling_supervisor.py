"""Asynchronous timer for polling and Main Hermes project management."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from project_hermes.candidate_review import InternalCandidateReviewService
from project_hermes.issue_screening import IssueScreeningService
from project_hermes.models import StrictModel, utc_now
from project_hermes.polling import IssuePollingService
from project_hermes.project_manager import MainHermesProjectManager
from project_hermes.redaction import redact_text

_LOG = logging.getLogger(__name__)


class PollingSupervisorStatus(StrictModel):
    running: bool = False
    stopping: bool = False
    polling_runs: int = 0
    manager_turns: int = 0
    reconcile_count: int = 0
    reviewer_turns: int = 0
    screening_turns: int = 0
    last_polled_at: datetime | None = None
    last_manager_turn_at: datetime | None = None
    last_reconciled_at: datetime | None = None
    last_reviewed_at: datetime | None = None
    last_screened_at: datetime | None = None
    polling_heartbeat_at: datetime | None = None
    manager_heartbeat_at: datetime | None = None
    reviewer_heartbeat_at: datetime | None = None
    screening_heartbeat_at: datetime | None = None
    last_error: str | None = None
    last_review_error: str | None = None
    last_screening_error: str | None = None


class PollingSupervisor:
    """Drive due polls and decision-bearing Main Hermes turns."""

    def __init__(
        self,
        polling: IssuePollingService,
        manager: MainHermesProjectManager,
        *,
        loop_interval_seconds: float,
        reviewer: InternalCandidateReviewService | None = None,
        screener: IssueScreeningService | None = None,
        reviewer_error_retry_seconds: float = 300,
        stall_timeout_seconds: float = 5400,
    ) -> None:
        if loop_interval_seconds <= 0:
            raise ValueError("polling supervisor interval must be positive")
        if reviewer_error_retry_seconds <= 0:
            raise ValueError("reviewer retry interval must be positive")
        if stall_timeout_seconds <= 0:
            raise ValueError("supervisor stall timeout must be positive")
        self.polling = polling
        self.manager = manager
        self.reviewer = reviewer
        self.screener = screener
        self.loop_interval_seconds = loop_interval_seconds
        self.reviewer_error_retry_seconds = reviewer_error_retry_seconds
        self.stall_timeout_seconds = stall_timeout_seconds
        self._stop = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._tasks: tuple[asyncio.Task[None], ...] = ()
        self._status = PollingSupervisorStatus()

    @property
    def status(self) -> PollingSupervisorStatus:
        return self._status.model_copy(deep=True)

    def liveness(self, *, now: datetime | None = None) -> dict[str, object]:
        """Report whether every configured loop is alive and heartbeating."""

        timestamp = now or utc_now()
        reasons: list[str] = []
        if not self._status.running:
            reasons.append("supervisor is not running")
        if self._status.stopping:
            reasons.append("supervisor is stopping")
        tasks = {task.get_name(): task for task in self._tasks}
        expected = {
            "project-hermes-polling-loop": self._status.polling_heartbeat_at,
            "project-hermes-manager-loop": self._status.manager_heartbeat_at,
        }
        if self.reviewer is not None:
            expected["project-hermes-reviewer-loop"] = (
                self._status.reviewer_heartbeat_at
            )
        if self.screener is not None:
            expected["project-hermes-screening-loop"] = (
                self._status.screening_heartbeat_at
            )
        for name, heartbeat in expected.items():
            task = tasks.get(name)
            if task is None or task.done():
                reasons.append(f"{name} is not running")
                continue
            if heartbeat is not None and (
                timestamp - heartbeat
            ).total_seconds() > self.stall_timeout_seconds:
                reasons.append(f"{name} heartbeat is stale")
        return {
            "ok": not reasons,
            "checked_at": timestamp.isoformat(),
            "stall_timeout_seconds": self.stall_timeout_seconds,
            "reasons": reasons,
            "status": self.status.model_dump(mode="json"),
        }

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if any(not task.done() for task in self._tasks):
                return
            self._stop = asyncio.Event()
            started_at = utc_now()
            self._status = self._status.model_copy(
                update={
                    "running": True,
                    "stopping": False,
                    "last_error": None,
                    "polling_heartbeat_at": started_at,
                    "manager_heartbeat_at": started_at,
                    "reviewer_heartbeat_at": (
                        started_at if self.reviewer is not None else None
                    ),
                    "screening_heartbeat_at": (
                        started_at if self.screener is not None else None
                    ),
                }
            )
            tasks = [
                asyncio.create_task(
                    self._run_polling(),
                    name="project-hermes-polling-loop",
                ),
                asyncio.create_task(
                    self._run_manager(),
                    name="project-hermes-manager-loop",
                ),
            ]
            if self.reviewer is not None:
                tasks.append(
                    asyncio.create_task(
                        self._run_reviewer(),
                        name="project-hermes-reviewer-loop",
                    )
                )
            if self.screener is not None:
                tasks.append(
                    asyncio.create_task(
                        self._run_screening(),
                        name="project-hermes-screening-loop",
                    )
                )
            self._tasks = tuple(tasks)

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            tasks = self._tasks
            if not tasks:
                return
            self._status = self._status.model_copy(update={"stopping": True})
            self._stop.set()
        await asyncio.gather(*(asyncio.shield(task) for task in tasks))
        async with self._lifecycle_lock:
            self._tasks = ()
            self._status = self._status.model_copy(
                update={"running": False, "stopping": False}
            )

    async def _run_polling(self) -> None:
        while not self._stop.is_set():
            self._heartbeat("polling_heartbeat_at")
            polled = await self._safe_poll()
            self._heartbeat("polling_heartbeat_at")
            # A completed repository scan may make the next least-recently
            # scanned repository due immediately. Continue the bounded
            # pipeline without waiting for the normal idle tick; queue
            # backpressure inside ``run_if_due`` will stop the chain.
            if polled:
                continue
            await self._wait_for_tick()

    async def _run_manager(self) -> None:
        while not self._stop.is_set():
            self._heartbeat("manager_heartbeat_at")
            advanced = await self._safe_manager()
            self._heartbeat("manager_heartbeat_at")
            # State-changing actions should immediately expose the next
            # decision so Main Hermes can keep the configured worker lanes
            # occupied. A non-changing wait is deduplicated by ``advance``.
            if advanced:
                continue
            await self._wait_for_tick()

    async def _run_reviewer(self) -> None:
        while not self._stop.is_set():
            self._heartbeat("reviewer_heartbeat_at")
            reviewed, failed = await self._safe_review()
            self._heartbeat("reviewer_heartbeat_at")
            if reviewed:
                continue
            await self._wait_for_tick(
                self.reviewer_error_retry_seconds
                if failed
                else self.loop_interval_seconds
            )

    async def _run_screening(self) -> None:
        while not self._stop.is_set():
            self._heartbeat("screening_heartbeat_at")
            screened, failed = await self._safe_screening()
            self._heartbeat("screening_heartbeat_at")
            if screened:
                continue
            await self._wait_for_tick(
                self.reviewer_error_retry_seconds
                if failed
                else self.loop_interval_seconds
            )

    async def _wait_for_tick(self, seconds: float | None = None) -> None:
        if self._stop.is_set():
            return
        try:
            await asyncio.wait_for(
                self._stop.wait(),
                timeout=(
                    self.loop_interval_seconds if seconds is None else seconds
                ),
            )
        except TimeoutError:
            return

    async def _safe_poll(self) -> bool:
        try:
            run = await asyncio.to_thread(self.polling.run_if_due)
        except Exception as exc:
            self._record_error("Polling run failed", exc)
            return False
        if run is None:
            return False
        self._status = self._status.model_copy(
            update={
                "polling_runs": self._status.polling_runs + 1,
                "last_polled_at": utc_now(),
                "last_error": None,
            }
        )
        return True

    async def _safe_manager(self) -> bool:
        try:
            changed = await asyncio.to_thread(self.manager.reconcile)
        except Exception as exc:
            self._record_error("Work reconciliation failed", exc)
            return False
        self._status = self._status.model_copy(
            update={
                "reconcile_count": self._status.reconcile_count + 1,
                "last_reconciled_at": utc_now(),
                "last_error": None,
            }
        )
        try:
            result = await asyncio.to_thread(
                self.manager.advance,
                force=bool(changed),
                reconcile=False,
            )
        except Exception as exc:
            self._record_error("Main Hermes project-manager turn failed", exc)
            return False
        if result is None:
            return False
        self._status = self._status.model_copy(
            update={
                "manager_turns": self._status.manager_turns + 1,
                "last_manager_turn_at": utc_now(),
                "last_error": None,
            }
        )
        return True

    async def _safe_review(self) -> tuple[bool, bool]:
        if self.reviewer is None:
            return False, False
        try:
            result = await asyncio.to_thread(self.reviewer.review_next)
        except Exception as exc:
            detail = redact_text(str(exc)) or type(exc).__name__
            self._status = self._status.model_copy(
                update={"last_review_error": detail[:500]}
            )
            _LOG.exception("Candidate review failed: %s", detail)
            return False, True
        if result is None:
            return False, False
        self._status = self._status.model_copy(
            update={
                "reviewer_turns": self._status.reviewer_turns + 1,
                "last_reviewed_at": utc_now(),
                "last_review_error": None,
            }
        )
        return True, False

    async def _safe_screening(self) -> tuple[bool, bool]:
        if self.screener is None:
            return False, False
        try:
            result = await asyncio.to_thread(self.screener.screen_next)
        except Exception as exc:
            detail = redact_text(str(exc)) or type(exc).__name__
            self._status = self._status.model_copy(
                update={"last_screening_error": detail[:500]}
            )
            _LOG.exception("Issue screening failed: %s", detail)
            return False, True
        if result is None:
            return False, False
        self._status = self._status.model_copy(
            update={
                "screening_turns": self._status.screening_turns + 1,
                "last_screened_at": utc_now(),
                "last_screening_error": None,
            }
        )
        return True, False

    def _record_error(self, prefix: str, error: Exception) -> None:
        detail = redact_text(str(error)) or type(error).__name__
        self._status = self._status.model_copy(
            update={"last_error": f"{prefix}: {detail}"[:500]}
        )
        _LOG.exception("%s: %s", prefix, detail)

    def _heartbeat(self, field: str) -> None:
        self._status = self._status.model_copy(update={field: utc_now()})


__all__ = ["PollingSupervisor", "PollingSupervisorStatus"]
