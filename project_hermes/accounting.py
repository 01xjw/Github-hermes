"""Append-only timing, token, and model-API cost accounting."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from typing import Any, Iterator, Literal, Protocol

from pydantic import Field, model_validator

from agent.usage_pricing import (
    CanonicalUsage,
    estimate_usage_cost,
    normalize_usage,
)
from project_hermes.config import (
    ControlPlaneConfig,
    ControlPlaneMode,
    assert_private_directory,
)
from project_hermes.models import ProjectRole, StrictModel, utc_now
from project_hermes.runtime.base import RuntimeResult


class AccountingKind(StrEnum):
    """Kinds of active work shown in a task round breakdown."""

    MODEL_TURN = "model_turn"
    EXECUTION = "execution"


class AccountingCostStatus(StrEnum):
    """Whether a round's model API cost is complete."""

    ESTIMATED = "estimated"
    ACTUAL = "actual"
    INCLUDED = "included"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class AccountingRecord(StrictModel):
    """One immutable, prose-free active-work accounting record."""

    schema_version: Literal["accounting-record.v1"] = "accounting-record.v1"
    record_id: str
    kind: AccountingKind
    task_id: str
    run_id: str
    node_id: str | None = None
    session_id: str
    native_turn_id: str | None = None
    role: ProjectRole
    review_cycle: int | None = Field(default=None, ge=1)
    runtime_name: str | None = None
    model_profile: str | None = None
    model: str | None = None
    model_provider: str | None = None
    reasoning_mode: str | None = None
    runtime_generation: int = Field(default=0, ge=0, le=1)
    outcome: str
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    api_calls: int = Field(default=0, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    cost_status: AccountingCostStatus
    cost_source: str
    pricing_version: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @model_validator(mode="after")
    def timing_and_cost_are_consistent(self) -> "AccountingRecord":
        if self.completed_at < self.started_at:
            raise ValueError("accounting completion cannot precede its start")
        if self.kind is AccountingKind.EXECUTION:
            if (
                self.model is not None
                or self.model_profile is not None
                or self.estimated_cost_usd is not None
                or self.cost_status
                is not AccountingCostStatus.NOT_APPLICABLE
            ):
                raise ValueError(
                    "execution accounting cannot include model API cost"
                )
        elif self.cost_status in {
            AccountingCostStatus.ESTIMATED,
            AccountingCostStatus.ACTUAL,
            AccountingCostStatus.INCLUDED,
        } and self.estimated_cost_usd is None:
            raise ValueError("priced model turns require a USD amount")
        return self


class TaskAccountingSummary(StrictModel):
    """Wall-clock task total plus ordered active-work records."""

    schema_version: Literal["task-accounting-summary.v1"] = (
        "task-accounting-summary.v1"
    )
    task_id: str
    run_id: str
    started_at: datetime
    completed_at: datetime | None = None
    wall_clock_duration_ms: int = Field(ge=0)
    active_duration_ms: int = Field(ge=0)
    estimated_llm_cost_usd: float = Field(ge=0)
    cost_complete: bool
    unknown_cost_rounds: int = Field(ge=0)
    rounds: list[AccountingRecord] = Field(default_factory=list)


class AccountingStore(Protocol):
    """Persistence port for immutable accounting records."""

    def append(self, record: AccountingRecord) -> AccountingRecord:
        """Append an immutable record idempotently."""

    def list_records(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
    ) -> list[AccountingRecord]:
        """Read ordered accounting records for a task or run."""


_SQLITE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS project_hermes_accounting (
    record_id          TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL,
    run_id             TEXT NOT NULL,
    node_id            TEXT,
    session_id         TEXT NOT NULL,
    kind               TEXT NOT NULL,
    role               TEXT NOT NULL,
    review_cycle       INTEGER,
    model_profile      TEXT,
    model              TEXT,
    started_at         TEXT NOT NULL,
    completed_at       TEXT NOT NULL,
    duration_ms        INTEGER NOT NULL,
    estimated_cost_usd REAL,
    cost_status        TEXT NOT NULL,
    record_json        TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_hermes_accounting_task
ON project_hermes_accounting(task_id, run_id, started_at, record_id);
"""

_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_hermes_accounting (
    record_id          TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL,
    run_id             TEXT NOT NULL,
    node_id            TEXT,
    session_id         TEXT NOT NULL,
    kind               TEXT NOT NULL,
    role               TEXT NOT NULL,
    review_cycle       INTEGER,
    model_profile      TEXT,
    model              TEXT,
    started_at         TEXT NOT NULL,
    completed_at       TEXT NOT NULL,
    duration_ms        BIGINT NOT NULL,
    estimated_cost_usd DOUBLE PRECISION,
    cost_status        TEXT NOT NULL,
    record_json        TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_hermes_accounting_task
ON project_hermes_accounting(task_id, run_id, started_at, record_id);
"""


class SqliteAccountingStore:
    """SQLite accounting adapter for local development and migration tests."""

    _integrity_errors: tuple[type[BaseException], ...] = (
        sqlite3.IntegrityError,
    )

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        assert_private_directory(self.path.parent)
        self._lock = RLock()
        with self._connect() as connection:
            connection.executescript(_SQLITE_SCHEMA)

    def append(self, record: AccountingRecord) -> AccountingRecord:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT record_json
                FROM project_hermes_accounting
                WHERE record_id = ?
                """,
                (record.record_id,),
            ).fetchone()
            if row is not None:
                stored = AccountingRecord.model_validate_json(
                    row["record_json"]
                )
                if stored != record:
                    raise ValueError(
                        "accounting record id was reused with different data"
                    )
                return stored
            try:
                connection.execute(
                    """
                    INSERT INTO project_hermes_accounting (
                        record_id, task_id, run_id, node_id, session_id,
                        kind, role, review_cycle, model_profile, model,
                        started_at, completed_at, duration_ms,
                        estimated_cost_usd, cost_status, record_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.record_id,
                        record.task_id,
                        record.run_id,
                        record.node_id,
                        record.session_id,
                        record.kind.value,
                        record.role.value,
                        record.review_cycle,
                        record.model_profile,
                        record.model,
                        record.started_at.isoformat(),
                        record.completed_at.isoformat(),
                        record.duration_ms,
                        record.estimated_cost_usd,
                        record.cost_status.value,
                        record.model_dump_json(),
                        record.created_at.isoformat(),
                    ),
                )
            except self._integrity_errors as exc:
                raise ValueError("accounting record already exists") from exc
        return record

    def list_records(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
    ) -> list[AccountingRecord]:
        query = """
            SELECT record_json
            FROM project_hermes_accounting
            WHERE task_id = ?
        """
        parameters: tuple[Any, ...] = (task_id,)
        if run_id is not None:
            query += " AND run_id = ?"
            parameters = (task_id, run_id)
        query += " ORDER BY started_at ASC, record_id ASC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            AccountingRecord.model_validate_json(row["record_json"])
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


class PostgresAccountingStore(SqliteAccountingStore):
    """PostgreSQL accounting adapter in the control-plane database."""

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
                "PostgreSQL accounting requires the project-hermes "
                "optional dependencies"
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

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        from project_hermes.postgres_store import _ConnectionAdapter

        with self.pool.connection() as connection:
            yield _ConnectionAdapter(connection)

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        from project_hermes.postgres_store import _ConnectionAdapter

        with self._lock, self.pool.connection() as connection:
            with connection.transaction():
                yield _ConnectionAdapter(connection)


def open_accounting_store(config: ControlPlaneConfig) -> AccountingStore:
    """Open the accounting adapter selected by the control-plane config."""

    if config.mode is ControlPlaneMode.SQLITE:
        return SqliteAccountingStore(config.sqlite_path)
    if config.mode is ControlPlaneMode.POSTGRES:
        if not config.database_url:
            raise ValueError("postgres mode requires database_url")
        return PostgresAccountingStore(config.database_url)
    raise NotImplementedError(
        f"{config.mode.value} control-plane mode requires a registered "
        "AccountingStore adapter"
    )


def build_runtime_accounting_record(
    *,
    task_id: str,
    run_id: str,
    node_id: str | None,
    role: ProjectRole,
    review_cycle: int | None,
    runtime_name: str,
    result: RuntimeResult,
    observed_at: datetime | None = None,
) -> AccountingRecord:
    """Normalize numeric runtime metrics without retaining model prose."""

    usage = normalize_runtime_usage(
        result.output,
        provider=(
            result.model_route.model_provider
            if result.model_route is not None
            else None
        ),
        wire_api=(
            result.model_route.provider_wire_api
            if result.model_route is not None
            else None
        ),
    )
    completed_at = result.completed_at or observed_at or utc_now()
    duration_ms = result.duration_ms
    if duration_ms is None and result.started_at is not None:
        duration_ms = max(
            0,
            int((completed_at - result.started_at).total_seconds() * 1000),
        )
    duration_ms = duration_ms or 0
    started_at = result.started_at or (
        completed_at
        if duration_ms == 0
        else datetime.fromtimestamp(
            completed_at.timestamp() - duration_ms / 1000,
            tz=completed_at.tzinfo,
        )
    )

    route = result.model_route
    if route is None:
        amount = None
        cost_status = AccountingCostStatus.UNKNOWN
        cost_source = "none"
        pricing_version = None
    else:
        cost = estimate_usage_cost(
            route.model,
            usage,
            provider=route.model_provider,
            base_url=route.provider_endpoint,
        )
        amount = float(cost.amount_usd) if cost.amount_usd is not None else None
        cost_status = AccountingCostStatus(cost.status)
        cost_source = cost.source
        pricing_version = cost.pricing_version

    identity = "|".join(
        (
            run_id,
            result.session_id,
            result.native_turn_id or completed_at.isoformat(),
            str(review_cycle or 0),
        )
    )
    return AccountingRecord(
        record_id="accounting-" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest(),
        kind=AccountingKind.MODEL_TURN,
        task_id=task_id,
        run_id=run_id,
        node_id=node_id,
        session_id=result.session_id,
        native_turn_id=result.native_turn_id,
        role=role,
        review_cycle=review_cycle,
        runtime_name=runtime_name,
        model_profile=route.profile if route else None,
        model=route.model if route else None,
        model_provider=route.model_provider if route else None,
        reasoning_mode=route.reasoning_mode.value if route else None,
        runtime_generation=result.runtime_generation,
        outcome=result.status.value,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        api_calls=usage.request_count if _has_usage(usage) else 0,
        estimated_cost_usd=amount,
        cost_status=cost_status,
        cost_source=cost_source,
        pricing_version=pricing_version,
        created_at=completed_at,
    )


def build_execution_accounting_record(
    *,
    task_id: str,
    run_id: str,
    node_id: str,
    execution_id: str,
    outcome: str,
    started_at: datetime,
    completed_at: datetime,
) -> AccountingRecord:
    """Build an execution-time record that never contributes model cost."""

    duration_ms = max(
        0,
        int((completed_at - started_at).total_seconds() * 1000),
    )
    return AccountingRecord(
        record_id=f"accounting-execution-{execution_id}",
        kind=AccountingKind.EXECUTION,
        task_id=task_id,
        run_id=run_id,
        node_id=node_id,
        session_id=execution_id,
        role=ProjectRole.RUNNER,
        outcome=outcome,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        cost_status=AccountingCostStatus.NOT_APPLICABLE,
        cost_source="none",
        created_at=completed_at,
    )


def summarize_accounting(
    *,
    task_id: str,
    run_id: str,
    task_started_at: datetime,
    task_completed_at: datetime | None,
    records: list[AccountingRecord],
    now: datetime | None = None,
) -> TaskAccountingSummary:
    """Separate true task wall time from summed active round duration."""

    end = task_completed_at or now or utc_now()
    ordered = sorted(records, key=lambda record: (record.started_at, record.record_id))
    model_records = [
        record
        for record in ordered
        if record.kind is AccountingKind.MODEL_TURN
    ]
    unknown = sum(
        record.cost_status is AccountingCostStatus.UNKNOWN
        for record in model_records
    )
    return TaskAccountingSummary(
        task_id=task_id,
        run_id=run_id,
        started_at=task_started_at,
        completed_at=task_completed_at,
        wall_clock_duration_ms=max(
            0,
            int((end - task_started_at).total_seconds() * 1000),
        ),
        active_duration_ms=sum(record.duration_ms for record in ordered),
        estimated_llm_cost_usd=sum(
            record.estimated_cost_usd or 0 for record in model_records
        ),
        cost_complete=unknown == 0,
        unknown_cost_rounds=unknown,
        rounds=ordered,
    )


def normalize_runtime_usage(
    output: dict[str, Any],
    *,
    provider: str | None,
    wire_api: str | None,
) -> CanonicalUsage:
    """Normalize supported SDK usage shapes into numeric token buckets."""

    raw = output.get("usage")
    if raw is None:
        raw = output.get("token_usage")
    if raw is None and isinstance(output.get("metrics"), dict):
        raw = output["metrics"].get("usage")
    if not isinstance(raw, dict):
        return CanonicalUsage(request_count=0)
    mode = wire_api or ""
    if "input_tokens" in raw and "prompt_tokens" not in raw:
        mode = "codex_responses"
    normalized = normalize_usage(
        _to_namespace(raw),
        provider=provider,
        api_mode=mode,
    )
    request_count = _nonnegative_int(
        raw.get("request_count", raw.get("api_calls", 1))
    )
    return CanonicalUsage(
        input_tokens=normalized.input_tokens,
        output_tokens=normalized.output_tokens,
        cache_read_tokens=normalized.cache_read_tokens,
        cache_write_tokens=normalized.cache_write_tokens,
        reasoning_tokens=normalized.reasoning_tokens,
        request_count=request_count,
    )


def _to_namespace(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(
            **{key: _to_namespace(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return [_to_namespace(item) for item in value]
    return value


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _has_usage(usage: CanonicalUsage) -> bool:
    return bool(
        usage.input_tokens
        or usage.output_tokens
        or usage.cache_read_tokens
        or usage.cache_write_tokens
        or usage.reasoning_tokens
    )
