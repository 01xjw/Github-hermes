"""Adapter for the upstream Hermes ``AIAgent`` surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock, RLock
from typing import Any, Callable, Iterable
from uuid import uuid4

from project_hermes.models import utc_now
from project_hermes.redaction import redact_data, redact_text
from project_hermes.runtime.base import (
    RuntimeEvent,
    RuntimeHandle,
    RuntimeRequest,
    RuntimeResult,
    RuntimeStatus,
)


@dataclass
class _HermesSession:
    agent: Any
    handle: RuntimeHandle
    events: list[RuntimeEvent] = field(default_factory=list)
    event_sequence_base: int = 0
    result: RuntimeResult | None = None
    lock: RLock = field(default_factory=RLock)
    turn_lock: Any = field(default_factory=Lock)


class HermesRuntimeAdapter:
    """Use Hermes as a long-lived reasoning runtime.

    ``agent_factory`` is injected so ProjectHermes does not duplicate provider,
    credential, prompt, or plugin setup owned by upstream Hermes. The required
    attestor must fail unless the agent is inside the task sandbox and exposes
    only controller-owned capabilities.
    """

    name = "hermes"

    def __init__(
        self,
        agent_factory: Callable[[RuntimeRequest], Any],
        *,
        attest_agent: Callable[[Any, RuntimeRequest], None],
        agent_restorer: Callable[[RuntimeHandle, RuntimeRequest], Any]
        | None = None,
        known_secrets: Iterable[str]
        | Callable[[], Iterable[str]] = (),
    ) -> None:
        self._agent_factory = agent_factory
        self._attest_agent = attest_agent
        self._agent_restorer = agent_restorer
        self._known_secrets = known_secrets
        self._sessions: dict[str, _HermesSession] = {}
        self._pending_session_ids: set[str] = set()
        self._lock = RLock()

    def start(self, request: RuntimeRequest) -> RuntimeHandle:
        session_id = request.session_id or f"hermes-{uuid4().hex}"
        effective_request = request.model_copy(
            update={"session_id": session_id}
        )
        with self._lock:
            if (
                session_id in self._sessions
                or session_id in self._pending_session_ids
            ):
                raise ValueError(f"Hermes session already exists: {session_id}")
            self._pending_session_ids.add(session_id)

        agent: Any = None
        attested = False
        installed = False
        try:
            handle = RuntimeHandle(
                runtime_name=self.name,
                session_id=session_id,
                task_id=effective_request.task_id,
                status=RuntimeStatus.STARTING,
                model_route=effective_request.model_route,
                runtime_generation=effective_request.runtime_generation,
                worktree_path=effective_request.cwd,
            )
            agent = self._agent_factory(effective_request)
            self._attest_or_close(agent, effective_request)
            attested = True
            session = _HermesSession(
                agent=agent,
                handle=handle,
            )
            with self._lock:
                if session_id in self._sessions:
                    raise ValueError(
                        f"Hermes session already exists: {session_id}"
                    )
                self._sessions[session_id] = session
                installed = True
        except Exception:
            if agent is not None and attested and not installed:
                self._close_agent(agent)
            raise
        finally:
            with self._lock:
                self._pending_session_ids.discard(session_id)
        return self._run_turn(session, effective_request)

    def resume(
        self, handle: RuntimeHandle, request: RuntimeRequest
    ) -> RuntimeHandle:
        session = self._get(handle)
        if request.task_id != handle.task_id:
            raise ValueError("resume request belongs to a different task")
        if request.session_id not in {None, handle.session_id}:
            raise ValueError("resume request belongs to a different Hermes session")
        if request.model_route != handle.model_route:
            raise ValueError("cannot change model profile on a live Hermes session")
        if request.runtime_generation != handle.runtime_generation:
            raise ValueError(
                "cannot change runtime generation on a live Hermes session"
            )
        return self._run_turn(session, request)

    def reconnect(
        self,
        handle: RuntimeHandle,
        request: RuntimeRequest,
    ) -> RuntimeHandle:
        if handle.runtime_name != self.name:
            raise ValueError("runtime handle belongs to a different adapter")
        if request.task_id != handle.task_id:
            raise ValueError("reconnect request belongs to a different task")
        if request.model_route != handle.model_route:
            raise ValueError(
                "reconnect request model profile differs from the handle"
            )
        if request.runtime_generation != handle.runtime_generation:
            raise ValueError(
                "reconnect request runtime generation differs from the handle"
            )
        if (
            request.session_id is not None
            and request.session_id != handle.session_id
        ):
            raise ValueError(
                "reconnect request belongs to a different Hermes session"
            )
        event_sequence_base = int(
            request.metadata.get("after_runtime_event_sequence", 0)
        )
        if event_sequence_base < 0:
            raise ValueError(
                "runtime event recovery sequence cannot be negative"
            )
        effective_request = request.model_copy(
            update={"session_id": handle.session_id}
        )
        with self._lock:
            existing = self._sessions.get(handle.session_id)
            if existing is not None:
                self._validate_session_handle(existing, handle)
                return existing.handle
            if self._agent_restorer is None:
                raise RuntimeError(
                    "Hermes runtime recovery requires an injected "
                    "agent_restorer"
                )
            if handle.session_id in self._pending_session_ids:
                raise RuntimeError(
                    "Hermes session construction is already in progress"
                )
            self._pending_session_ids.add(handle.session_id)

        agent: Any = None
        attested = False
        installed = False
        try:
            restored = handle.model_copy(
                update={
                    "status": RuntimeStatus.IDLE,
                    "updated_at": utc_now(),
                }
            )
            agent = self._agent_restorer(handle, effective_request)
            self._attest_or_close(agent, effective_request)
            attested = True
            session = _HermesSession(
                agent=agent,
                handle=restored,
                event_sequence_base=event_sequence_base,
            )
            self._emit(session, "runtime.reconnected", {})
            with self._lock:
                if handle.session_id in self._sessions:
                    raise RuntimeError(
                        "Hermes session was installed during recovery"
                    )
                self._sessions[handle.session_id] = session
                installed = True
        except Exception:
            if agent is not None and attested and not installed:
                self._close_agent(agent)
            raise
        finally:
            with self._lock:
                self._pending_session_ids.discard(handle.session_id)
        return session.handle

    def cancel(self, handle: RuntimeHandle) -> RuntimeHandle:
        session = self._get(handle)
        agent = session.agent
        interrupt = getattr(agent, "request_interrupt", None)
        if not callable(interrupt):
            interrupt = getattr(agent, "interrupt", None)
        if callable(interrupt):
            interrupt()
        with session.lock:
            session.handle = session.handle.model_copy(
                update={
                    "status": RuntimeStatus.CANCELLED,
                    "updated_at": utc_now(),
                }
            )
            self._emit(session, "runtime.cancel_requested", {})
            return session.handle

    def events(
        self, handle: RuntimeHandle, *, after_sequence: int = 0
    ) -> Iterable[RuntimeEvent]:
        session = self._get(handle)
        with session.lock:
            return tuple(
                event
                for event in session.events
                if event.sequence > after_sequence
            )

    def result(self, handle: RuntimeHandle) -> RuntimeResult | None:
        session = self._get(handle)
        with session.lock:
            return session.result

    def close(self, handle: RuntimeHandle) -> RuntimeHandle:
        session = self._get(handle)
        if not session.turn_lock.acquire(blocking=False):
            raise RuntimeError(
                "cannot close Hermes while a turn is in progress"
            )
        try:
            self._close_agent(session.agent)
            with session.lock:
                session.handle = session.handle.model_copy(
                    update={
                        "status": RuntimeStatus.CLOSED,
                        "updated_at": utc_now(),
                    }
                )
                self._emit(session, "runtime.closed", {})
                return session.handle
        finally:
            session.turn_lock.release()

    def _run_turn(
        self, session: _HermesSession, request: RuntimeRequest
    ) -> RuntimeHandle:
        if not session.turn_lock.acquire(blocking=False):
            raise RuntimeError("a Hermes turn is already in progress")
        started_at = utc_now()
        try:
            with session.lock:
                session.handle = session.handle.model_copy(
                    update={
                        "status": RuntimeStatus.RUNNING,
                        "updated_at": utc_now(),
                    }
                )
                self._emit(
                    session,
                    "turn.started",
                    {"request_id": request.request_id},
                )
            try:
                if hasattr(session.agent, "run_runtime_turn"):
                    raw = session.agent.run_runtime_turn(request)
                    if isinstance(raw, dict):
                        response = raw.get("final_response")
                        text = (
                            None
                            if response is None
                            else str(response)
                        )
                        output = raw
                    else:
                        text = str(raw)
                        output = {"value": raw}
                elif hasattr(session.agent, "run_conversation"):
                    raw = session.agent.run_conversation(
                        request.prompt,
                        task_id=request.task_id,
                    )
                    if isinstance(raw, dict):
                        response = raw.get("final_response")
                        text = (
                            None
                            if response is None
                            else str(response)
                        )
                        output = raw
                    else:
                        text = str(raw)
                        output = {"value": raw}
                elif hasattr(session.agent, "chat"):
                    response = session.agent.chat(request.prompt)
                    text = (
                        None
                        if response is None
                        else str(response)
                    )
                    output = {"final_response": text}
                    raw = output
                else:
                    raise TypeError(
                        "Hermes agent must provide run_conversation() or chat()"
                    )
            except Exception as exc:
                error = self._redact_text(str(exc))
                completed_at = utc_now()
                with session.lock:
                    session.result = RuntimeResult(
                        session_id=session.handle.session_id,
                        status=RuntimeStatus.FAILED,
                        model_route=session.handle.model_route,
                        runtime_generation=session.handle.runtime_generation,
                        error=error,
                        started_at=started_at,
                        completed_at=completed_at,
                        duration_ms=max(
                            0,
                            int(
                                (
                                    completed_at - started_at
                                ).total_seconds()
                                * 1000
                            ),
                        ),
                    )
                    session.handle = session.handle.model_copy(
                        update={
                            "status": RuntimeStatus.FAILED,
                            "updated_at": utc_now(),
                        }
                    )
                    self._emit(session, "turn.failed", {"error": error})
                    return session.handle

            redacted_output = self._redact_data(output)
            redacted_text = (
                None if text is None else self._redact_text(text)
            )
            interrupted = bool(
                isinstance(raw, dict) and raw.get("interrupted")
            )
            failed = bool(isinstance(raw, dict) and raw.get("failed"))
            if isinstance(raw, dict):
                raw_status = str(raw.get("status") or "").casefold()
                failed = failed or raw_status in {"error", "failed"}
                interrupted = interrupted or raw_status in {
                    "cancelled",
                    "canceled",
                    "interrupted",
                }
            if interrupted:
                result_status = RuntimeStatus.CANCELLED
                handle_status = RuntimeStatus.CANCELLED
                event_type = "turn.cancelled"
            elif failed:
                result_status = RuntimeStatus.FAILED
                handle_status = RuntimeStatus.FAILED
                event_type = "turn.failed"
            else:
                result_status = RuntimeStatus.COMPLETED
                handle_status = RuntimeStatus.IDLE
                event_type = "turn.completed"
            error: str | None = None
            if interrupted or failed:
                raw_error = None
                if isinstance(raw, dict):
                    raw_error = (
                        raw.get("error")
                        or raw.get("failure_reason")
                        or raw.get("interrupt_message")
                    )
                if raw_error is not None:
                    error = self._redact_text(str(raw_error))
                elif failed:
                    error = redacted_text or "Hermes turn failed"

            with session.lock:
                completed_at = utc_now()
                session.result = RuntimeResult(
                    session_id=session.handle.session_id,
                    status=result_status,
                    final_response=redacted_text,
                    model_route=session.handle.model_route,
                    runtime_generation=session.handle.runtime_generation,
                    output=redacted_output,
                    error=error,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=max(
                        0,
                        int(
                            (completed_at - started_at).total_seconds()
                            * 1000
                        ),
                    ),
                )
                session.handle = session.handle.model_copy(
                    update={
                        "status": handle_status,
                        "updated_at": utc_now(),
                    }
                )
                event_payload: dict[str, Any]
                if failed:
                    event_payload = {"error": error}
                elif interrupted:
                    event_payload = {
                        "error": error,
                        "final_response": redacted_text,
                    }
                else:
                    event_payload = {"final_response": redacted_text}
                self._emit(
                    session,
                    event_type,
                    event_payload,
                )
                return session.handle
        finally:
            session.turn_lock.release()

    def _emit(
        self,
        session: _HermesSession,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        with session.lock:
            session.events.append(
                RuntimeEvent(
                    event_id=f"event-{uuid4().hex}",
                    session_id=session.handle.session_id,
                    sequence=(
                        session.event_sequence_base + len(session.events) + 1
                    ),
                    event_type=event_type,
                    payload=self._redact_data(payload),
                )
            )

    def _get(self, handle: RuntimeHandle) -> _HermesSession:
        if handle.runtime_name != self.name:
            raise ValueError("runtime handle belongs to a different adapter")
        with self._lock:
            try:
                session = self._sessions[handle.session_id]
            except KeyError as exc:
                raise KeyError(
                    f"unknown Hermes session: {handle.session_id}"
                ) from exc
        self._validate_session_handle(session, handle)
        return session

    @staticmethod
    def _validate_session_handle(
        session: _HermesSession,
        handle: RuntimeHandle,
    ) -> None:
        expected = session.handle
        if (
            handle.task_id != expected.task_id
            or handle.model_route != expected.model_route
            or handle.runtime_generation != expected.runtime_generation
            or handle.worktree_path != expected.worktree_path
        ):
            raise ValueError(
                "runtime handle identity differs from the live Hermes session"
            )

    def _attest_or_close(
        self,
        agent: Any,
        request: RuntimeRequest,
    ) -> None:
        try:
            self._attest_agent(agent, request)
        except Exception:
            close = getattr(agent, "close", None)
            if callable(close):
                close()
            raise

    def _known_secret_values(self) -> tuple[str, ...]:
        try:
            values = (
                self._known_secrets()
                if callable(self._known_secrets)
                else self._known_secrets
            )
            return tuple(
                value
                for value in values
                if isinstance(value, str) and value
            )
        except Exception:
            return ()

    def _redact_text(self, value: str) -> str:
        return redact_text(
            value,
            known_secrets=self._known_secret_values(),
        )

    def _redact_data(self, value: Any) -> Any:
        return redact_data(
            value,
            known_secrets=self._known_secret_values(),
        )

    @staticmethod
    def _close_agent(agent: Any) -> None:
        close = getattr(agent, "close", None)
        if callable(close):
            close()
