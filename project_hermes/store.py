"""Durable local event store for ProjectHermes development and migration tests.

Production deployments can implement the same ``RunStore`` protocol against
the existing PostgreSQL control plane. SQLite remains a single-host adapter,
not a second production control plane.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Protocol
from uuid import uuid4

from project_hermes.config import (
    ControlPlaneConfig,
    ControlPlaneMode,
    assert_private_directory,
)
from project_hermes.models import IssueTask, ProjectRole, utc_now
from project_hermes.work_graph import (
    AgentEvent,
    LifecycleStatus,
    PipelineRun,
    WorkGraph,
    WorkNode,
)


class RunStore(Protocol):
    """Persistence port consumed by the event-driven controller."""

    def create_run(self, task: IssueTask, *, run_id: str | None = None) -> PipelineRun:
        """Create a durable run."""

    def get_run(self, run_id: str) -> PipelineRun:
        """Read a run projection."""

    def get_run_for_task(self, task_id: str) -> PipelineRun:
        """Read the unique run projection for a task."""

    def complete_run(
        self,
        run_id: str,
        *,
        status: LifecycleStatus = LifecycleStatus.COMPLETED,
    ) -> PipelineRun:
        """Set a terminal run status and durable completion timestamp."""

    def get_task(self, run_id: str) -> IssueTask:
        """Read the locked task snapshot for a run."""

    def add_node(self, node: WorkNode) -> WorkNode:
        """Append one work node."""

    def claim_ready_node(
        self,
        run_id: str,
        *,
        owner_token: str,
        role: ProjectRole,
        lease_seconds: int,
    ) -> WorkNode | None:
        """Atomically claim one dependency-ready node."""

    def transition_node(
        self,
        run_id: str,
        node_id: str,
        target: LifecycleStatus,
        *,
        owner_token: str | None = None,
        output: dict[str, Any] | None = None,
    ) -> WorkNode:
        """Apply a compare-and-set lifecycle transition."""

    def load_graph(self, run_id: str) -> WorkGraph:
        """Load the current graph projection."""

    def list_events(
        self, run_id: str, *, after_sequence: int = 0
    ) -> list[AgentEvent]:
        """Read append-only events."""

    def record_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        node_id: str | None = None,
        session_id: str | None = None,
    ) -> AgentEvent:
        """Append a controller event."""


_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS project_hermes_runs (
    run_id          TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    status          TEXT NOT NULL,
    goal_revision   INTEGER NOT NULL,
    task_json       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_project_hermes_runs_task
ON project_hermes_runs(task_id);

CREATE TABLE IF NOT EXISTS project_hermes_nodes (
    node_id           TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES project_hermes_runs(run_id),
    status            TEXT NOT NULL,
    requested_role    TEXT NOT NULL,
    idempotency_key   TEXT,
    owner_token       TEXT,
    lease_expires_at  TEXT,
    node_json         TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_project_hermes_node_idempotency
ON project_hermes_nodes(run_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_project_hermes_node_ready
ON project_hermes_nodes(run_id, status, requested_role, created_at);

CREATE TABLE IF NOT EXISTS project_hermes_events (
    event_id       TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES project_hermes_runs(run_id),
    node_id        TEXT,
    session_id     TEXT,
    sequence       INTEGER NOT NULL,
    event_type     TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE(run_id, sequence)
);
"""


