"""PostgreSQL adapter for ProjectHermes assurance records."""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Iterator

from project_hermes.assurance_store import SqliteAssuranceStore
from project_hermes.postgres_store import _ConnectionAdapter

_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_hermes_evidence (
    evidence_id      TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    completion_layer TEXT NOT NULL,
    code_diff_sha    TEXT NOT NULL,
    goal_revision    INTEGER NOT NULL,
    content_hash     TEXT NOT NULL,
    evidence_json    TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_hermes_evidence_candidate
ON project_hermes_evidence(task_id, code_diff_sha, goal_revision);

CREATE TABLE IF NOT EXISTS project_hermes_completion (
    task_id          TEXT PRIMARY KEY,
    matrix_json      TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_hermes_reviews (
    review_id        TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    review_role      TEXT NOT NULL,
    code_diff_sha    TEXT NOT NULL,
    goal_revision    INTEGER NOT NULL,
    review_cycle     INTEGER,
    progress_assessment TEXT,
    progress_explanation TEXT,
    review_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE(task_id, review_role, code_diff_sha, goal_revision)
);

ALTER TABLE project_hermes_reviews
ADD COLUMN IF NOT EXISTS review_cycle INTEGER;

ALTER TABLE project_hermes_reviews
ADD COLUMN IF NOT EXISTS progress_assessment TEXT;

ALTER TABLE project_hermes_reviews
ADD COLUMN IF NOT EXISTS progress_explanation TEXT;

CREATE TABLE IF NOT EXISTS project_hermes_review_packets (
    packet_id        TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    code_diff_sha    TEXT NOT NULL,
    goal_revision    INTEGER NOT NULL,
    content_hash     TEXT NOT NULL UNIQUE,
    packet_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_hermes_review_resolutions (
    resolution_id    TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    review_id        TEXT NOT NULL,
    finding_id       TEXT NOT NULL,
    resolution_json  TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE(review_id, finding_id)
);

CREATE INDEX IF NOT EXISTS idx_project_hermes_resolutions_task
ON project_hermes_review_resolutions(task_id, review_id);

CREATE TABLE IF NOT EXISTS project_hermes_review_escalations (
    task_id          TEXT NOT NULL,
    goal_revision    INTEGER NOT NULL,
    status           TEXT NOT NULL,
    state_json       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY(task_id, goal_revision)
);
"""


class PostgresAssuranceStore(SqliteAssuranceStore):
    """Production assurance storage in the control-plane database."""

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
            import psycopg
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL mode requires the project-hermes optional "
                "dependencies"
            ) from exc

        self._integrity_errors = (psycopg.IntegrityError,)
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

    def _lock_completion_tx(
        self,
        connection: _ConnectionAdapter,
        task_id: str,
    ) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (f"completion:{task_id}",),
        )

    @contextmanager
    def _connect(self) -> Iterator[_ConnectionAdapter]:
        with self.pool.connection() as connection:
            yield _ConnectionAdapter(connection)

    @contextmanager
    def _transaction(self) -> Iterator[_ConnectionAdapter]:
        with self._lock, self.pool.connection() as connection:
            with connection.transaction():
                yield _ConnectionAdapter(connection)
