"""Durable bindings between WorkGraph nodes and agent runtimes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Iterator, Literal, Protocol
from uuid import uuid4

from pydantic import Field, model_validator

from project_hermes.accounting import (
    AccountingStore,
    build_runtime_accounting_record,
)
from project_hermes.assurance_store import AssuranceStore
from project_hermes.config import ProjectHermesConfig, assert_private_directory
from project_hermes.models import ProjectRole, StrictModel, utc_now
from project_hermes.runtime.base import (
    RuntimeEvent,
    RuntimeHandle,
    RuntimeModelRoute,
    RuntimeRequest,
    RuntimeResult,
    RuntimeStatus,
)
from project_hermes.runtime.registry import RuntimeRegistry
from project_hermes.store import RunStore


class RuntimeBinding(StrictModel):
    """Serializable controller binding for one runtime session."""

    schema_version: Literal["runtime-binding.v1"] = "runtime-binding.v1"
    binding_id: str
    run_id: str
    task_id: str
    node_id: str | None = None
    role: ProjectRole
    review_cycle: int | None = Field(default=None, ge=1)
    handle: RuntimeHandle
    last_event_sequence: int = Field(default=0, ge=0)
    last_result_status: RuntimeStatus | None = None
    version: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def handle_matches_binding(self) -> "RuntimeBinding":
        if self.handle.task_id != self.task_id:
            raise ValueError("runtime handle belongs to another task")
        return self


class RuntimeBindingStore(Protocol):
    """Persistence port for runtime bindings."""

    def create(self, binding: RuntimeBinding) -> RuntimeBinding:
        """Create a unique runtime binding."""

    def get(self, binding_id: str) -> RuntimeBinding:
        """Read one runtime binding."""

    def find(
        self,
        run_id: str,
        session_id: str,
    ) -> RuntimeBinding | None:
        """Find a binding by durable runtime session identity."""

    def update(
        self,
        binding: RuntimeBinding,
        *,
        expected_version: int,
    ) -> RuntimeBinding:
        """Compare-and-set one runtime binding."""

    def list_for_run(self, run_id: str) -> list[RuntimeBinding]:
        """Read runtime bindings for one run in creation order."""


class SqliteRuntimeBindingStore:
    """Local durable runtime binding store."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS project_hermes_runtime_bindings (
        binding_id          TEXT PRIMARY KEY,
        run_id             TEXT NOT NULL,
        task_id            TEXT NOT NULL,
        node_id            TEXT,
        runtime_name       TEXT NOT NULL,
        session_id         TEXT NOT NULL,
        status             TEXT NOT NULL,
        version            INTEGER NOT NULL,
        binding_json       TEXT NOT NULL,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        UNIQUE(run_id, session_id)
    );
    CREATE INDEX IF NOT EXISTS idx_project_hermes_bindings_active
    ON project_hermes_runtime_bindings(run_id, status, updated_at);
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        assert_private_directory(self.path.parent)
        self._lock = RLock()
        with self._connect() as connection:
            connection.executescript(self._SCHEMA)

    def create(self, binding: RuntimeBinding) -> RuntimeBinding:
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT binding_json
                FROM project_hermes_runtime_bindings
                WHERE run_id = ? AND session_id = ?
                """,
                (binding.run_id, binding.handle.session_id),
            ).fetchone()
            if existing is not None:
                restored = RuntimeBinding.model_validate_json(
                    existing["binding_json"]
                )
                if (
                    restored.task_id != binding.task_id
                    or restored.handle.runtime_name
                    != binding.handle.runtime_name
                ):
                    raise ValueError(
                        "runtime session identity was reused incompatibly"
                    )
                raise ValueError(
                    f"runtime session is already bound: "
                    f"{restored.handle.session_id}"
                )
            connection.execute(
                """
                INSERT INTO project_hermes_runtime_bindings (
                    binding_id, run_id, task_id, node_id, runtime_name,
                    session_id, status, version, binding_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.binding_id,
                    binding.run_id,
                    binding.task_id,
                    binding.node_id,
                    binding.handle.runtime_name,
                    binding.handle.session_id,
                    binding.handle.status.value,
                    binding.version,
                    binding.model_dump_json(),
                    binding.created_at.isoformat(),
                    binding.updated_at.isoformat(),
                ),
            )
        return binding

    def get(self, binding_id: str) -> RuntimeBinding:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT binding_json
                FROM project_hermes_runtime_bindings
                WHERE binding_id = ?
                """,
                (binding_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown runtime binding: {binding_id}")
        return RuntimeBinding.model_validate_json(row["binding_json"])

    def find(
        self,
        run_id: str,
        session_id: str,
    ) -> RuntimeBinding | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT binding_json
                FROM project_hermes_runtime_bindings
                WHERE run_id = ? AND session_id = ?
                """,
                (run_id, session_id),
            ).fetchone()
        if row is None:
            return None
        return RuntimeBinding.model_validate_json(row["binding_json"])

    def update(
        self,
        binding: RuntimeBinding,
        *,
        expected_version: int,
    ) -> RuntimeBinding:
        updated = binding.model_copy(
            update={
                "version": expected_version + 1,
                "updated_at": utc_now(),
            }
        )
        updated = RuntimeBinding.model_validate(updated.model_dump())
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE project_hermes_runtime_bindings
                SET status = ?, version = ?, binding_json = ?, updated_at = ?
                WHERE binding_id = ? AND version = ?
                """,
                (
                    updated.handle.status.value,
                    updated.version,
                    updated.model_dump_json(),
                    updated.updated_at.isoformat(),
                    updated.binding_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("runtime binding changed concurrently")
        return updated

    def list_for_run(self, run_id: str) -> list[RuntimeBinding]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT binding_json
                FROM project_hermes_runtime_bindings
                WHERE run_id = ?
                ORDER BY created_at ASC, binding_id ASC
                """,
                (run_id,),
            ).fetchall()
        return [
            RuntimeBinding.model_validate_json(row["binding_json"])
            for row in rows
        ]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()