class SqliteRunStore:
    """SQLite implementation with atomic claims and append-only events."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        assert_private_directory(self.path.parent)
        self._lock = RLock()
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def create_run(
        self,
        task: IssueTask,
        *,
        run_id: str | None = None,
    ) -> PipelineRun:
        task_json = task.model_dump_json()
        run = PipelineRun(
            run_id=run_id or f"run-{uuid4().hex}",
            task_id=task.task_id,
            status=LifecycleStatus.DISCOVERED,
            goal_revision=task.revision,
        )
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM project_hermes_runs
                WHERE task_id = ? OR run_id = ?
                ORDER BY CASE WHEN task_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (task.task_id, run.run_id, task.task_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["task_id"] != task.task_id
                    or existing["task_json"] != task_json
                    or (
                        run_id is not None
                        and existing["run_id"] != run_id
                    )
                ):
                    raise ValueError(
                        "IssueTask run identity already exists with "
                        "different immutable input"
                    )
                return self._run_from_row(existing)
            connection.execute(
                """
                INSERT INTO project_hermes_runs (
                    run_id, task_id, status, goal_revision, task_json,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.task_id,
                    run.status.value,
                    run.goal_revision,
                    task_json,
                    run.created_at.isoformat(),
                    run.updated_at.isoformat(),
                    None,
                ),
            )
            self._append_event_tx(
                connection,
                run.run_id,
                "run.created",
                {"task_id": task.task_id, "goal_revision": task.revision},
            )
        return run

    def get_run(self, run_id: str) -> PipelineRun:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM project_hermes_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        return self._run_from_row(row)

    def get_run_for_task(self, task_id: str) -> PipelineRun:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT run_id FROM project_hermes_runs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown task run: {task_id}")
        return self.get_run(str(row["run_id"]))

    def complete_run(
        self,
        run_id: str,
        *,
        status: LifecycleStatus = LifecycleStatus.COMPLETED,
    ) -> PipelineRun:
        if status not in {
            LifecycleStatus.COMPLETED,
            LifecycleStatus.BLOCKED,
            LifecycleStatus.FAILED,
            LifecycleStatus.CANCELLED,
        }:
            raise ValueError("run completion requires a terminal status")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM project_hermes_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown run: {run_id}")
            if row["completed_at"] is not None:
                if row["status"] != status.value:
                    raise ValueError(
                        "completed run cannot change its terminal status"
                    )
                return self._run_from_row(row)
            completed_at = utc_now()
            connection.execute(
                """
                UPDATE project_hermes_runs
                SET status = ?, updated_at = ?, completed_at = ?
                WHERE run_id = ? AND completed_at IS NULL
                """,
                (
                    status.value,
                    completed_at.isoformat(),
                    completed_at.isoformat(),
                    run_id,
                ),
            )
            self._append_event_tx(
                connection,
                run_id,
                "run.completed",
                {
                    "status": status.value,
                    "completed_at": completed_at.isoformat(),
                },
            )
        return self.get_run(run_id)

    def get_task(self, run_id: str) -> IssueTask:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT task_json FROM project_hermes_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        return IssueTask.model_validate_json(row["task_json"])

    def add_node(self, node: WorkNode) -> WorkNode:
        with self._transaction() as connection:
            self._lock_run_tx(connection, node.run_id)
            graph = self._load_graph_tx(connection, node.run_id)
            graph.add_node(node)
            connection.execute(
                """
                INSERT INTO project_hermes_nodes (
                    node_id, run_id, status, requested_role, idempotency_key,
                    owner_token, lease_expires_at, node_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node.node_id,
                    node.run_id,
                    node.status.value,
                    node.requested_role.value,
                    node.idempotency_key,
                    node.owner_token,
                    node.lease_expires_at.isoformat()
                    if node.lease_expires_at
                    else None,
                    node.model_dump_json(),
                    node.created_at.isoformat(),
                    node.updated_at.isoformat(),
                ),
            )
            self._append_event_tx(
                connection,
                node.run_id,
                "node.created",
                {
                    "kind": node.kind.value,
                    "capability": node.capability,
                    "status": node.status.value,
                },
                node_id=node.node_id,
            )
            self._touch_run_tx(connection, node.run_id)
        return node

    def queue_node(self, run_id: str, node_id: str) -> WorkNode:
        return self.transition_node(
            run_id,
            node_id,
            LifecycleStatus.QUEUED,
        )

    def claim_ready_node(
        self,
        run_id: str,
        *,
        owner_token: str,
        role: ProjectRole,
        lease_seconds: int,
    ) -> WorkNode | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        with self._transaction() as connection:
            self._lock_run_tx(connection, run_id)
            graph = self._load_graph_tx(connection, run_id)
            task_row = connection.execute(
                """
                SELECT task_json FROM project_hermes_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if task_row is None:
                raise KeyError(f"unknown run: {run_id}")
            task = IssueTask.model_validate_json(task_row["task_json"])
            active_count = sum(
                node.status
                in {
                    LifecycleStatus.CLAIMED,
                    LifecycleStatus.RUNNING,
                }
                for node in graph.nodes.values()
            )
            if active_count >= task.resource_limits.max_parallel_nodes:
                return None
            candidates = [
                node_id
                for node_id in graph.ready_node_ids()
                if graph.nodes[node_id].requested_role is role
            ]
            if not candidates:
                return None

            node_id = candidates[0]
            lease_expires = utc_now() + timedelta(seconds=lease_seconds)
            node = graph.transition(
                node_id,
                LifecycleStatus.CLAIMED,
                owner_token=owner_token,
                lease_expires_at=lease_expires,
            )
            cursor = connection.execute(
                """
                UPDATE project_hermes_nodes
                SET status = ?, owner_token = ?, lease_expires_at = ?,
                    node_json = ?, updated_at = ?
                WHERE run_id = ? AND node_id = ? AND status = ?
                  AND owner_token IS NULL
                """,
                (
                    node.status.value,
                    owner_token,
                    lease_expires.isoformat(),
                    node.model_dump_json(),
                    node.updated_at.isoformat(),
                    run_id,
                    node_id,
                    LifecycleStatus.QUEUED.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            self._append_event_tx(
                connection,
                run_id,
                "node.claimed",
                {
                    "owner_token": owner_token,
                    "lease_expires_at": lease_expires.isoformat(),
                },
                node_id=node_id,
            )
            self._touch_run_tx(connection, run_id)
            return node

    def _lock_run_tx(self, connection: Any, run_id: str) -> None:
        """Acquire any adapter-specific per-run transaction lock."""

        del connection, run_id

    def renew_lease(
        self,
        run_id: str,
        node_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> WorkNode:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        with self._transaction() as connection:
            node = self._load_node_tx(connection, run_id, node_id)
            if node.owner_token != owner_token:
                raise PermissionError("node lease is owned by another worker")
            if node.status not in {
                LifecycleStatus.CLAIMED,
                LifecycleStatus.RUNNING,
            }:
                raise ValueError("only claimed or running leases can be renewed")
            lease_expires = utc_now() + timedelta(seconds=lease_seconds)
            updated = WorkNode.model_validate(
                node.model_copy(
                    update={
                        "lease_expires_at": lease_expires,
                        "updated_at": utc_now(),
                    }
                ).model_dump()
            )
            connection.execute(
                """
                UPDATE project_hermes_nodes
                SET lease_expires_at = ?, node_json = ?, updated_at = ?
                WHERE run_id = ? AND node_id = ? AND owner_token = ?
                """,
                (
                    lease_expires.isoformat(),
                    updated.model_dump_json(),
                    updated.updated_at.isoformat(),
                    run_id,
                    node_id,
                    owner_token,
                ),
            )
            self._append_event_tx(
                connection,
                run_id,
                "node.lease_renewed",
                {"lease_expires_at": lease_expires.isoformat()},
                node_id=node_id,
            )
            return updated

    def transition_node(
        self,
        run_id: str,
        node_id: str,
        target: LifecycleStatus,
        *,
        owner_token: str | None = None,
        output: dict[str, Any] | None = None,
    ) -> WorkNode:
        with self._transaction() as connection:
            graph = self._load_graph_tx(connection, run_id)
            current = graph.nodes[node_id]
            if current.owner_token is not None and owner_token != current.owner_token:
                raise PermissionError("node lease is owned by another worker")
            updated = graph.transition(
                node_id,
                target,
                output=output,
                owner_token=current.owner_token,
                lease_expires_at=current.lease_expires_at,
            )
            cursor = connection.execute(
                """
                UPDATE project_hermes_nodes
                SET status = ?, owner_token = ?, lease_expires_at = ?,
                    node_json = ?, updated_at = ?
                WHERE run_id = ? AND node_id = ? AND status = ?
                """,
                (
                    updated.status.value,
                    updated.owner_token,
                    updated.lease_expires_at.isoformat()
                    if updated.lease_expires_at
                    else None,
                    updated.model_dump_json(),
                    updated.updated_at.isoformat(),
                    run_id,
                    node_id,
                    current.status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("node changed concurrently")
            self._append_event_tx(
                connection,
                run_id,
                "node.transitioned",
                {
                    "from": current.status.value,
                    "to": target.value,
                    "output": output or {},
                },
                node_id=node_id,
            )
            self._touch_run_tx(connection, run_id)
            return updated

    def reconcile_expired_leases(self, run_id: str) -> list[str]:
        now = utc_now()
        reclaimed: list[str] = []
        with self._transaction() as connection:
            graph = self._load_graph_tx(connection, run_id)
            for node_id, node in graph.nodes.items():
                if (
                    node.status
                    not in {LifecycleStatus.CLAIMED, LifecycleStatus.RUNNING}
                    or node.lease_expires_at is None
                    or node.lease_expires_at > now
                ):
                    continue
                updated = graph.transition(node_id, LifecycleStatus.QUEUED)
                connection.execute(
                    """
                    UPDATE project_hermes_nodes
                    SET status = ?, owner_token = NULL, lease_expires_at = NULL,
                        node_json = ?, updated_at = ?
                    WHERE run_id = ? AND node_id = ? AND owner_token = ?
                    """,
                    (
                        updated.status.value,
                        updated.model_dump_json(),
                        updated.updated_at.isoformat(),
                        run_id,
                        node_id,
                        node.owner_token,
                    ),
                )
                reclaimed.append(node_id)
                self._append_event_tx(
                    connection,
                    run_id,
                    "node.lease_expired",
                    {"previous_owner_token": node.owner_token},
                    node_id=node_id,
                )
            if reclaimed:
                self._touch_run_tx(connection, run_id)
        return reclaimed

    def load_graph(self, run_id: str) -> WorkGraph:
        with self._connect() as connection:
            return self._load_graph_tx(connection, run_id)

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> list[AgentEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM project_hermes_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (run_id, after_sequence),
            ).fetchall()
        return [
            AgentEvent.model_validate(
                {
                    "event_id": row["event_id"],
                    "run_id": row["run_id"],
                    "node_id": row["node_id"],
                    "session_id": row["session_id"],
                    "sequence": row["sequence"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
            )
            for row in rows
        ]

    def record_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        node_id: str | None = None,
        session_id: str | None = None,
    ) -> AgentEvent:
        """Append a controller event outside a node transition."""

        with self._transaction() as connection:
            event = self._append_event_tx(
                connection,
                run_id,
                event_type,
                payload,
                node_id=node_id,
                session_id=session_id,
            )
            self._touch_run_tx(connection, run_id)
            return event

    def _load_graph_tx(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> WorkGraph:
        run = connection.execute(
            "SELECT * FROM project_hermes_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        rows = connection.execute(
            """
            SELECT node_json FROM project_hermes_nodes
            WHERE run_id = ? ORDER BY created_at ASC, node_id ASC
            """,
            (run_id,),
        ).fetchall()
        nodes = [
            WorkNode.model_validate_json(row["node_json"])
            for row in rows
        ]
        return WorkGraph(
            run_id=run_id,
            task_id=run["task_id"],
            nodes={node.node_id: node for node in nodes},
            created_at=run["created_at"],
            updated_at=run["updated_at"],
        )

    @staticmethod
    def _run_from_row(row: Any) -> PipelineRun:
        return PipelineRun.model_validate(
            {
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "status": row["status"],
                "goal_revision": row["goal_revision"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "completed_at": row["completed_at"],
            }
        )

    @staticmethod
    def _load_node_tx(
        connection: sqlite3.Connection,
        run_id: str,
        node_id: str,
    ) -> WorkNode:
        row = connection.execute(
            """
            SELECT node_json FROM project_hermes_nodes
            WHERE run_id = ? AND node_id = ?
            """,
            (run_id, node_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown work node: {node_id}")
        return WorkNode.model_validate_json(row["node_json"])

    def _append_event_tx(
        self,
        connection: Any,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        node_id: str | None = None,
        session_id: str | None = None,
    ) -> AgentEvent:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) AS last_sequence
            FROM project_hermes_events WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        sequence = int(row["last_sequence"]) + 1
        event = AgentEvent(
            event_id=f"event-{uuid4().hex}",
            run_id=run_id,
            node_id=node_id,
            session_id=session_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
        )
        connection.execute(
            """
            INSERT INTO project_hermes_events (
                event_id, run_id, node_id, session_id, sequence,
                event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.run_id,
                event.node_id,
                event.session_id,
                event.sequence,
                event.event_type,
                json.dumps(
                    event.payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
                event.created_at.isoformat(),
            ),
        )
        return event

    @staticmethod
    def _touch_run_tx(
        connection: sqlite3.Connection,
        run_id: str,
        *,
        status: LifecycleStatus | None = None,
    ) -> None:
        now = utc_now().isoformat()
        if status is None:
            connection.execute(
                """
                UPDATE project_hermes_runs SET updated_at = ?
                WHERE run_id = ?
                """,
                (now, run_id),
            )
            return
        connection.execute(
            """
            UPDATE project_hermes_runs SET status = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (status.value, now, run_id),
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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


def open_run_store(config: ControlPlaneConfig) -> RunStore:
    """Open the configured persistence adapter without silent fallback."""

    if config.mode is ControlPlaneMode.SQLITE:
        return SqliteRunStore(config.sqlite_path)
    if config.mode is ControlPlaneMode.POSTGRES:
        from project_hermes.postgres_store import PostgresRunStore

        if not config.database_url:
            raise ValueError("postgres mode requires database_url")
        return PostgresRunStore(config.database_url)
    raise NotImplementedError(
        f"{config.mode.value} control-plane mode requires a registered "
        "production RunStore adapter"
    )
