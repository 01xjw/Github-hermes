"""PostgreSQL adapter for the ProjectHermes RunStore."""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Any, Iterator

from project_hermes.store import SqliteRunStore
from project_hermes.work_graph import AgentEvent

_POSTGRES_SCHEMA = """
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
    updated_at        TEXT NOT NULL,
    UNIQUE(run_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_project_hermes_nodes_ready
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

CREATE INDEX IF NOT EXISTS idx_project_hermes_events_run
ON project_hermes_events(run_id, sequence);
"""


class _ConnectionAdapter:
    def __init__(self, connection: Any) -> None:
        self.raw = connection

    def execute(
        self,
        statement: str,
        parameters: Any = (),
    ) -> Any:
        return self.raw.execute(statement.replace("?", "%s"), parameters)


class PostgresRunStore(SqliteRunStore):
    """Production RunStore with pooled PostgreSQL transactions."""

    def __init__(
        self,
        dsn: str,
        *,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
    ) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL mode requires the project-hermes optional "
                "dependencies"
            ) from exc

        self.dsn = dsn
        self._lock = RLock()
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=max(1, min_pool_size),
            max_size=max(min_pool_size, max_pool_size),
            kwargs={"row_factory": dict_row},
            open=True,
        )
        self.pool.wait(timeout=15)
        with self._connect() as connection:
            for statement in _POSTGRES_SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def _connect(self) -> Iterator[_ConnectionAdapter]:
        with self.pool.connection() as connection:
            yield _ConnectionAdapter(connection)

    @contextmanager
    def _transaction(self) -> Iterator[_ConnectionAdapter]:
        with self._lock, self.pool.connection() as connection:
            with connection.transaction():
                yield _ConnectionAdapter(connection)

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
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (run_id,),
        )
        return super()._append_event_tx(
            connection,
            run_id,
            event_type,
            payload,
            node_id=node_id,
            session_id=session_id,
        )

    def _lock_run_tx(
        self,
        connection: Any,
        run_id: str,
    ) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (f"run:{run_id}",),
        )