class SessionCoordinator:
    """Drive runtime turns while persisting only evidence-safe metadata."""

    def __init__(
        self,
        runtimes: RuntimeRegistry,
        bindings: RuntimeBindingStore,
        runs: RunStore,
        accounting: AccountingStore | None = None,
    ) -> None:
        self.runtimes = runtimes
        self.bindings = bindings
        self.runs = runs
        self.accounting = accounting
        self._lock = RLock()
        self._turn_locks: dict[str, Any] = {}
        self._sync_locks: dict[str, Any] = {}

    def start(
        self,
        run_id: str,
        runtime_name: str,
        request: RuntimeRequest,
        *,
        node_id: str | None = None,
    ) -> tuple[RuntimeBinding, RuntimeResult | None]:
        task = self.runs.get_task(run_id)
        if request.task_id != task.task_id:
            raise ValueError("runtime request belongs to another task")
        if node_id is not None:
            graph = self.runs.load_graph(run_id)
            try:
                node = graph.nodes[node_id]
            except KeyError as exc:
                raise KeyError(f"unknown work node: {node_id}") from exc
            if node.requested_role is not request.role:
                raise PermissionError(
                    "runtime role does not match the WorkGraph node"
                )
        runtime = self.runtimes.get(runtime_name)
        session_id = request.session_id or (
            f"{runtime.name}-{uuid4().hex}"
        )
        request = request.model_copy(update={"session_id": session_id})
        binding = self.bindings.create(
            RuntimeBinding(
                binding_id=f"binding-{uuid4().hex}",
                run_id=run_id,
                task_id=task.task_id,
                node_id=node_id,
                role=request.role,
                review_cycle=request.review_cycle,
                handle=RuntimeHandle(
                    runtime_name=runtime.name,
                    session_id=session_id,
                    task_id=task.task_id,
                    status=RuntimeStatus.STARTING,
                ),
            )
        )
        try:
            handle = runtime.start(request)
        except Exception:
            failed = binding.handle.model_copy(
                update={
                    "status": RuntimeStatus.FAILED,
                    "updated_at": utc_now(),
                }
            )
            self.bindings.update(
                binding.model_copy(update={"handle": failed}),
                expected_version=binding.version,
            )
            raise
        if (
            handle.session_id != session_id
            or handle.runtime_name != runtime.name
            or handle.task_id != task.task_id
        ):
            with suppress(Exception):
                runtime.close(handle)
            failed = binding.handle.model_copy(
                update={
                    "status": RuntimeStatus.FAILED,
                    "updated_at": utc_now(),
                }
            )
            self.bindings.update(
                binding.model_copy(update={"handle": failed}),
                expected_version=binding.version,
            )
            raise ValueError(
                "runtime returned an identity that differs from its "
                "controller reservation"
            )
        binding = self.bindings.update(
            binding.model_copy(update={"handle": handle}),
            expected_version=binding.version,
        )
        return self._synchronize(binding)

    def resume(
        self,
        binding_id: str,
        request: RuntimeRequest,
    ) -> tuple[RuntimeBinding, RuntimeResult | None]:
        turn_lock = self._binding_lock(
            self._turn_locks,
            binding_id,
        )
        if not turn_lock.acquire(blocking=False):
            raise RuntimeError("a runtime turn is already in progress")
        try:
            binding = self.bindings.get(binding_id)
            self._validate_request(binding, request)
            runtime = self.runtimes.get(binding.handle.runtime_name)
            connected = runtime.reconnect(binding.handle, request)
            handle = runtime.resume(connected, request)
            current = self.bindings.get(binding_id)
            self._validate_runtime_identity(current, handle)
            binding = self.bindings.update(
                current.model_copy(update={"handle": handle}),
                expected_version=current.version,
            )
            sync_lock = self._binding_lock(
                self._sync_locks,
                binding_id,
            )
            with sync_lock:
                return self._synchronize(binding)
        finally:
            turn_lock.release()

    def reconnect(
        self,
        binding_id: str,
        request: RuntimeRequest,
    ) -> RuntimeBinding:
        sync_lock = self._binding_lock(
            self._sync_locks,
            binding_id,
        )
        with sync_lock:
            binding = self.bindings.get(binding_id)
            self._validate_request(binding, request)
            runtime = self.runtimes.get(binding.handle.runtime_name)
            handle = runtime.reconnect(binding.handle, request)
            self._validate_runtime_identity(binding, handle)
            return self.bindings.update(
                binding.model_copy(update={"handle": handle}),
                expected_version=binding.version,
            )

    def cancel(self, binding_id: str) -> RuntimeBinding:
        binding = self.bindings.get(binding_id)
        runtime = self.runtimes.get(binding.handle.runtime_name)
        handle = runtime.cancel(binding.handle)
        current = self.bindings.get(binding_id)
        self._validate_runtime_identity(current, handle)
        return self.bindings.update(
            current.model_copy(update={"handle": handle}),
            expected_version=current.version,
        )

    def close(self, binding_id: str) -> RuntimeBinding:
        turn_lock = self._binding_lock(
            self._turn_locks,
            binding_id,
        )
        if not turn_lock.acquire(blocking=False):
            raise RuntimeError(
                "cannot close a runtime while a turn is in progress"
            )
        try:
            binding = self.bindings.get(binding_id)
            runtime = self.runtimes.get(binding.handle.runtime_name)
            handle = runtime.close(binding.handle)
            self._validate_runtime_identity(binding, handle)
            return self.bindings.update(
                binding.model_copy(update={"handle": handle}),
                expected_version=binding.version,
            )
        finally:
            turn_lock.release()

    def reprovision_codex(
        self,
        binding_id: str,
        request: RuntimeRequest,
        *,
        locked_issue_contract: str,
        review_findings: list[str],
        escalation_explanation: str,
    ) -> tuple[RuntimeBinding, RuntimeResult | None]:
        """Replace a primary Codex daemon with one fresh escalated session."""

        if not locked_issue_contract.strip():
            raise ValueError("locked_issue_contract is required for reprovisioning")
        if not review_findings:
            raise ValueError("review findings are required for reprovisioning")
        if not escalation_explanation.strip():
            raise ValueError("escalation explanation is required")
        if request.role is not ProjectRole.CODEX:
            raise PermissionError("only Codex bindings may be reprovisioned")
        if request.model_route is None:
            raise ValueError("reprovisioning requires an explicit model profile")
        if request.runtime_generation != 1:
            raise ValueError("automatic Codex reprovisioning must use generation 1")

        turn_lock = self._binding_lock(self._turn_locks, binding_id)
        if not turn_lock.acquire(blocking=False):
            raise RuntimeError(
                "cannot reprovision Codex while a turn is in progress"
            )
        try:
            binding = self.bindings.get(binding_id)
            self._validate_request(binding, request)
            if binding.role is not ProjectRole.CODEX:
                raise PermissionError("runtime binding is not a Codex session")
            if binding.handle.status is RuntimeStatus.CLOSED:
                raise RuntimeError(
                    "the primary Codex binding was already closed for "
                    "reprovisioning"
                )
            if binding.handle.runtime_generation != 0:
                raise RuntimeError(
                    "Codex was already automatically reprovisioned"
                )
            if request.model_route == binding.handle.model_route:
                raise ValueError(
                    "reprovisioning requires a different model profile"
                )
            if (
                binding.handle.worktree_path
                and request.cwd
                and Path(request.cwd).resolve()
                != Path(binding.handle.worktree_path).resolve()
            ):
                raise ValueError(
                    "reprovisioned Codex must use the original worktree"
                )
            runtime = self.runtimes.get(binding.handle.runtime_name)
            closed_handle = runtime.close(binding.handle)
            self._validate_runtime_identity(binding, closed_handle)
            closed = self.bindings.update(
                binding.model_copy(update={"handle": closed_handle}),
                expected_version=binding.version,
            )
        finally:
            turn_lock.release()

        escalation_context = json.dumps(
            {
                "locked_issue_contract": locked_issue_contract,
                "review_findings": review_findings,
                "reviewer_explanation": escalation_explanation,
            },
            sort_keys=True,
            ensure_ascii=True,
        )
        escalated_request = request.model_copy(
            update={
                "session_id": (
                    f"{closed.handle.runtime_name}-escalated-{uuid4().hex}"
                ),
                "cwd": request.cwd or closed.handle.worktree_path,
                "prompt": (
                    "Continue implementation in the existing worktree using "
                    "the frozen issue contract and reviewer findings below. "
                    "Do not broaden the goal or discard valid existing work.\n\n"
                    f"{escalation_context}\n\n"
                    f"Requested next action:\n{request.prompt}"
                ),
                "metadata": {
                    **request.metadata,
                    "escalated_from_session_id": closed.handle.session_id,
                    "escalation_kind": "review_no_progress",
                },
            }
        )
        return self.start(
            closed.run_id,
            closed.handle.runtime_name,
            escalated_request,
            node_id=closed.node_id,
        )

    def synchronize(
        self,
        binding_id: str,
    ) -> tuple[RuntimeBinding, RuntimeResult | None]:
        sync_lock = self._binding_lock(
            self._sync_locks,
            binding_id,
        )
        with sync_lock:
            return self._synchronize(self.bindings.get(binding_id))

    def _binding_lock(
        self,
        locks: dict[str, Any],
        binding_id: str,
    ) -> Any:
        with self._lock:
            return locks.setdefault(binding_id, Lock())

    def _synchronize(
        self,
        binding: RuntimeBinding,
    ) -> tuple[RuntimeBinding, RuntimeResult | None]:
        runtime = self.runtimes.get(binding.handle.runtime_name)
        events = tuple(
            runtime.events(
                binding.handle,
                after_sequence=binding.last_event_sequence,
            )
        )
        sequence = binding.last_event_sequence
        for event in events:
            if event.session_id != binding.handle.session_id:
                raise ValueError("runtime event belongs to another session")
            if event.sequence <= sequence:
                raise ValueError("runtime events are not strictly ordered")
            self.runs.record_event(
                binding.run_id,
                f"runtime.{event.event_type}",
                _event_receipt(event),
                node_id=binding.node_id,
                session_id=binding.handle.session_id,
            )
            sequence = event.sequence

        result = runtime.result(binding.handle)
        if result is not None and result.session_id != binding.handle.session_id:
            raise ValueError("runtime result belongs to another session")
        if result is not None and self.accounting is not None:
            self.accounting.append(
                build_runtime_accounting_record(
                    task_id=binding.task_id,
                    run_id=binding.run_id,
                    node_id=binding.node_id,
                    role=binding.role,
                    review_cycle=binding.review_cycle,
                    runtime_name=binding.handle.runtime_name,
                    result=result,
                    observed_at=binding.handle.updated_at,
                )
            )
        update = {
            "last_event_sequence": sequence,
            "last_result_status": result.status if result else None,
        }
        if sequence != binding.last_event_sequence or (
            result is not None
            and result.status is not binding.last_result_status
        ):
            binding = self.bindings.update(
                binding.model_copy(update=update),
                expected_version=binding.version,
            )
        return binding, result

    @staticmethod
    def _validate_request(
        binding: RuntimeBinding,
        request: RuntimeRequest,
    ) -> None:
        if request.task_id != binding.task_id:
            raise ValueError("runtime request belongs to another task")
        if request.role is not binding.role:
            raise PermissionError("runtime request changed the bound role")
        if request.session_id not in {None, binding.handle.session_id}:
            raise ValueError("runtime request names another session")

    @staticmethod
    def _validate_runtime_identity(
        binding: RuntimeBinding,
        handle: RuntimeHandle,
    ) -> None:
        if (
            handle.runtime_name != binding.handle.runtime_name
            or handle.session_id != binding.handle.session_id
            or handle.task_id != binding.task_id
        ):
            raise ValueError(
                "runtime returned an identity that differs from its binding"
            )


def _event_receipt(event: RuntimeEvent) -> dict[str, object]:
    """Persist event integrity without retaining raw model content."""

    serialized = json.dumps(
        event.payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return {
        "runtime_event_id": event.event_id,
        "runtime_sequence": event.sequence,
        "payload_sha256": hashlib.sha256(
            serialized.encode("utf-8")
        ).hexdigest(),
        "payload_keys": sorted(event.payload),
        "created_at": event.created_at.isoformat(),
    }


def codex_escalation_handler(
    coordinator: SessionCoordinator,
    bindings: RuntimeBindingStore,
    assurance: AssuranceStore,
    config: ProjectHermesConfig,
) -> Any:
    """Build the controller callback for one-time Opus reprovisioning."""

    route = RuntimeModelRoute.model_validate(
        config.codex.route(config.codex.escalation_profile).model_dump()
    )

    def handle(run_id: str, task: Any, decision: Any) -> RuntimeBinding:
        candidates = [
            binding
            for binding in bindings.list_for_run(run_id)
            if binding.role is ProjectRole.CODEX
            and binding.handle.runtime_generation == 0
            and binding.handle.status is not RuntimeStatus.CLOSED
        ]
        if not candidates:
            raise RuntimeError(
                "no primary Codex runtime binding is available to escalate"
            )
        primary = candidates[-1]
        reviews = [
            assurance.get_review(review_id)
            for review_id in decision.review_ids
        ]
        findings = [
            (
                f"{review.role.value} cycle {review.review_cycle}: "
                f"{review.progress_explanation or review.summary}"
            )
            for review in reviews
        ]
        for review in reviews:
            findings.extend(
                (
                    f"{review.role.value} finding {finding.finding_id}: "
                    f"{finding.title} - {finding.description}"
                )
                for finding in review.findings
            )
        request = RuntimeRequest(
            request_id=f"escalate-{uuid4().hex}",
            task_id=task.task_id,
            role=ProjectRole.CODEX,
            prompt=(
                "Address the latest reviewer explanation and run the "
                "required verification."
            ),
            cwd=primary.handle.worktree_path,
            model_route=route,
            runtime_generation=1,
            review_cycle=decision.review_cycle,
        )
        binding, _result = coordinator.reprovision_codex(
            primary.binding_id,
            request,
            locked_issue_contract=task.model_dump_json(),
            review_findings=findings,
            escalation_explanation=(
                decision.reviewer_explanation or reviews[-1].summary
            ),
        )
        return binding

    return handle
