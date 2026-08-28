"""Durable polling, candidate, and Work projections for ProjectHermes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Literal, Sequence

from pydantic import Field, field_validator, model_validator

from project_hermes.config import PollingConfig, assert_private_directory
from project_hermes.models import StrictModel, utc_now
from project_hermes.redaction import redact_data, redact_text
from project_hermes.runtime.base import RuntimeHandle


class PollingTaskStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PARTIAL = "partial"
    FAILED = "failed"


class PollingRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class RepositoryScanStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class WorkStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    REVIEW = "review"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"


class EnvironmentStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"


OPERATOR_SELECTION_HOLD_REASON = "Awaiting operator selection in guided training mode."


TERMINAL_WORK_STATUSES = frozenset({
    WorkStatus.DONE,
    WorkStatus.BLOCKED,
    WorkStatus.FAILED,
})


class PollingTask(StrictModel):
    task_id: Literal["github-issue-polling"] = "github-issue-polling"
    enabled: bool
    interval_seconds: int = Field(ge=30)
    status: PollingTaskStatus = PollingTaskStatus.IDLE
    next_run_at: datetime | None = None
    last_run_id: str | None = None
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class PollingRepository(StrictModel):
    repository_id: int = Field(ge=1)
    repository: str
    enabled: bool = True
    default_branch: str
    html_url: str
    last_scanned_at: datetime | None = None
    last_status: RepositoryScanStatus | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class PollingRun(StrictModel):
    run_id: str
    task_id: str = "github-issue-polling"
    status: PollingRunStatus
    cutoff: datetime
    repositories_requested: int = Field(ge=0)
    repositories_scanned: int = Field(default=0, ge=0)
    repositories_failed: int = Field(default=0, ge=0)
    issues_seen: int = Field(default=0, ge=0)
    candidates_matched: int = Field(default=0, ge=0)
    work_items_queued: int = Field(default=0, ge=0)
    started_at: datetime
    completed_at: datetime | None = None
    error: str | None = None


class PollingRunRepository(StrictModel):
    run_id: str
    repository_id: int | None = Field(default=None, ge=1)
    repository: str
    status: RepositoryScanStatus
    window_start: datetime | None = None
    window_end: datetime | None = None
    scan_mode: Literal["rolling", "fresh", "backfill"] | None = None
    issues_seen: int = Field(default=0, ge=0)
    candidates_matched: int = Field(default=0, ge=0)
    work_items_queued: int = Field(default=0, ge=0)
    started_at: datetime
    completed_at: datetime | None = None
    error: str | None = None


class PollingCandidate(StrictModel):
    candidate_id: str
    repository_id: int = Field(ge=1)
    repository: str
    issue_number: int = Field(ge=1)
    issue_url: str
    title: str = Field(min_length=1)
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    author: str | None = None
    comments: int = Field(default=0, ge=0)
    evidence_score: int = Field(default=0, ge=0)
    eligible: bool
    filter_reasons: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    first_seen_at: datetime
    last_seen_at: datetime
    latest_run_id: str

    @field_validator("filter_reasons")
    @classmethod
    def unique_reasons(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))


class ScreeningDecision(StrEnum):
    """Repository-agnostic screening outcomes shown to the operator."""

    SELECT = "SELECT"
    DEFER = "DEFER"
    REJECT = "REJECT"


class MachineCompatibility(StrEnum):
    """Whether the frozen Issue can be handled in the configured worker lane."""

    COMPATIBLE = "COMPATIBLE"
    NEEDS_PROBE = "NEEDS_PROBE"
    INCOMPATIBLE = "INCOMPATIBLE"


class ScreeningTaskKind(StrEnum):
    """The useful work implied by one Issue, independent of repository."""

    LOCAL_CODE_OR_TEST = "LOCAL_CODE_OR_TEST"
    LOCAL_REPRODUCTION = "LOCAL_REPRODUCTION"
    EXTERNAL_OPERATION = "EXTERNAL_OPERATION"
    NON_ACTIONABLE_REPORT = "NON_ACTIONABLE_REPORT"


class ScreeningEnvironmentRequirements(StrictModel):
    """Minimum execution envelope inferred from the frozen Issue report."""

    operating_systems: list[str] = Field(default_factory=list, max_length=12)
    cpu_architectures: list[str] = Field(default_factory=list, max_length=12)
    minimum_cpu_cores: int = Field(default=1, ge=1, le=1024)
    minimum_memory_gib: int = Field(default=1, ge=1, le=4096)
    gpu_count: int = Field(default=0, ge=0, le=64)
    gpu_architectures: list[str] = Field(default_factory=list, max_length=16)
    network_access_required: bool = False
    external_system_write_required: bool = False
    external_dependencies: list[str] = Field(default_factory=list, max_length=20)

    @field_validator(
        "operating_systems",
        "cpu_architectures",
        "gpu_architectures",
        "external_dependencies",
    )
    @classmethod
    def normalized_values(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values))
        if any(not value for value in normalized):
            raise ValueError("screening environment values cannot be empty")
        return normalized

    @model_validator(mode="after")
    def cpu_only_has_no_gpu_architecture(
        self,
    ) -> "ScreeningEnvironmentRequirements":
        if self.gpu_count == 0 and self.gpu_architectures:
            raise ValueError("CPU-only screening cannot require a GPU architecture")
        return self


class IssueScreening(StrictModel):
    """One immutable Subagent decision for one frozen Issue snapshot."""

    candidate_id: str
    candidate_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: ScreeningDecision
    machine_compatibility: MachineCompatibility
    task_kind: ScreeningTaskKind
    reason: str = Field(min_length=1, max_length=4000)
    required_environment: ScreeningEnvironmentRequirements
    evidence: list[str] = Field(default_factory=list, max_length=20)
    uncertainties: list[str] = Field(default_factory=list, max_length=20)
    model_provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    soul_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(default=1, ge=1)
    screening_trigger: Literal["initial", "operator_rescreen"] = "initial"
    requested_by: str | None = Field(default=None, max_length=256)
    request_reason: str | None = Field(default=None, max_length=1000)
    probe_evidence: list[str] = Field(default_factory=list, max_length=20)
    screened_at: datetime

    @field_validator("evidence", "uncertainties", "probe_evidence")
    @classmethod
    def normalized_findings(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values))
        if any(not value for value in normalized):
            raise ValueError("screening findings cannot be empty")
        return normalized

    @model_validator(mode="after")
    def decision_matches_machine_fit(self) -> "IssueScreening":
        if self.decision is ScreeningDecision.SELECT:
            if self.machine_compatibility is not MachineCompatibility.COMPATIBLE:
                raise ValueError("SELECT requires COMPATIBLE machine fit")
            if self.task_kind not in {
                ScreeningTaskKind.LOCAL_CODE_OR_TEST,
                ScreeningTaskKind.LOCAL_REPRODUCTION,
            }:
                raise ValueError("SELECT requires local repository work")
            if self.required_environment.network_access_required:
                raise ValueError("SELECT cannot require worker network access")
            if self.required_environment.external_system_write_required:
                raise ValueError("SELECT cannot require an external write")
        if self.decision is ScreeningDecision.DEFER:
            if self.machine_compatibility is not MachineCompatibility.NEEDS_PROBE:
                raise ValueError("DEFER requires NEEDS_PROBE machine fit")
            if not self.uncertainties:
                raise ValueError("DEFER requires a concrete uncertainty")
        if (
            self.decision is ScreeningDecision.REJECT
            and self.machine_compatibility is MachineCompatibility.NEEDS_PROBE
        ):
            raise ValueError("NEEDS_PROBE Issues must be deferred, not rejected")
        if self.revision == 1:
            if self.screening_trigger != "initial":
                raise ValueError("initial screening revision must use initial trigger")
            if self.requested_by or self.request_reason or self.probe_evidence:
                raise ValueError("initial screening cannot contain re-screen metadata")
        else:
            if self.screening_trigger != "operator_rescreen":
                raise ValueError("later screening revisions require re-screen trigger")
            if not self.requested_by or not self.request_reason:
                raise ValueError("re-screen revision requires requester and reason")
            if not self.probe_evidence:
                raise ValueError("re-screen revision requires probe evidence")
        return self


class WorkPlan(StrictModel):
    summary: str = Field(min_length=1, max_length=4000)
    steps: list[str] = Field(min_length=1, max_length=20)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    risks: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("steps", "acceptance_criteria", "risks")
    @classmethod
    def non_empty_unique(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values))
        if any(not value for value in normalized):
            raise ValueError("work plan entries cannot be empty")
        return normalized


class WorkResourceRequirements(StrictModel):
    cpu_request: str
    cpu_limit: str
    memory_request: str
    memory_limit: str
    gpu_count: int = Field(ge=0, le=8)
    gpu_architecture: str | None = None
    worker_model: str
    execution_environment: Literal["kubernetes"] = "kubernetes"


class WorkItem(StrictModel):
    work_item_id: str
    candidate_id: str
    repository_id: int = Field(ge=1)
    repository: str
    issue_number: int = Field(ge=1)
    issue_url: str
    title: str
    status: WorkStatus
    current_step: str
    plan: WorkPlan | None = None
    environment_status: EnvironmentStatus = EnvironmentStatus.PENDING
    environment_verified_at: datetime | None = None
    named_baseline: str | None = None
    resource_requirements: WorkResourceRequirements | None = None
    task_id: str | None = None
    run_id: str | None = None
    execution_id: str | None = None
    internal_candidate_id: str | None = None
    blocked_reason: str | None = None
    last_error: str | None = None
    execution_attempt: int = Field(default=0, ge=0)
    retry_not_before: datetime | None = None
    queued_at: datetime
    planning_started_at: datetime | None = None
    started_at: datetime | None = None
    review_started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime


class WorkEvent(StrictModel):
    sequence: int = Field(ge=1)
    event_id: str
    work_item_id: str | None = None
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ProjectManagerState(StrictModel):
    manager_id: Literal["main-hermes-project-manager"] = "main-hermes-project-manager"
    session_id: str
    handle: RuntimeHandle
    instructions_digest: str
    snapshot_digest: str | None = None
    last_event_sequence: int = Field(default=0, ge=0)
    last_turn_at: datetime | None = None
    last_error: str | None = None
    version: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime


_SCHEMA = """
CREATE TABLE IF NOT EXISTS polling_tasks (
    task_id             TEXT PRIMARY KEY,
    enabled             INTEGER NOT NULL,
    interval_seconds    INTEGER NOT NULL,
    status              TEXT NOT NULL,
    next_run_at         TEXT,
    last_run_id         TEXT,
    last_started_at     TEXT,
    last_completed_at   TEXT,
    last_error          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS polling_repositories (
    repository_id       INTEGER PRIMARY KEY,
    repository          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    enabled             INTEGER NOT NULL,
    default_branch      TEXT NOT NULL,
    html_url            TEXT NOT NULL,
    last_scanned_at     TEXT,
    last_status         TEXT,
    last_error          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS polling_runs (
    run_id                  TEXT PRIMARY KEY,
    task_id                 TEXT NOT NULL,
    status                  TEXT NOT NULL,
    cutoff                  TEXT NOT NULL,
    repositories_requested  INTEGER NOT NULL,
    repositories_scanned    INTEGER NOT NULL DEFAULT 0,
    repositories_failed     INTEGER NOT NULL DEFAULT 0,
    issues_seen             INTEGER NOT NULL DEFAULT 0,
    candidates_matched      INTEGER NOT NULL DEFAULT 0,
    work_items_queued       INTEGER NOT NULL DEFAULT 0,
    started_at              TEXT NOT NULL,
    completed_at            TEXT,
    error                   TEXT
);

CREATE INDEX IF NOT EXISTS idx_polling_runs_started
ON polling_runs(started_at DESC, run_id);

CREATE TABLE IF NOT EXISTS polling_run_repositories (
    run_id               TEXT NOT NULL,
    repository_id        INTEGER,
    repository           TEXT NOT NULL COLLATE NOCASE,
    status               TEXT NOT NULL,
    window_start         TEXT,
    window_end           TEXT,
    scan_mode            TEXT,
    issues_seen          INTEGER NOT NULL DEFAULT 0,
    candidates_matched   INTEGER NOT NULL DEFAULT 0,
    work_items_queued    INTEGER NOT NULL DEFAULT 0,
    started_at           TEXT NOT NULL,
    completed_at         TEXT,
    error                TEXT,
    PRIMARY KEY (run_id, repository)
);

CREATE INDEX IF NOT EXISTS idx_polling_run_repositories_repository
ON polling_run_repositories(repository_id, repository, started_at DESC);

CREATE TABLE IF NOT EXISTS polling_repository_backfill (
    repository          TEXT PRIMARY KEY COLLATE NOCASE,
    next_window_start   TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS polling_candidates (
    candidate_id          TEXT PRIMARY KEY,
    repository_id         INTEGER NOT NULL,
    repository            TEXT NOT NULL COLLATE NOCASE,
    issue_number          INTEGER NOT NULL,
    issue_url             TEXT NOT NULL,
    title                 TEXT NOT NULL,
    body                  TEXT NOT NULL,
    labels_json           TEXT NOT NULL,
    author                TEXT,
    comments              INTEGER NOT NULL,
    evidence_score        INTEGER NOT NULL,
    eligible              INTEGER NOT NULL,
    filter_reasons_json   TEXT NOT NULL,
    snapshot_digest       TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    first_seen_at         TEXT NOT NULL,
    last_seen_at          TEXT NOT NULL,
    latest_run_id         TEXT NOT NULL,
    UNIQUE(repository_id, issue_number)
);

CREATE INDEX IF NOT EXISTS idx_polling_candidates_query
ON polling_candidates(repository_id, eligible, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS polling_run_candidates (
    run_id             TEXT NOT NULL,
    candidate_id       TEXT NOT NULL,
    repository_id      INTEGER NOT NULL,
    snapshot_digest    TEXT NOT NULL,
    eligible           INTEGER NOT NULL,
    observed_at        TEXT NOT NULL,
    PRIMARY KEY (run_id, candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_polling_run_candidates_repository
ON polling_run_candidates(repository_id, run_id, candidate_id);

CREATE TABLE IF NOT EXISTS issue_screenings (
    candidate_id               TEXT NOT NULL,
    candidate_snapshot_digest  TEXT NOT NULL,
    decision                   TEXT NOT NULL,
    machine_compatibility      TEXT NOT NULL,
    task_kind                  TEXT NOT NULL,
    reason                     TEXT NOT NULL,
    required_environment_json  TEXT NOT NULL,
    evidence_json              TEXT NOT NULL,
    uncertainties_json         TEXT NOT NULL,
    model_provider             TEXT NOT NULL,
    model                      TEXT NOT NULL,
    session_id                 TEXT NOT NULL,
    profile_digest             TEXT NOT NULL,
    soul_digest                TEXT NOT NULL,
    agent_digest               TEXT NOT NULL,
    screened_at                TEXT NOT NULL,
    PRIMARY KEY (candidate_id, candidate_snapshot_digest)
);

CREATE INDEX IF NOT EXISTS idx_issue_screenings_decision
ON issue_screenings(decision, screened_at DESC, candidate_id);

CREATE TABLE IF NOT EXISTS issue_screening_revisions (
    candidate_id               TEXT NOT NULL,
    candidate_snapshot_digest  TEXT NOT NULL,
    revision                   INTEGER NOT NULL,
    decision                   TEXT NOT NULL,
    machine_compatibility      TEXT NOT NULL,
    task_kind                  TEXT NOT NULL,
    reason                     TEXT NOT NULL,
    required_environment_json  TEXT NOT NULL,
    evidence_json              TEXT NOT NULL,
    uncertainties_json         TEXT NOT NULL,
    model_provider             TEXT NOT NULL,
    model                      TEXT NOT NULL,
    session_id                 TEXT NOT NULL,
    profile_digest             TEXT NOT NULL,
    soul_digest                TEXT NOT NULL,
    agent_digest               TEXT NOT NULL,
    screening_trigger          TEXT NOT NULL,
    requested_by               TEXT NOT NULL,
    request_reason             TEXT NOT NULL,
    probe_evidence_json        TEXT NOT NULL,
    screened_at                TEXT NOT NULL,
    PRIMARY KEY (candidate_id, candidate_snapshot_digest, revision)
);

CREATE TABLE IF NOT EXISTS issue_screening_current (
    candidate_id               TEXT NOT NULL,
    candidate_snapshot_digest  TEXT NOT NULL,
    revision                   INTEGER NOT NULL,
    decision                   TEXT NOT NULL,
    machine_compatibility      TEXT NOT NULL,
    task_kind                  TEXT NOT NULL,
    reason                     TEXT NOT NULL,
    required_environment_json  TEXT NOT NULL,
    evidence_json              TEXT NOT NULL,
    uncertainties_json         TEXT NOT NULL,
    model_provider             TEXT NOT NULL,
    model                      TEXT NOT NULL,
    session_id                 TEXT NOT NULL,
    profile_digest             TEXT NOT NULL,
    soul_digest                TEXT NOT NULL,
    agent_digest               TEXT NOT NULL,
    screening_trigger          TEXT NOT NULL,
    requested_by               TEXT,
    request_reason             TEXT,
    probe_evidence_json        TEXT NOT NULL,
    screened_at                TEXT NOT NULL,
    PRIMARY KEY (candidate_id, candidate_snapshot_digest)
);

CREATE INDEX IF NOT EXISTS idx_issue_screening_current_decision
ON issue_screening_current(decision, screened_at DESC, candidate_id);

CREATE TABLE IF NOT EXISTS work_items (
    work_item_id           TEXT PRIMARY KEY,
    candidate_id           TEXT NOT NULL UNIQUE,
    repository_id          INTEGER NOT NULL,
    repository             TEXT NOT NULL COLLATE NOCASE,
    issue_number           INTEGER NOT NULL,
    issue_url              TEXT NOT NULL,
    title                  TEXT NOT NULL,
    status                 TEXT NOT NULL,
    current_step           TEXT NOT NULL,
    plan_json              TEXT,
    environment_status     TEXT NOT NULL DEFAULT 'pending',
    environment_verified_at TEXT,
    named_baseline         TEXT,
    resource_requirements_json TEXT,
    task_id                TEXT,
    run_id                 TEXT,
    execution_id           TEXT,
    internal_candidate_id  TEXT,
    blocked_reason         TEXT,
    last_error             TEXT,
    execution_attempt      INTEGER NOT NULL DEFAULT 0,
    retry_not_before       TEXT,
    queued_at              TEXT NOT NULL,
    planning_started_at    TEXT,
    started_at             TEXT,
    review_started_at      TEXT,
    completed_at           TEXT,
    updated_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_work_items_status
ON work_items(status, updated_at DESC, work_item_id);

CREATE TABLE IF NOT EXISTS work_events (
    sequence       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL UNIQUE,
    work_item_id   TEXT,
    event_type     TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_work_events_item
ON work_events(work_item_id, sequence);

CREATE TABLE IF NOT EXISTS project_manager_state (
    manager_id             TEXT PRIMARY KEY,
    session_id             TEXT NOT NULL,
    handle_json            TEXT NOT NULL,
    instructions_digest    TEXT NOT NULL,
    snapshot_digest        TEXT,
    last_event_sequence    INTEGER NOT NULL,
    last_turn_at           TEXT,
    last_error             TEXT,
    version                INTEGER NOT NULL,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
"""


_WORK_TRANSITIONS: dict[WorkStatus, frozenset[WorkStatus]] = {
    WorkStatus.QUEUED: frozenset({WorkStatus.PLANNING, WorkStatus.BLOCKED}),
    WorkStatus.PLANNING: frozenset({
        WorkStatus.RUNNING,
        WorkStatus.BLOCKED,
        WorkStatus.FAILED,
    }),
    WorkStatus.RUNNING: frozenset({
        WorkStatus.REVIEW,
        WorkStatus.BLOCKED,
        WorkStatus.FAILED,
    }),
    WorkStatus.REVIEW: frozenset({
        WorkStatus.DONE,
        WorkStatus.BLOCKED,
        WorkStatus.FAILED,
    }),
    WorkStatus.DONE: frozenset(),
    WorkStatus.BLOCKED: frozenset(),
    WorkStatus.FAILED: frozenset(),
}


class SqlitePollingStore:
    """Thread-safe SQLite store shared by the poller, manager, and API."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        assert_private_directory(self.path.parent)
        self._lock = RLock()
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            self._migrate_work_retry_columns(connection)
            self._migrate_candidate_snapshot_column(connection)
            self._migrate_repository_scan_window_columns(connection)
            self._backfill_polling_run_candidates(connection)
            self._migrate_screening_current_projection(connection)

    @staticmethod
    def _migrate_work_retry_columns(connection: sqlite3.Connection) -> None:
        """Add retry projection fields to pre-existing polling databases."""

        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(work_items)")
        }
        if "execution_attempt" not in columns:
            connection.execute(
                "ALTER TABLE work_items ADD COLUMN "
                "execution_attempt INTEGER NOT NULL DEFAULT 0"
            )
        if "retry_not_before" not in columns:
            connection.execute(
                "ALTER TABLE work_items ADD COLUMN retry_not_before TEXT"
            )
        connection.execute(
            """
            UPDATE work_items SET execution_attempt = 1
            WHERE execution_attempt = 0
              AND (task_id IS NOT NULL OR run_id IS NOT NULL
                   OR execution_id IS NOT NULL)
            """
        )

    @staticmethod
    def _migrate_candidate_snapshot_column(
        connection: sqlite3.Connection,
    ) -> None:
        """Backfill immutable Issue snapshot identities for older stores."""

        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(polling_candidates)")
        }
        if "snapshot_digest" not in columns:
            connection.execute(
                "ALTER TABLE polling_candidates ADD COLUMN "
                "snapshot_digest TEXT NOT NULL DEFAULT ''"
            )
        rows = connection.execute("SELECT * FROM polling_candidates").fetchall()
        for row in rows:
            candidate = _candidate_from_row(row)
            digest = candidate_snapshot_digest(candidate)
            if str(row["snapshot_digest"] or "") != digest:
                connection.execute(
                    "UPDATE polling_candidates SET snapshot_digest = ? "
                    "WHERE candidate_id = ?",
                    (digest, candidate.candidate_id),
                )

    @staticmethod
    def _migrate_repository_scan_window_columns(
        connection: sqlite3.Connection,
    ) -> None:
        """Add per-repository scan windows without rewriting old history."""

        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(polling_run_repositories)"
            )
        }
        for name in ("window_start", "window_end", "scan_mode"):
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE polling_run_repositories ADD COLUMN {name} TEXT"
                )

    @staticmethod
    def _backfill_polling_run_candidates(
        connection: sqlite3.Connection,
    ) -> None:
        """Seed batch membership for the most recent snapshot of older rows.

        Before this projection existed, ``polling_candidates.latest_run_id``
        retained only the newest observation.  That is still enough to seed
        the current per-repository coverage window without inventing history
        for older runs.
        """

        connection.execute(
            """
            INSERT OR IGNORE INTO polling_run_candidates (
                run_id, candidate_id, repository_id, snapshot_digest,
                eligible, observed_at
            )
            SELECT latest_run_id, candidate_id, repository_id,
                   snapshot_digest, eligible, last_seen_at
            FROM polling_candidates
            WHERE snapshot_digest != ''
              AND EXISTS (
                  SELECT 1 FROM polling_runs AS run
                  WHERE run.run_id = polling_candidates.latest_run_id
              )
            """
        )

    @staticmethod
    def _migrate_screening_current_projection(
        connection: sqlite3.Connection,
    ) -> None:
        """Backfill the mutable current projection from immutable revision 1."""

        connection.execute(
            """
            INSERT OR IGNORE INTO issue_screening_current (
                candidate_id, candidate_snapshot_digest, revision, decision,
                machine_compatibility, task_kind, reason,
                required_environment_json, evidence_json,
                uncertainties_json, model_provider, model, session_id,
                profile_digest, soul_digest, agent_digest,
                screening_trigger, requested_by, request_reason,
                probe_evidence_json, screened_at
            )
            SELECT candidate_id, candidate_snapshot_digest, 1, decision,
                   machine_compatibility, task_kind, reason,
                   required_environment_json, evidence_json,
                   uncertainties_json, model_provider, model, session_id,
                   profile_digest, soul_digest, agent_digest,
                   'initial', NULL, NULL, '[]', screened_at
            FROM issue_screenings
            """
        )

    def close(self) -> None:
        """Compatibility hook for composed service shutdown."""

    def configure_task(
        self,
        *,
        enabled: bool,
        interval_seconds: int,
        now: datetime | None = None,
    ) -> PollingTask:
        timestamp = _utc(now or utc_now())
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM polling_tasks WHERE task_id = ?",
                ("github-issue-polling",),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO polling_tasks (
                        task_id, enabled, interval_seconds, status,
                        next_run_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "github-issue-polling",
                        int(enabled),
                        interval_seconds,
                        PollingTaskStatus.IDLE.value,
                        _iso(timestamp) if enabled else None,
                        _iso(timestamp),
                        _iso(timestamp),
                    ),
                )
            else:
                next_run_at = row["next_run_at"]
                if enabled and not next_run_at:
                    next_run_at = _iso(timestamp)
                if not enabled:
                    next_run_at = None
                connection.execute(
                    """
                    UPDATE polling_tasks
                    SET enabled = ?, interval_seconds = ?, next_run_at = ?,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        int(enabled),
                        interval_seconds,
                        next_run_at,
                        _iso(timestamp),
                        "github-issue-polling",
                    ),
                )
        return self.get_task()

    def recover_interrupted_run(
        self,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        timestamp = _utc(now or utc_now())
        recovered: list[str] = []
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT run_id FROM polling_runs WHERE status = ?",
                (PollingRunStatus.RUNNING.value,),
            ).fetchall()
            for row in rows:
                run_id = str(row["run_id"])
                connection.execute(
                    """
                    UPDATE polling_runs
                    SET status = ?, completed_at = ?, error = ?
                    WHERE run_id = ?
                    """,
                    (
                        PollingRunStatus.FAILED.value,
                        _iso(timestamp),
                        "Polling process stopped before the run completed.",
                        run_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE polling_run_repositories
                    SET status = ?, completed_at = ?, error = COALESCE(error, ?)
                    WHERE run_id = ? AND status = ?
                    """,
                    (
                        RepositoryScanStatus.FAILED.value,
                        _iso(timestamp),
                        "Polling process stopped before repository completion.",
                        run_id,
                        RepositoryScanStatus.RUNNING.value,
                    ),
                )
                recovered.append(run_id)
            if recovered:
                connection.execute(
                    """
                    UPDATE polling_tasks
                    SET status = ?, next_run_at = ?, last_error = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        PollingTaskStatus.FAILED.value,
                        _iso(timestamp),
                        "Recovered an interrupted polling run.",
                        _iso(timestamp),
                        "github-issue-polling",
                    ),
                )
        return recovered

    def get_task(self) -> PollingTask:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM polling_tasks WHERE task_id = ?",
                ("github-issue-polling",),
            ).fetchone()
        if row is None:
            raise KeyError("polling task is not configured")
        return _task_from_row(row)

    def is_due(self, *, now: datetime | None = None) -> bool:
        task = self.get_task()
        timestamp = _utc(now or utc_now())
        return bool(
            task.enabled
            and task.status is not PollingTaskStatus.RUNNING
            and (task.next_run_at is None or task.next_run_at <= timestamp)
        )

    def begin_run(
        self,
        run: PollingRun,
    ) -> PollingRun:
        if run.status is not PollingRunStatus.RUNNING:
            raise ValueError("new polling runs must start running")
        with self._transaction() as connection:
            task = connection.execute(
                "SELECT * FROM polling_tasks WHERE task_id = ?",
                (run.task_id,),
            ).fetchone()
            if task is None:
                raise KeyError("polling task is not configured")
            if not bool(task["enabled"]):
                raise RuntimeError("polling task is disabled")
            if task["status"] == PollingTaskStatus.RUNNING.value:
                raise RuntimeError("another polling run is already active")
            connection.execute(
                """
                INSERT INTO polling_runs (
                    run_id, task_id, status, cutoff, repositories_requested,
                    repositories_scanned, repositories_failed, issues_seen,
                    candidates_matched, work_items_queued, started_at,
                    completed_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _run_values(run),
            )
            connection.execute(
                """
                UPDATE polling_tasks
                SET status = ?, last_run_id = ?, last_started_at = ?,
                    next_run_at = NULL, last_error = NULL, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    PollingTaskStatus.RUNNING.value,
                    run.run_id,
                    _iso(run.started_at),
                    _iso(run.started_at),
                    run.task_id,
                ),
            )
        return run

    def begin_repository_scan(
        self,
        run_id: str,
        repository: str,
        *,
        started_at: datetime | None = None,
    ) -> PollingRunRepository:
        item = PollingRunRepository(
            run_id=run_id,
            repository=repository,
            status=RepositoryScanStatus.RUNNING,
            started_at=_utc(started_at or utc_now()),
        )
        with self._transaction() as connection:
            _require_running_run(connection, run_id)
            connection.execute(
                """
                INSERT INTO polling_run_repositories (
                    run_id, repository_id, repository, status, window_start,
                    window_end, scan_mode, issues_seen, candidates_matched,
                    work_items_queued, started_at, completed_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _run_repository_values(item),
            )
        return item

    def upsert_repository(
        self,
        repository: PollingRepository,
    ) -> PollingRepository:
        with self._transaction() as connection:
            collision = connection.execute(
                """
                SELECT repository_id, repository FROM polling_repositories
                WHERE repository_id = ? OR repository = ? COLLATE NOCASE
                """,
                (repository.repository_id, repository.repository),
            ).fetchall()
            if any(
                int(row["repository_id"]) != repository.repository_id
                or str(row["repository"]).casefold() != repository.repository.casefold()
                for row in collision
            ):
                raise ValueError("GitHub repository id/name identity changed")
            connection.execute(
                """
                INSERT INTO polling_repositories (
                    repository_id, repository, enabled, default_branch,
                    html_url, last_scanned_at, last_status, last_error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository_id) DO UPDATE SET
                    repository = excluded.repository,
                    enabled = excluded.enabled,
                    default_branch = excluded.default_branch,
                    html_url = excluded.html_url,
                    updated_at = excluded.updated_at
                """,
                _repository_values(repository),
            )
        return repository

    def finish_repository_scan(
        self,
        item: PollingRunRepository,
        *,
        next_backfill_cursor: datetime | None = None,
    ) -> PollingRunRepository:
        if item.status is RepositoryScanStatus.RUNNING:
            raise ValueError("repository scan must have a terminal status")
        if item.completed_at is None:
            raise ValueError("finished repository scan requires completed_at")
        safe_error = redact_text(item.error) if item.error else None
        item = item.model_copy(update={"error": safe_error})
        cursor_value = (
            _utc(next_backfill_cursor)
            if next_backfill_cursor is not None
            else None
        )
        if cursor_value is not None and any(
            (
                cursor_value.hour,
                cursor_value.minute,
                cursor_value.second,
                cursor_value.microsecond,
            )
        ):
            raise ValueError("repository backfill cursor must be a UTC day boundary")
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE polling_run_repositories
                SET repository_id = ?, status = ?, window_start = ?,
                    window_end = ?, scan_mode = ?, issues_seen = ?,
                    candidates_matched = ?, work_items_queued = ?,
                    completed_at = ?, error = ?
                WHERE run_id = ? AND repository = ? COLLATE NOCASE
                  AND status = ?
                """,
                (
                    item.repository_id,
                    item.status.value,
                    _iso(item.window_start) if item.window_start else None,
                    _iso(item.window_end) if item.window_end else None,
                    item.scan_mode,
                    item.issues_seen,
                    item.candidates_matched,
                    item.work_items_queued,
                    _iso(item.completed_at),
                    item.error,
                    item.run_id,
                    item.repository,
                    RepositoryScanStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError("repository scan is missing or already finished")
            if item.repository_id is not None:
                connection.execute(
                    """
                    UPDATE polling_repositories
                    SET last_scanned_at = ?, last_status = ?, last_error = ?,
                        updated_at = ?
                    WHERE repository_id = ?
                    """,
                    (
                        _iso(item.completed_at),
                        item.status.value,
                        item.error,
                        _iso(item.completed_at),
                        item.repository_id,
                    ),
                )
            if cursor_value is not None:
                connection.execute(
                    """
                    INSERT INTO polling_repository_backfill (
                        repository, next_window_start, updated_at
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(repository) DO UPDATE SET
                        next_window_start = excluded.next_window_start,
                        updated_at = excluded.updated_at
                    """,
                    (
                        item.repository,
                        _iso(cursor_value),
                        _iso(item.completed_at),
                    ),
                )
        return item

    def candidate_snapshot_changed(self, candidate: PollingCandidate) -> bool:
        """Return whether an Issue is new or its screening input changed."""

        expected = candidate_snapshot_digest(candidate)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT snapshot_digest FROM polling_candidates "
                "WHERE candidate_id = ?",
                (candidate.candidate_id,),
            ).fetchone()
        return row is None or str(row["snapshot_digest"] or "") != expected

    def save_candidate(
        self,
        candidate: PollingCandidate,
        *,
        enqueue_eligible: bool = True,
        max_pending_work_items: int | None = None,
    ) -> tuple[PollingCandidate, bool]:
        """Upsert an Issue snapshot and enqueue it within the queue budget."""

        if max_pending_work_items is not None and max_pending_work_items < 1:
            raise ValueError("pending Work capacity must be positive")

        with self._transaction() as connection:
            _require_running_run(connection, candidate.latest_run_id)
            repository = connection.execute(
                """
                SELECT repository FROM polling_repositories
                WHERE repository_id = ?
                """,
                (candidate.repository_id,),
            ).fetchone()
            if repository is None:
                raise KeyError("candidate repository metadata is not stored")
            if (
                str(repository["repository"]).casefold()
                != candidate.repository.casefold()
            ):
                raise ValueError("candidate repository id/name disagree")
            existing = connection.execute(
                """
                SELECT first_seen_at FROM polling_candidates
                WHERE candidate_id = ?
                """,
                (candidate.candidate_id,),
            ).fetchone()
            if existing is not None:
                candidate = candidate.model_copy(
                    update={"first_seen_at": _parse(existing["first_seen_at"])}
                )
            connection.execute(
                """
                INSERT INTO polling_candidates (
                    candidate_id, repository_id, repository, issue_number,
                    issue_url, title, body, labels_json, author, comments,
                    evidence_score, eligible, filter_reasons_json,
                    snapshot_digest, created_at, updated_at, first_seen_at,
                    last_seen_at, latest_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    issue_url = excluded.issue_url,
                    title = excluded.title,
                    body = excluded.body,
                    labels_json = excluded.labels_json,
                    author = excluded.author,
                    comments = excluded.comments,
                    evidence_score = excluded.evidence_score,
                    eligible = excluded.eligible,
                    filter_reasons_json = excluded.filter_reasons_json,
                    snapshot_digest = excluded.snapshot_digest,
                    updated_at = excluded.updated_at,
                    last_seen_at = excluded.last_seen_at,
                    latest_run_id = excluded.latest_run_id
                """,
                _candidate_values(candidate),
            )
            connection.execute(
                """
                INSERT INTO polling_run_candidates (
                    run_id, candidate_id, repository_id, snapshot_digest,
                    eligible, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, candidate_id) DO UPDATE SET
                    repository_id = excluded.repository_id,
                    snapshot_digest = excluded.snapshot_digest,
                    eligible = excluded.eligible,
                    observed_at = excluded.observed_at
                """,
                (
                    candidate.latest_run_id,
                    candidate.candidate_id,
                    candidate.repository_id,
                    candidate_snapshot_digest(candidate),
                    int(candidate.eligible),
                    _iso(candidate.last_seen_at),
                ),
            )
            queued = bool(
                enqueue_eligible
                and candidate.eligible
                and self._enqueue_candidate_tx(
                    connection,
                    candidate,
                    queued_at=candidate.last_seen_at,
                    max_pending_work_items=max_pending_work_items,
                )
            )
        return candidate, queued

    def hold_unselected_work_items(
        self,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Hold legacy auto-enqueued Work until an operator re-selects it."""

        timestamp = _utc(now or utc_now())
        held: list[str] = []
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT work.* FROM work_items AS work
                WHERE work.status IN (?, ?)
                  AND NOT EXISTS (
                    SELECT 1 FROM work_events AS event
                    WHERE event.work_item_id = work.work_item_id
                      AND event.event_type = 'work.operator_selected'
                  )
                ORDER BY work.queued_at, work.work_item_id
                """,
                (WorkStatus.QUEUED.value, WorkStatus.PLANNING.value),
            ).fetchall()
            for row in rows:
                work_id = str(row["work_item_id"])
                prior = WorkStatus(str(row["status"]))
                connection.execute(
                    """
                    UPDATE work_items SET status = ?, current_step = ?,
                        task_id = NULL, run_id = NULL, execution_id = NULL,
                        internal_candidate_id = NULL, blocked_reason = ?,
                        retry_not_before = NULL, started_at = NULL,
                        review_started_at = NULL, completed_at = NULL,
                        updated_at = ?
                    WHERE work_item_id = ?
                    """,
                    (
                        WorkStatus.BLOCKED.value,
                        "Waiting for an operator to select this Issue.",
                        OPERATOR_SELECTION_HOLD_REASON,
                        _iso(timestamp),
                        work_id,
                    ),
                )
                self._record_event_tx(
                    connection,
                    work_id,
                    "work.operator_selection_required",
                    {
                        "from": prior.value,
                        "to": WorkStatus.BLOCKED.value,
                        "reason": OPERATOR_SELECTION_HOLD_REASON,
                    },
                    created_at=timestamp,
                )
                held.append(work_id)
        return held

    def select_candidate_for_work(
        self,
        candidate_id_value: str,
        *,
        selected_by: str,
        max_pending_work_items: int,
        require_screening_select: bool = False,
        now: datetime | None = None,
    ) -> WorkItem:
        """Persist operator admission before Main Hermes sees the Work."""

        if max_pending_work_items < 1:
            raise ValueError("pending Work capacity must be positive")
        actor = redact_text(selected_by).strip()
        if not actor:
            raise ValueError("operator selection requires an actor")
        timestamp = _utc(now or utc_now())
        with self._transaction() as connection:
            candidate_row = connection.execute(
                "SELECT * FROM polling_candidates WHERE candidate_id = ?",
                (candidate_id_value,),
            ).fetchone()
            if candidate_row is None:
                raise KeyError(f"unknown polling candidate: {candidate_id_value}")
            candidate = _candidate_from_row(candidate_row)
            screening = _current_screening_tx(
                connection,
                candidate_id_value,
                str(candidate_row["snapshot_digest"]),
            )
            if require_screening_select and not _screening_allows_admission(screening):
                raise ValueError(
                    "only current dependency-closed Subagent SELECT decisions "
                    "can be admitted"
                )
            if not require_screening_select and not candidate.eligible:
                raise ValueError("only mechanically eligible Issues can be selected")
            work_row = connection.execute(
                "SELECT * FROM work_items WHERE candidate_id = ?",
                (candidate_id_value,),
            ).fetchone()
            if work_row is not None and _operator_selected_tx(
                connection,
                str(work_row["work_item_id"]),
            ):
                return _work_from_row(work_row)
            if _pending_work_count_tx(connection) >= max_pending_work_items:
                raise ValueError("selected Work capacity is full")
            if work_row is None:
                work_id = self._enqueue_candidate_tx(
                    connection,
                    candidate,
                    queued_at=timestamp,
                    max_pending_work_items=max_pending_work_items,
                )
                assert work_id is not None
            else:
                current = _work_from_row(work_row)
                if (
                    current.status is not WorkStatus.BLOCKED
                    or current.blocked_reason != OPERATOR_SELECTION_HOLD_REASON
                ):
                    raise ValueError(
                        "the Issue already has Work that is not awaiting selection"
                    )
                work_id = current.work_item_id
                target = (
                    WorkStatus.PLANNING
                    if current.plan is not None
                    else WorkStatus.QUEUED
                )
                step = (
                    "Operator selected this Issue; its committed plan is "
                    "ready for dispatch."
                    if target is WorkStatus.PLANNING
                    else "Operator selected this Issue for Main Hermes planning."
                )
                connection.execute(
                    """
                    UPDATE work_items SET status = ?, current_step = ?,
                        blocked_reason = NULL, last_error = NULL,
                        retry_not_before = NULL, updated_at = ?
                    WHERE work_item_id = ?
                    """,
                    (target.value, step, _iso(timestamp), work_id),
                )
            self._record_event_tx(
                connection,
                work_id,
                "work.operator_selected",
                {
                    "candidate_id": candidate_id_value,
                    "selected_by": actor,
                    "screening_snapshot_digest": (
                        screening.candidate_snapshot_digest
                        if screening is not None
                        else None
                    ),
                    "screening_profile_digest": (
                        screening.profile_digest if screening is not None else None
                    ),
                },
                created_at=timestamp,
            )
            return _require_work_tx(connection, work_id)

    def operator_selection_state(
        self,
        candidate_id_value: str,
        *,
        require_screening_select: bool = False,
    ) -> dict[str, bool]:
        """Return safe UI state without exposing operator identity."""

        with self._connect() as connection:
            candidate = connection.execute(
                "SELECT eligible, snapshot_digest FROM polling_candidates "
                "WHERE candidate_id = ?",
                (candidate_id_value,),
            ).fetchone()
            if candidate is None:
                raise KeyError(f"unknown polling candidate: {candidate_id_value}")
            work = connection.execute(
                "SELECT * FROM work_items WHERE candidate_id = ?",
                (candidate_id_value,),
            ).fetchone()
            selected = bool(
                work is not None
                and _operator_selected_tx(
                    connection,
                    str(work["work_item_id"]),
                )
            )
            screening = _current_screening_tx(
                connection,
                candidate_id_value,
                str(candidate["snapshot_digest"]),
            )
            admitted_by_policy = (
                _screening_allows_admission(screening)
                if require_screening_select
                else bool(candidate["eligible"])
            )
            selectable = admitted_by_policy and (
                work is None
                or (
                    WorkStatus(str(work["status"])) is WorkStatus.BLOCKED
                    and str(work["blocked_reason"] or "")
                    == OPERATOR_SELECTION_HOLD_REASON
                    and not selected
                )
            )
            return {"selected": selected, "selectable": selectable}

    def exclude_candidate(
        self,
        candidate_id_value: str,
        *,
        reasons: list[str],
        now: datetime | None = None,
    ) -> PollingCandidate:
        """Fail closed on a newly enforced relevance policy.

        Existing queued or planned Work is blocked so stale candidates cannot
        keep polling backpressure full after the policy becomes stricter.
        Running and terminal Work is retained as historical evidence.
        """

        if not reasons or any(not reason.strip() for reason in reasons):
            raise ValueError("candidate exclusion requires concrete reasons")
        timestamp = _utc(now or utc_now())
        safe_reasons = [redact_text(reason) for reason in reasons]
        work_id: str | None = None
        work_status: WorkStatus | None = None
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM polling_candidates WHERE candidate_id = ?",
                (candidate_id_value,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown polling candidate: {candidate_id_value}")
            existing = _candidate_from_row(row)
            merged = list(dict.fromkeys([*existing.filter_reasons, *safe_reasons]))
            updated = existing.model_copy(
                update={
                    "eligible": False,
                    "filter_reasons": merged,
                    "last_seen_at": timestamp,
                }
            )
            connection.execute(
                """
                UPDATE polling_candidates
                SET eligible = 0, filter_reasons_json = ?,
                    snapshot_digest = ?, last_seen_at = ?
                WHERE candidate_id = ?
                """,
                (
                    _json(merged),
                    candidate_snapshot_digest(updated),
                    _iso(timestamp),
                    candidate_id_value,
                ),
            )
            work = connection.execute(
                """
                SELECT work_item_id, status FROM work_items
                WHERE candidate_id = ?
                """,
                (candidate_id_value,),
            ).fetchone()
            if work is not None:
                work_id = str(work["work_item_id"])
                work_status = WorkStatus(str(work["status"]))
            stored = _candidate_from_row(
                connection.execute(
                    "SELECT * FROM polling_candidates WHERE candidate_id = ?",
                    (candidate_id_value,),
                ).fetchone()
            )
        if work_id is not None and work_status in {
            WorkStatus.QUEUED,
            WorkStatus.PLANNING,
        }:
            self.transition_work_item(
                work_id,
                WorkStatus.BLOCKED,
                current_step="Excluded by the AMD-or-portable Issue policy.",
                reason="; ".join(safe_reasons),
                now=timestamp,
            )
        return stored

    def save_screenings(
        self,
        screenings: Sequence[IssueScreening],
    ) -> list[IssueScreening]:
        """Atomically bind a complete Subagent batch to current snapshots."""

        if not screenings:
            raise ValueError("screening batch cannot be empty")
        if any(item.revision != 1 for item in screenings):
            raise ValueError("initial screening batch must contain revision one")
        candidate_ids = [item.candidate_id for item in screenings]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("screening batch contains duplicate candidates")
        with self._transaction() as connection:
            for item in screenings:
                candidate = connection.execute(
                    "SELECT snapshot_digest FROM polling_candidates "
                    "WHERE candidate_id = ?",
                    (item.candidate_id,),
                ).fetchone()
                if candidate is None:
                    raise KeyError(f"unknown polling candidate: {item.candidate_id}")
                if str(candidate["snapshot_digest"]) != item.candidate_snapshot_digest:
                    raise ValueError(
                        "screening decision targets a stale Issue snapshot"
                    )
                existing = connection.execute(
                    "SELECT * FROM issue_screenings WHERE candidate_id = ? "
                    "AND candidate_snapshot_digest = ?",
                    (item.candidate_id, item.candidate_snapshot_digest),
                ).fetchone()
                if existing is not None:
                    if _screening_from_row(existing) != item:
                        raise ValueError(
                            "screening decisions are immutable for one snapshot"
                        )
                else:
                    connection.execute(
                        """
                        INSERT INTO issue_screenings (
                            candidate_id, candidate_snapshot_digest, decision,
                            machine_compatibility, task_kind, reason,
                            required_environment_json, evidence_json,
                            uncertainties_json, model_provider, model, session_id,
                            profile_digest, soul_digest, agent_digest, screened_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        _screening_values(item),
                    )
                _insert_initial_screening_projection(connection, item)
        return list(screenings)

    def save_screening_revision(
        self,
        screening: IssueScreening,
    ) -> IssueScreening:
        """Append one audited re-screen and advance only the current projection."""

        if screening.revision < 2:
            raise ValueError("re-screen revision must be greater than one")
        with self._transaction() as connection:
            candidate = connection.execute(
                "SELECT snapshot_digest FROM polling_candidates WHERE candidate_id = ?",
                (screening.candidate_id,),
            ).fetchone()
            if candidate is None:
                raise KeyError(f"unknown polling candidate: {screening.candidate_id}")
            snapshot_digest = str(candidate["snapshot_digest"])
            if snapshot_digest != screening.candidate_snapshot_digest:
                raise ValueError("screening revision targets a stale Issue snapshot")
            work = connection.execute(
                "SELECT 1 FROM work_items WHERE candidate_id = ?",
                (screening.candidate_id,),
            ).fetchone()
            if work is not None:
                raise ValueError("cannot re-screen an Issue after Work was created")
            current = _current_screening_tx(
                connection,
                screening.candidate_id,
                snapshot_digest,
            )
            if current is None:
                raise ValueError("re-screen requires an initial screening")
            if screening.revision != current.revision + 1:
                raise ValueError("screening revision is not the next revision")
            connection.execute(
                """
                INSERT INTO issue_screening_revisions (
                    candidate_id, candidate_snapshot_digest, revision,
                    decision, machine_compatibility, task_kind, reason,
                    required_environment_json, evidence_json,
                    uncertainties_json, model_provider, model, session_id,
                    profile_digest, soul_digest, agent_digest,
                    screening_trigger, requested_by, request_reason,
                    probe_evidence_json, screened_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _screening_current_values(screening),
            )
            _replace_current_screening_projection(connection, screening)
        return screening

    def get_current_screening(
        self,
        candidate_id_value: str,
    ) -> IssueScreening | None:
        """Return the decision for the current frozen Issue snapshot."""

        with self._connect() as connection:
            candidate = connection.execute(
                "SELECT snapshot_digest FROM polling_candidates WHERE candidate_id = ?",
                (candidate_id_value,),
            ).fetchone()
            if candidate is None:
                raise KeyError(f"unknown polling candidate: {candidate_id_value}")
            return _current_screening_tx(
                connection,
                candidate_id_value,
                str(candidate["snapshot_digest"]),
            )

    def list_current_screening_history(
        self,
        candidate_id_value: str,
    ) -> list[IssueScreening]:
        """Return immutable screening revisions for the current Issue snapshot."""

        with self._connect() as connection:
            candidate = connection.execute(
                "SELECT snapshot_digest FROM polling_candidates WHERE candidate_id = ?",
                (candidate_id_value,),
            ).fetchone()
            if candidate is None:
                raise KeyError(f"unknown polling candidate: {candidate_id_value}")
            snapshot_digest = str(candidate["snapshot_digest"])
            initial = connection.execute(
                "SELECT * FROM issue_screenings WHERE candidate_id = ? "
                "AND candidate_snapshot_digest = ?",
                (candidate_id_value, snapshot_digest),
            ).fetchone()
            revisions = connection.execute(
                "SELECT * FROM issue_screening_revisions "
                "WHERE candidate_id = ? AND candidate_snapshot_digest = ? "
                "ORDER BY revision",
                (candidate_id_value, snapshot_digest),
            ).fetchall()
        history = [_screening_from_row(initial)] if initial is not None else []
        history.extend(_screening_from_row(row) for row in revisions)
        return history

    def list_candidates_pending_screening(
        self,
        *,
        limit: int,
    ) -> list[PollingCandidate]:
        """Return unscreened current snapshots in deterministic repo batches."""

        _validate_page(limit, 0, maximum=500)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT candidates.*
                FROM polling_candidates AS candidates
                LEFT JOIN issue_screening_current AS screening
                  ON screening.candidate_id = candidates.candidate_id
                 AND screening.candidate_snapshot_digest =
                     candidates.snapshot_digest
                WHERE screening.candidate_id IS NULL
                ORDER BY candidates.repository COLLATE NOCASE,
                         candidates.issue_number, candidates.candidate_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_candidate_from_row(row) for row in rows]

    def screening_counts(
        self,
        *,
        repository_id: int | None = None,
    ) -> dict[str, int]:
        """Count current SELECT/DEFER/REJECT and pending snapshots."""

        clauses = ["1 = 1"]
        values: list[Any] = []
        if repository_id is not None:
            clauses.append("candidates.repository_id = ?")
            values.append(repository_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT screening.decision, COUNT(*) AS total
                FROM polling_candidates AS candidates
                LEFT JOIN issue_screening_current AS screening
                  ON screening.candidate_id = candidates.candidate_id
                 AND screening.candidate_snapshot_digest =
                     candidates.snapshot_digest
                WHERE """
                + " AND ".join(clauses)
                + " GROUP BY screening.decision",
                values,
            ).fetchall()
        counts = {decision.value: 0 for decision in ScreeningDecision}
        counts["PENDING"] = 0
        for row in rows:
            key = str(row["decision"] or "PENDING")
            counts[key] = int(row["total"])
        return counts

    def enqueue_waiting_candidates(
        self,
        *,
        max_pending_work_items: int,
        require_screening_select: bool = False,
        now: datetime | None = None,
    ) -> list[str]:
        """Fill free queue slots from eligible candidates not yet in Work."""

        if max_pending_work_items < 1:
            raise ValueError("pending Work capacity must be positive")
        timestamp = _utc(now or utc_now())
        queued: list[str] = []
        with self._transaction() as connection:
            available = max_pending_work_items - _pending_work_count_tx(connection)
            if available <= 0:
                return []
            if require_screening_select:
                rows = connection.execute(
                    """
                    SELECT candidates.*
                    FROM polling_candidates AS candidates
                    JOIN issue_screening_current AS screening
                      ON screening.candidate_id = candidates.candidate_id
                     AND screening.candidate_snapshot_digest =
                         candidates.snapshot_digest
                    LEFT JOIN work_items AS work
                      ON work.candidate_id = candidates.candidate_id
                    WHERE candidates.eligible = 1
                      AND work.candidate_id IS NULL
                      AND screening.decision = ?
                    ORDER BY candidates.first_seen_at,
                             candidates.candidate_id
                    """,
                    (ScreeningDecision.SELECT.value,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT candidates.*
                    FROM polling_candidates AS candidates
                    LEFT JOIN work_items AS work
                      ON work.candidate_id = candidates.candidate_id
                    WHERE candidates.eligible = 1
                      AND work.candidate_id IS NULL
                    ORDER BY candidates.first_seen_at,
                             candidates.candidate_id
                    LIMIT ?
                    """,
                    (available,),
                ).fetchall()
            for row in rows:
                candidate = _candidate_from_row(row)
                if require_screening_select and not _screening_allows_admission(
                    _current_screening_tx(
                        connection,
                        candidate.candidate_id,
                        str(row["snapshot_digest"]),
                    )
                ):
                    continue
                work_id = self._enqueue_candidate_tx(
                    connection,
                    candidate,
                    queued_at=timestamp,
                    max_pending_work_items=max_pending_work_items,
                )
                if work_id is not None:
                    queued.append(work_id)
                if len(queued) >= available:
                    break
        return queued

    def _enqueue_candidate_tx(
        self,
        connection: sqlite3.Connection,
        candidate: PollingCandidate,
        *,
        queued_at: datetime,
        max_pending_work_items: int | None,
    ) -> str | None:
        found = connection.execute(
            "SELECT 1 FROM work_items WHERE candidate_id = ?",
            (candidate.candidate_id,),
        ).fetchone()
        if found is not None:
            return None
        if (
            max_pending_work_items is not None
            and _pending_work_count_tx(connection) >= max_pending_work_items
        ):
            return None
        work_id = work_item_id(candidate.candidate_id)
        connection.execute(
            """
            INSERT INTO work_items (
                work_item_id, candidate_id, repository_id,
                repository, issue_number, issue_url, title,
                status, current_step, queued_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                work_id,
                candidate.candidate_id,
                candidate.repository_id,
                candidate.repository,
                candidate.issue_number,
                candidate.issue_url,
                candidate.title,
                WorkStatus.QUEUED.value,
                "Awaiting a plan from Main Hermes.",
                _iso(queued_at),
                _iso(queued_at),
            ),
        )
        self._record_event_tx(
            connection,
            work_id,
            "work.queued",
            {
                "candidate_id": candidate.candidate_id,
                "polling_run_id": candidate.latest_run_id,
                "repository_id": candidate.repository_id,
                "repository": candidate.repository,
                "issue_number": candidate.issue_number,
            },
            created_at=queued_at,
        )
        return work_id

    def finish_run(
        self,
        run: PollingRun,
        *,
        next_run_at: datetime | None = None,
    ) -> PollingRun:
        if run.status is PollingRunStatus.RUNNING:
            raise ValueError("polling run must have a terminal status")
        if run.completed_at is None:
            raise ValueError("finished polling run requires completed_at")
        safe_error = redact_text(run.error) if run.error else None
        run = run.model_copy(update={"error": safe_error})
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE polling_runs
                SET status = ?, repositories_scanned = ?,
                    repositories_failed = ?, issues_seen = ?,
                    candidates_matched = ?, work_items_queued = ?,
                    completed_at = ?, error = ?
                WHERE run_id = ? AND status = ?
                """,
                (
                    run.status.value,
                    run.repositories_scanned,
                    run.repositories_failed,
                    run.issues_seen,
                    run.candidates_matched,
                    run.work_items_queued,
                    _iso(run.completed_at),
                    run.error,
                    run.run_id,
                    PollingRunStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError("polling run is missing or already finished")
            task_status = {
                PollingRunStatus.COMPLETED: PollingTaskStatus.IDLE,
                PollingRunStatus.PARTIAL: PollingTaskStatus.PARTIAL,
                PollingRunStatus.FAILED: PollingTaskStatus.FAILED,
            }[run.status]
            task = connection.execute(
                "SELECT interval_seconds, enabled FROM polling_tasks WHERE task_id = ?",
                (run.task_id,),
            ).fetchone()
            if task is None:
                raise KeyError("polling task disappeared")
            next_run = None
            if bool(task["enabled"]):
                next_run = (
                    _utc(next_run_at)
                    if next_run_at is not None
                    else run.completed_at
                    + timedelta(seconds=int(task["interval_seconds"]))
                )
            connection.execute(
                """
                UPDATE polling_tasks
                SET status = ?, next_run_at = ?, last_completed_at = ?,
                    last_error = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    task_status.value,
                    _iso(next_run) if next_run is not None else None,
                    _iso(run.completed_at),
                    run.error,
                    _iso(run.completed_at),
                    run.task_id,
                ),
            )
        return run

    def get_run(self, run_id: str) -> PollingRun:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM polling_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown polling run: {run_id}")
        return _run_from_row(row)

    def latest_run(self) -> PollingRun | None:
        items = self.list_runs(limit=1)
        return items[0] if items else None

    def list_runs(self, *, limit: int = 50, offset: int = 0) -> list[PollingRun]:
        _validate_page(limit, offset, maximum=500)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM polling_runs
                ORDER BY started_at DESC, run_id DESC LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def list_run_repositories(
        self,
        run_id: str,
        *,
        repository_id: int | None = None,
        repository: str | None = None,
    ) -> list[PollingRunRepository]:
        clauses = ["run_id = ?"]
        values: list[Any] = [run_id]
        if repository_id is not None:
            clauses.append("repository_id = ?")
            values.append(repository_id)
        if repository:
            clauses.append("repository LIKE ? COLLATE NOCASE")
            values.append(f"%{repository}%")
        with self._connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM polling_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(f"unknown polling run: {run_id}")
            rows = connection.execute(
                """
                SELECT * FROM polling_run_repositories WHERE
                """
                + " AND ".join(clauses)
                + " ORDER BY repository COLLATE NOCASE",
                values,
            ).fetchall()
        return [_run_repository_from_row(row) for row in rows]

    def list_repositories(
        self,
        *,
        repository_id: int | None = None,
        repository: str | None = None,
    ) -> list[PollingRepository]:
        clauses = ["1 = 1"]
        values: list[Any] = []
        if repository_id is not None:
            clauses.append("repository_id = ?")
            values.append(repository_id)
        if repository:
            clauses.append("repository LIKE ? COLLATE NOCASE")
            values.append(f"%{repository}%")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM polling_repositories WHERE "
                + " AND ".join(clauses)
                + " ORDER BY repository COLLATE NOCASE",
                values,
            ).fetchall()
        return [_repository_from_row(row) for row in rows]

    def repository_last_attempts(self) -> dict[str, datetime]:
        """Return the durable last scan attempt for round-robin scheduling."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT repository, MAX(started_at) AS last_started_at
                FROM polling_run_repositories
                GROUP BY repository COLLATE NOCASE
                """
            ).fetchall()
        return {
            str(row["repository"]).casefold(): _parse(row["last_started_at"])
            for row in rows
            if row["last_started_at"]
        }

    def repository_backfill_cursor(self, repository: str) -> datetime | None:
        """Return the next UTC day boundary to scan for one repository."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT next_window_start FROM polling_repository_backfill "
                "WHERE repository = ? COLLATE NOCASE",
                (repository,),
            ).fetchone()
        return _parse(str(row["next_window_start"])) if row is not None else None

    def set_repository_backfill_cursor(
        self,
        repository: str,
        next_window_start: datetime,
        *,
        now: datetime | None = None,
    ) -> None:
        """Persist the next historical UTC day without sharing repo state."""

        cursor = _utc(next_window_start)
        if any((cursor.hour, cursor.minute, cursor.second, cursor.microsecond)):
            raise ValueError("repository backfill cursor must be a UTC day boundary")
        timestamp = _utc(now or utc_now())
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO polling_repository_backfill (
                    repository, next_window_start, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(repository) DO UPDATE SET
                    next_window_start = excluded.next_window_start,
                    updated_at = excluded.updated_at
                """,
                (repository, _iso(cursor), _iso(timestamp)),
            )

    def list_candidates(
        self,
        *,
        repository_id: int | None = None,
        repository: str | None = None,
        eligible: bool | None = None,
        run_id: str | None = None,
        search: str | None = None,
        work_statuses: Sequence[WorkStatus] | None = None,
        screening_decisions: Sequence[ScreeningDecision | Literal["PENDING"]]
        | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PollingCandidate]:
        _validate_page(limit, offset, maximum=500)
        clauses, values = _candidate_filters(
            repository_id=repository_id,
            repository=repository,
            eligible=eligible,
            run_id=run_id,
            search=search,
            work_statuses=work_statuses,
            screening_decisions=screening_decisions,
        )
        values.extend((limit, offset))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM polling_candidates WHERE "
                + " AND ".join(clauses)
                + " ORDER BY last_seen_at DESC, candidate_id LIMIT ? OFFSET ?",
                values,
            ).fetchall()
        return [_candidate_from_row(row) for row in rows]

    def count_candidates(
        self,
        *,
        repository_id: int | None = None,
        repository: str | None = None,
        eligible: bool | None = None,
        run_id: str | None = None,
        search: str | None = None,
        work_statuses: Sequence[WorkStatus] | None = None,
        screening_decisions: Sequence[ScreeningDecision | Literal["PENDING"]]
        | None = None,
    ) -> int:
        clauses, values = _candidate_filters(
            repository_id=repository_id,
            repository=repository,
            eligible=eligible,
            run_id=run_id,
            search=search,
            work_statuses=work_statuses,
            screening_decisions=screening_decisions,
        )
        query = (
            "SELECT COUNT(*) AS total FROM polling_candidates WHERE "
            + " AND ".join(clauses)
        )
        with self._connect() as connection:
            row = connection.execute(query, values).fetchone()
        return int(row["total"])

    def list_issue_feed(
        self,
        *,
        repository_id: int | None = None,
        repository: str | None = None,
        eligible: bool | None = None,
        run_id: str | None = None,
        search: str | None = None,
        work_statuses: Sequence[WorkStatus] | None = None,
        screening_decisions: Sequence[ScreeningDecision | Literal["PENDING"]]
        | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[tuple[PollingCandidate, WorkItem | None, IssueScreening | None]]:
        """Return Issue candidates paired with their current Work projection."""

        candidates = self.list_candidates(
            repository_id=repository_id,
            repository=repository,
            eligible=eligible,
            run_id=run_id,
            search=search,
            work_statuses=work_statuses,
            screening_decisions=screening_decisions,
            limit=limit,
            offset=offset,
        )
        if not candidates:
            return []
        candidate_ids = [candidate.candidate_id for candidate in candidates]
        placeholders = ",".join("?" for _ in candidate_ids)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM work_items WHERE candidate_id IN (" + placeholders + ")",
                candidate_ids,
            ).fetchall()
            screening_rows = connection.execute(
                """
                SELECT screening.* FROM issue_screening_current AS screening
                JOIN polling_candidates AS candidate
                  ON candidate.candidate_id = screening.candidate_id
                 AND candidate.snapshot_digest =
                     screening.candidate_snapshot_digest
                WHERE screening.candidate_id IN ("""
                + placeholders
                + ")",
                candidate_ids,
            ).fetchall()
        work_by_candidate = {
            str(row["candidate_id"]): _work_from_row(row) for row in rows
        }
        screening_by_candidate = {
            str(row["candidate_id"]): _screening_from_row(row) for row in screening_rows
        }
        return [
            (
                candidate,
                work_by_candidate.get(candidate.candidate_id),
                screening_by_candidate.get(candidate.candidate_id),
            )
            for candidate in candidates
        ]

    def get_candidate(self, candidate_id: str) -> PollingCandidate:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM polling_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown polling candidate: {candidate_id}")
        return _candidate_from_row(row)

    def get_work_item_for_candidate(
        self,
        candidate_id_value: str,
    ) -> WorkItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM work_items WHERE candidate_id = ?",
                (candidate_id_value,),
            ).fetchone()
        return _work_from_row(row) if row is not None else None

    def get_work_item(self, work_item_id_value: str) -> WorkItem:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?",
                (work_item_id_value,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown work item: {work_item_id_value}")
        return _work_from_row(row)

    def list_work_items(
        self,
        *,
        statuses: Sequence[WorkStatus] | None = None,
        repository_id: int | None = None,
        repository: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[WorkItem]:
        _validate_page(limit, offset, maximum=500)
        clauses = ["1 = 1"]
        values: list[Any] = []
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            values.extend(status.value for status in statuses)
        if repository_id is not None:
            clauses.append("repository_id = ?")
            values.append(repository_id)
        if repository:
            clauses.append("repository LIKE ? COLLATE NOCASE")
            values.append(f"%{repository}%")
        values.extend((limit, offset))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM work_items WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC, work_item_id LIMIT ? OFFSET ?",
                values,
            ).fetchall()
        return [_work_from_row(row) for row in rows]

    def work_counts(
        self,
        *,
        repository_id: int | None = None,
        repository: str | None = None,
    ) -> dict[str, int]:
        clauses = ["1 = 1"]
        values: list[Any] = []
        if repository_id is not None:
            clauses.append("repository_id = ?")
            values.append(repository_id)
        if repository:
            clauses.append("repository LIKE ? COLLATE NOCASE")
            values.append(f"%{repository}%")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM work_items WHERE "
                + " AND ".join(clauses)
                + " GROUP BY status",
                values,
            ).fetchall()
        counts = {status.value: 0 for status in WorkStatus}
        counts.update({str(row["status"]): int(row["total"]) for row in rows})
        return counts

    def count_work_items(
        self,
        *,
        statuses: Sequence[WorkStatus] | None = None,
        repository_id: int | None = None,
        repository: str | None = None,
    ) -> int:
        clauses = ["1 = 1"]
        values: list[Any] = []
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            values.extend(status.value for status in statuses)
        if repository_id is not None:
            clauses.append("repository_id = ?")
            values.append(repository_id)
        if repository:
            clauses.append("repository LIKE ? COLLATE NOCASE")
            values.append(f"%{repository}%")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM work_items WHERE "
                + " AND ".join(clauses),
                values,
            ).fetchone()
        return int(row["total"])

    def active_work_count(self) -> int:
        """Return Work currently occupying an execution/GPU lane.

        Review is an independent model and human-verification queue.  Counting
        it as execution capacity leaves GPUs idle whenever candidates await
        review and can deadlock the whole Issue pipeline.
        """

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total FROM work_items
                WHERE status = ?
                """,
                (WorkStatus.RUNNING.value,),
            ).fetchone()
        return int(row["total"])

    def active_work_gpu_count(self) -> int:
        """Return GPUs requested by Work that currently occupies worker lanes."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT resource_requirements_json FROM work_items
                WHERE status = ?
                """,
                (WorkStatus.RUNNING.value,),
            ).fetchall()
        return sum(
            WorkResourceRequirements.model_validate_json(
                row["resource_requirements_json"]
            ).gpu_count
            for row in rows
            if row["resource_requirements_json"]
        )

    def pending_work_count(self) -> int:
        """Return Work waiting for a worker lane (queued or planning)."""

        with self._connect() as connection:
            return _pending_work_count_tx(connection)

    def plan_work_item(
        self,
        work_item_id_value: str,
        plan: WorkPlan,
        *,
        now: datetime | None = None,
    ) -> WorkItem:
        timestamp = _utc(now or utc_now())
        with self._transaction() as connection:
            current = _require_work_tx(connection, work_item_id_value)
            if current.status is not WorkStatus.QUEUED:
                raise ValueError("only queued work can enter planning")
            if current.environment_status is not EnvironmentStatus.VERIFIED:
                raise ValueError("environment verification must finish before planning")
            connection.execute(
                """
                UPDATE work_items SET status = ?, current_step = ?,
                    plan_json = ?, planning_started_at = ?, updated_at = ?
                WHERE work_item_id = ?
                """,
                (
                    WorkStatus.PLANNING.value,
                    "Main Hermes produced a plan; awaiting execution dispatch.",
                    plan.model_dump_json(),
                    _iso(timestamp),
                    _iso(timestamp),
                    work_item_id_value,
                ),
            )
            self._record_event_tx(
                connection,
                work_item_id_value,
                "work.planned",
                {
                    "summary": plan.summary,
                    "step_count": len(plan.steps),
                    "acceptance_criteria_count": len(plan.acceptance_criteria),
                },
                created_at=timestamp,
            )
            return _require_work_tx(connection, work_item_id_value)

    def verify_work_environment(
        self,
        work_item_id_value: str,
        *,
        named_baseline: str,
        resources: WorkResourceRequirements,
        now: datetime | None = None,
    ) -> WorkItem:
        timestamp = _utc(now or utc_now())
        with self._transaction() as connection:
            current = _require_work_tx(connection, work_item_id_value)
            if current.status is not WorkStatus.QUEUED:
                raise ValueError("environment verification belongs to queued Work")
            connection.execute(
                """
                UPDATE work_items SET environment_status = ?,
                    environment_verified_at = ?, named_baseline = ?,
                    resource_requirements_json = ?, last_error = NULL,
                    current_step = ?, updated_at = ?
                WHERE work_item_id = ?
                """,
                (
                    EnvironmentStatus.VERIFIED.value,
                    _iso(timestamp),
                    named_baseline,
                    resources.model_dump_json(),
                    "Environment verified; Main Hermes is preparing a plan.",
                    _iso(timestamp),
                    work_item_id_value,
                ),
            )
            self._record_event_tx(
                connection,
                work_item_id_value,
                "work.environment_verified",
                {
                    "named_baseline": named_baseline,
                    "gpu_count": resources.gpu_count,
                    "gpu_architecture": resources.gpu_architecture,
                    "worker_model": resources.worker_model,
                },
                created_at=timestamp,
            )
            return _require_work_tx(connection, work_item_id_value)

    def start_work_item(
        self,
        work_item_id_value: str,
        *,
        task_id: str,
        run_id: str,
        execution_id: str,
        now: datetime | None = None,
    ) -> WorkItem:
        timestamp = _utc(now or utc_now())
        with self._transaction() as connection:
            current = _require_work_tx(connection, work_item_id_value)
            if current.status is not WorkStatus.PLANNING or current.plan is None:
                raise ValueError("work requires a committed plan before execution")
            if (
                current.retry_not_before is not None
                and timestamp < current.retry_not_before
            ):
                raise ValueError("work capacity retry cooldown has not elapsed")
            attempt = current.execution_attempt + 1
            connection.execute(
                """
                UPDATE work_items SET status = ?, current_step = ?, task_id = ?,
                    run_id = ?, execution_id = ?, started_at = ?,
                    execution_attempt = ?, retry_not_before = NULL,
                    last_error = NULL, updated_at = ?
                WHERE work_item_id = ?
                """,
                (
                    WorkStatus.RUNNING.value,
                    "Worker execution is running.",
                    task_id,
                    run_id,
                    execution_id,
                    _iso(timestamp),
                    attempt,
                    _iso(timestamp),
                    work_item_id_value,
                ),
            )
            self._record_event_tx(
                connection,
                work_item_id_value,
                "work.started",
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "execution_id": execution_id,
                    "execution_attempt": attempt,
                },
                created_at=timestamp,
            )
            return _require_work_tx(connection, work_item_id_value)

    def retry_work_item_after_capacity_failure(
        self,
        work_item_id_value: str,
        *,
        retry_not_before: datetime,
        error: str,
        now: datetime | None = None,
    ) -> WorkItem:
        """Preserve a committed plan while releasing a capacity-failed lane."""

        timestamp = _utc(now or utc_now())
        retry_at = _utc(retry_not_before)
        if retry_at <= timestamp:
            raise ValueError("capacity retry must be scheduled in the future")
        safe_error = redact_text(error)
        with self._transaction() as connection:
            current = _require_work_tx(connection, work_item_id_value)
            if current.status is not WorkStatus.RUNNING or current.plan is None:
                raise ValueError(
                    "capacity retry requires running Work with a committed plan"
                )
            previous = {
                "task_id": current.task_id,
                "run_id": current.run_id,
                "execution_id": current.execution_id,
            }
            connection.execute(
                """
                UPDATE work_items SET status = ?, current_step = ?,
                    task_id = NULL, run_id = NULL, execution_id = NULL,
                    blocked_reason = NULL,
                    last_error = ?, retry_not_before = ?, started_at = NULL,
                    review_started_at = NULL, completed_at = NULL,
                    updated_at = ?
                WHERE work_item_id = ?
                """,
                (
                    WorkStatus.PLANNING.value,
                    "Worker provider capacity was exhausted; the committed "
                    f"plan will retry after {_iso(retry_at)}.",
                    safe_error,
                    _iso(retry_at),
                    _iso(timestamp),
                    work_item_id_value,
                ),
            )
            self._record_event_tx(
                connection,
                work_item_id_value,
                "work.capacity_retry_scheduled",
                {
                    "from": WorkStatus.RUNNING.value,
                    "to": WorkStatus.PLANNING.value,
                    "execution_attempt": current.execution_attempt,
                    "retry_not_before": _iso(retry_at),
                    "error": safe_error,
                    **previous,
                },
                created_at=timestamp,
            )
            return _require_work_tx(connection, work_item_id_value)

    def retry_cancelled_work_item(
        self,
        work_item_id_value: str,
        *,
        reason: str,
        resources: WorkResourceRequirements | None = None,
        now: datetime | None = None,
    ) -> WorkItem:
        """Return one cancellation-blocked item to its committed plan.

        This is an explicit operator recovery boundary, not an automatic retry.
        It accepts only the deterministic block produced when a controller run
        ends CANCELLED, preserves the reviewed plan and execution-attempt count,
        and clears the stale run identities before the scheduler launches a new
        immutable task.
        """

        timestamp = _utc(now or utc_now())
        safe_reason = redact_text(reason).strip()
        if not safe_reason:
            raise ValueError("cancelled Work retry requires an audit reason")
        with self._transaction() as connection:
            current = _require_work_tx(connection, work_item_id_value)
            if current.status is not WorkStatus.BLOCKED or current.plan is None:
                raise ValueError(
                    "cancelled Work retry requires blocked Work with a plan"
                )
            expected_reason = "Controller run ended CANCELLED."
            if (
                current.blocked_reason != expected_reason
                or current.current_step != expected_reason
            ):
                raise ValueError("only a controller-cancelled Work item can be retried")
            if not all((current.task_id, current.run_id, current.execution_id)):
                raise ValueError(
                    "controller-cancelled Work is missing prior execution identity"
                )
            previous = {
                "task_id": current.task_id,
                "run_id": current.run_id,
                "execution_id": current.execution_id,
            }
            effective_resources = resources or current.resource_requirements
            connection.execute(
                """
                UPDATE work_items SET status = ?, current_step = ?,
                    task_id = NULL, run_id = NULL, execution_id = NULL,
                    blocked_reason = NULL,
                    last_error = NULL, retry_not_before = NULL,
                    resource_requirements_json = ?, started_at = NULL,
                    review_started_at = NULL, completed_at = NULL, updated_at = ?
                WHERE work_item_id = ?
                """,
                (
                    WorkStatus.PLANNING.value,
                    "Operator approved one retry of the committed plan after "
                    "controller cancellation.",
                    (
                        effective_resources.model_dump_json()
                        if effective_resources is not None
                        else None
                    ),
                    _iso(timestamp),
                    work_item_id_value,
                ),
            )
            self._record_event_tx(
                connection,
                work_item_id_value,
                "work.cancelled_execution_retry_requested",
                {
                    "from": WorkStatus.BLOCKED.value,
                    "to": WorkStatus.PLANNING.value,
                    "execution_attempt": current.execution_attempt,
                    "reason": safe_reason,
                    **previous,
                },
                created_at=timestamp,
            )
            return _require_work_tx(connection, work_item_id_value)

    def retry_failed_launch_work_item(
        self,
        work_item_id_value: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> WorkItem:
        """Return one pre-start admission failure to its committed plan.

        A definitive backend admission failure can create a terminal Execution
        before ``start_work_item`` records its identity on Work. This explicit
        operator boundary accepts only that narrow projection, accounts for the
        consumed launch identity, and lets the next start derive a fresh task
        and idempotency key without editing the database out of band.
        """

        timestamp = _utc(now or utc_now())
        safe_reason = redact_text(reason).strip()
        if not safe_reason:
            raise ValueError("failed launch retry requires an audit reason")
        with self._transaction() as connection:
            current = _require_work_tx(connection, work_item_id_value)
            if current.status is not WorkStatus.FAILED or current.plan is None:
                raise ValueError("failed launch retry requires failed Work with a plan")
            if (
                current.current_step != "Worker launch failed."
                or not current.last_error
            ):
                raise ValueError(
                    "only a pre-start worker launch failure can be retried"
                )
            if any((current.task_id, current.run_id, current.execution_id)):
                raise ValueError(
                    "failed launch retry cannot replace recorded execution identity"
                )
            consumed_attempt = current.execution_attempt + 1
            connection.execute(
                """
                UPDATE work_items SET status = ?, current_step = ?,
                    execution_attempt = ?, retry_not_before = NULL,
                    blocked_reason = NULL,
                    last_error = NULL, started_at = NULL,
                    review_started_at = NULL, completed_at = NULL, updated_at = ?
                WHERE work_item_id = ?
                """,
                (
                    WorkStatus.PLANNING.value,
                    "Operator approved a fresh worker identity after a "
                    "pre-start admission failure.",
                    consumed_attempt,
                    _iso(timestamp),
                    work_item_id_value,
                ),
            )
            self._record_event_tx(
                connection,
                work_item_id_value,
                "work.failed_launch_retry_requested",
                {
                    "from": WorkStatus.FAILED.value,
                    "to": WorkStatus.PLANNING.value,
                    "execution_attempt": consumed_attempt,
                    "launch_error": current.last_error,
                    "reason": safe_reason,
                },
                created_at=timestamp,
            )
            return _require_work_tx(connection, work_item_id_value)

    def retry_failed_execution_work_item(
        self,
        work_item_id_value: str,
        *,
        reason: str,
        requested_by: str,
        now: datetime | None = None,
    ) -> WorkItem:
        """Return one failed controller execution to its committed plan.

        This operator recovery boundary accepts only Work that reached a
        controller-owned execution identity and later ended ``FAILED``.  It
        preserves the reviewed plan, environment proof, resources, and
        execution-attempt count while clearing the terminal run identities so
        the scheduler can create one fresh immutable task.
        """

        timestamp = _utc(now or utc_now())
        safe_reason = redact_text(reason).strip()
        safe_actor = redact_text(requested_by).strip()
        if not safe_reason:
            raise ValueError("failed execution retry requires an audit reason")
        if not safe_actor:
            raise ValueError("failed execution retry requires an operator")
        with self._transaction() as connection:
            current = _require_work_tx(connection, work_item_id_value)
            expected_error = "Controller run ended FAILED."
            if current.status is not WorkStatus.FAILED or current.plan is None:
                raise ValueError(
                    "failed execution retry requires failed Work with a plan"
                )
            if (
                current.current_step != expected_error
                or current.last_error != expected_error
            ):
                raise ValueError("only a controller-failed Work item can be retried")
            if not all((current.task_id, current.run_id, current.execution_id)):
                raise ValueError(
                    "controller-failed Work is missing prior execution identity"
                )
            previous = {
                "task_id": current.task_id,
                "run_id": current.run_id,
                "execution_id": current.execution_id,
            }
            connection.execute(
                """
                UPDATE work_items SET status = ?, current_step = ?,
                    task_id = NULL, run_id = NULL, execution_id = NULL,
                    blocked_reason = NULL,
                    last_error = NULL, retry_not_before = NULL,
                    started_at = NULL, review_started_at = NULL,
                    completed_at = NULL, updated_at = ?
                WHERE work_item_id = ?
                """,
                (
                    WorkStatus.PLANNING.value,
                    "Operator approved one retry after a failed controller execution.",
                    _iso(timestamp),
                    work_item_id_value,
                ),
            )
            self._record_event_tx(
                connection,
                work_item_id_value,
                "work.failed_execution_retry_requested",
                {
                    "from": WorkStatus.FAILED.value,
                    "to": WorkStatus.PLANNING.value,
                    "execution_attempt": current.execution_attempt,
                    "controller_error": expected_error,
                    "reason": safe_reason,
                    "requested_by": safe_actor,
                    **previous,
                },
                created_at=timestamp,
            )
            return _require_work_tx(connection, work_item_id_value)

    def retry_miscounted_review_budget_work_item(
        self,
        work_item_id_value: str,
        *,
        reason: str,
        requested_by: str,
        max_review_attempts: int,
        now: datetime | None = None,
    ) -> WorkItem:
        """Resume a review revision blocked only by non-review failures.

        ``execution_attempt`` includes provider and infrastructure failures,
        while the independent-review budget applies only to candidates that
        actually reached Review.  This operator boundary repairs the narrow
        legacy projection where total executions exhausted that budget even
        though the durable ``work.review`` count remains below the configured
        limit.  The reviewed candidate is preserved so its complete feedback
        is carried into the fresh immutable IssueTask.
        """

        timestamp = _utc(now or utc_now())
        safe_reason = redact_text(reason).strip()
        safe_actor = redact_text(requested_by).strip()
        if not safe_reason:
            raise ValueError("review-budget retry requires an audit reason")
        if not safe_actor:
            raise ValueError("review-budget retry requires an operator")
        if max_review_attempts < 1:
            raise ValueError("review-budget retry limit must be positive")
        expected_step = "Independent review revision budget was exhausted."
        with self._transaction() as connection:
            current = _require_work_tx(connection, work_item_id_value)
            if (
                current.status is not WorkStatus.BLOCKED
                or current.plan is None
                or current.current_step != expected_step
                or current.internal_candidate_id is None
            ):
                raise ValueError(
                    "review-budget retry requires the deterministic "
                    "independent-review budget block"
                )
            if not all((current.task_id, current.run_id, current.execution_id)):
                raise ValueError(
                    "review-budget blocked Work is missing prior execution identity"
                )
            row = connection.execute(
                """
                SELECT COUNT(*) AS event_count
                FROM work_events
                WHERE work_item_id = ? AND event_type = ?
                """,
                (work_item_id_value, "work.review"),
            ).fetchone()
            review_attempts = int(row["event_count"])
            if review_attempts >= max_review_attempts:
                raise ValueError(
                    "independent-review candidate budget is genuinely exhausted"
                )
            previous = {
                "task_id": current.task_id,
                "run_id": current.run_id,
                "execution_id": current.execution_id,
            }
            connection.execute(
                """
                UPDATE work_items SET status = ?, current_step = ?,
                    task_id = NULL, run_id = NULL, execution_id = NULL,
                    blocked_reason = NULL, last_error = NULL,
                    retry_not_before = NULL, started_at = NULL,
                    review_started_at = NULL, completed_at = NULL,
                    updated_at = ?
                WHERE work_item_id = ?
                """,
                (
                    WorkStatus.PLANNING.value,
                    "Operator resumed review after excluding non-review "
                    "failures from the candidate budget.",
                    _iso(timestamp),
                    work_item_id_value,
                ),
            )
            self._record_event_tx(
                connection,
                work_item_id_value,
                "work.review_budget_retry_requested",
                {
                    "from": WorkStatus.BLOCKED.value,
                    "to": WorkStatus.PLANNING.value,
                    "reviewed_candidate_id": current.internal_candidate_id,
                    "reviewed_candidate_attempts": review_attempts,
                    "total_execution_attempts": current.execution_attempt,
                    "max_review_attempts": max_review_attempts,
                    "reason": safe_reason,
                    "requested_by": safe_actor,
                    **previous,
                },
                created_at=timestamp,
            )
            return _require_work_tx(connection, work_item_id_value)

    def request_review_revision(
        self,
        work_item_id_value: str,
        *,
        reviewed_candidate_id: str,
        review_feedback: dict[str, Any],
        now: datetime | None = None,
    ) -> WorkItem:
        """Return non-unanimous Review Work to its committed Worker plan.

        The reviewed candidate and its verdicts remain append-only in the
        candidate store.  Work retains only the pointer to that candidate so
        the next immutable IssueTask can carry bounded reviewer feedback.
        """

        candidate_id_value = reviewed_candidate_id.strip()
        if not candidate_id_value:
            raise ValueError("review revision requires a candidate identity")
        if redact_data(review_feedback) != review_feedback:
            raise ValueError("review feedback contains credential-shaped data")
        encoded_feedback = json.dumps(
            review_feedback,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        if len(encoded_feedback) > 65_536:
            raise ValueError("review feedback exceeds the bounded Work event")
        safe_feedback = json.loads(encoded_feedback)
        timestamp = _utc(now or utc_now())
        with self._transaction() as connection:
            current = _require_work_tx(connection, work_item_id_value)
            if current.status is not WorkStatus.REVIEW or current.plan is None:
                raise ValueError(
                    "review revision requires Review Work with a committed plan"
                )
            if current.internal_candidate_id != candidate_id_value:
                raise ValueError(
                    "review revision candidate differs from the Work projection"
                )
            if not all((current.task_id, current.run_id, current.execution_id)):
                raise ValueError(
                    "review revision requires the prior execution identity"
                )
            previous = {
                "task_id": current.task_id,
                "run_id": current.run_id,
                "execution_id": current.execution_id,
            }
            connection.execute(
                """
                UPDATE work_items SET status = ?, current_step = ?,
                    task_id = NULL, run_id = NULL, execution_id = NULL,
                    blocked_reason = NULL, last_error = NULL,
                    retry_not_before = NULL, started_at = NULL,
                    review_started_at = NULL, completed_at = NULL,
                    updated_at = ?
                WHERE work_item_id = ?
                """,
                (
                    WorkStatus.PLANNING.value,
                    "Independent review requested a bounded worker revision.",
                    _iso(timestamp),
                    work_item_id_value,
                ),
            )
            self._record_event_tx(
                connection,
                work_item_id_value,
                "work.review_revision_requested",
                {
                    "from": WorkStatus.REVIEW.value,
                    "to": WorkStatus.PLANNING.value,
                    "reviewed_candidate_id": candidate_id_value,
                    "execution_attempt": current.execution_attempt,
                    "review_feedback": safe_feedback,
                    **previous,
                },
                created_at=timestamp,
            )
            return _require_work_tx(connection, work_item_id_value)

    def retry_policy_blocked_work_item(
        self,
        work_item_id_value: str,
        *,
        reason: str,
        requested_by: str,
        now: datetime | None = None,
    ) -> WorkItem:
        """Return one pre-execution policy false-positive to its plan.

        This boundary is deliberately narrower than a general unblock.  It
        accepts only the deterministic external-GitHub-action blocker, after
        environment verification and before any task/run/execution identity
        exists.  The committed plan and resource proof remain unchanged.
        """

        timestamp = _utc(now or utc_now())
        safe_reason = redact_text(reason).strip()
        safe_actor = redact_text(requested_by).strip()
        if not safe_reason:
            raise ValueError("policy-blocked Work retry requires an audit reason")
        if not safe_actor:
            raise ValueError("policy-blocked Work retry requires an operator")
        expected_reason = (
            "Committed plan requires an external GitHub action, but workers "
            "may only produce and verify local repository changes."
        )
        with self._transaction() as connection:
            current = _require_work_tx(connection, work_item_id_value)
            if (
                current.status is not WorkStatus.BLOCKED
                or current.plan is None
                or current.environment_status is not EnvironmentStatus.VERIFIED
                or current.blocked_reason != expected_reason
            ):
                raise ValueError(
                    "only a verified pre-execution GitHub-policy block can be retried"
                )
            if any((current.task_id, current.run_id, current.execution_id)):
                raise ValueError(
                    "policy-blocked Work cannot replace an execution identity"
                )
            connection.execute(
                """
                UPDATE work_items SET status = ?, current_step = ?,
                    blocked_reason = NULL, last_error = NULL,
                    retry_not_before = NULL, completed_at = NULL,
                    updated_at = ?
                WHERE work_item_id = ?
                """,
                (
                    WorkStatus.PLANNING.value,
                    "Operator requested a policy re-evaluation of the "
                    "unchanged committed plan.",
                    _iso(timestamp),
                    work_item_id_value,
                ),
            )
            self._record_event_tx(
                connection,
                work_item_id_value,
                "work.policy_block_retry_requested",
                {
                    "from": WorkStatus.BLOCKED.value,
                    "to": WorkStatus.PLANNING.value,
                    "blocked_reason": expected_reason,
                    "reason": safe_reason,
                    "requested_by": safe_actor,
                },
                created_at=timestamp,
            )
            return _require_work_tx(connection, work_item_id_value)

    def transition_work_item(
        self,
        work_item_id_value: str,
        target: WorkStatus,
        *,
        current_step: str,
        internal_candidate_id: str | None = None,
        reason: str | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> WorkItem:
        timestamp = _utc(now or utc_now())
        safe_reason = redact_text(reason) if reason else None
        safe_error = redact_text(error) if error else None
        with self._transaction() as connection:
            current = _require_work_tx(connection, work_item_id_value)
            if target is current.status:
                return current
            if target not in _WORK_TRANSITIONS[current.status]:
                raise ValueError(
                    f"invalid Work transition: {current.status.value} -> {target.value}"
                )
            completed_at = _iso(timestamp) if target in TERMINAL_WORK_STATUSES else None
            review_started_at = (
                _iso(timestamp)
                if target is WorkStatus.REVIEW
                else (
                    _iso(current.review_started_at)
                    if current.review_started_at is not None
                    else None
                )
            )
            connection.execute(
                """
                UPDATE work_items SET status = ?, current_step = ?,
                    internal_candidate_id = COALESCE(?, internal_candidate_id),
                    blocked_reason = ?, last_error = ?, review_started_at = ?,
                    retry_not_before = NULL, completed_at = ?, updated_at = ?
                WHERE work_item_id = ?
                """,
                (
                    target.value,
                    current_step,
                    internal_candidate_id,
                    safe_reason if target is WorkStatus.BLOCKED else None,
                    safe_error,
                    review_started_at,
                    completed_at,
                    _iso(timestamp),
                    work_item_id_value,
                ),
            )
            self._record_event_tx(
                connection,
                work_item_id_value,
                f"work.{target.value}",
                {
                    "from": current.status.value,
                    "to": target.value,
                    "reason": safe_reason,
                    "error": safe_error,
                    "internal_candidate_id": internal_candidate_id,
                },
                created_at=timestamp,
            )
            return _require_work_tx(connection, work_item_id_value)

    def list_work_events(
        self,
        *,
        work_item_id_value: str | None = None,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> list[WorkEvent]:
        if after_sequence < 0:
            raise ValueError("event cursor cannot be negative")
        _validate_page(limit, 0, maximum=1000)
        clauses = ["sequence > ?"]
        values: list[Any] = [after_sequence]
        if work_item_id_value is not None:
            clauses.append("work_item_id = ?")
            values.append(work_item_id_value)
        values.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM work_events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY sequence LIMIT ?",
                values,
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def count_work_events(
        self,
        *,
        work_item_id_value: str,
        event_type: str,
    ) -> int:
        """Count one durable Work event type without a pagination ceiling."""

        normalized_type = event_type.strip()
        if not normalized_type:
            raise ValueError("event type cannot be empty")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS event_count
                FROM work_events
                WHERE work_item_id = ? AND event_type = ?
                """,
                (work_item_id_value, normalized_type),
            ).fetchone()
        return int(row["event_count"])

    def record_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        work_item_id_value: str | None = None,
        now: datetime | None = None,
    ) -> WorkEvent:
        with self._transaction() as connection:
            return self._record_event_tx(
                connection,
                work_item_id_value,
                event_type,
                payload,
                created_at=_utc(now or utc_now()),
            )

    def get_manager_state(self) -> ProjectManagerState | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM project_manager_state
                WHERE manager_id = ?
                """,
                ("main-hermes-project-manager",),
            ).fetchone()
        return _manager_from_row(row) if row is not None else None

    def put_manager_state(
        self,
        state: ProjectManagerState,
        *,
        expected_version: int | None,
    ) -> ProjectManagerState:
        now = _utc(state.updated_at)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT version FROM project_manager_state WHERE manager_id = ?
                """,
                (state.manager_id,),
            ).fetchone()
            if existing is None:
                if expected_version is not None:
                    raise ValueError("project manager state does not exist")
                stored = state.model_copy(update={"version": 0})
                connection.execute(
                    """
                    INSERT INTO project_manager_state (
                        manager_id, session_id, handle_json,
                        instructions_digest, snapshot_digest,
                        last_event_sequence, last_turn_at, last_error,
                        version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _manager_values(stored),
                )
                return stored
            current_version = int(existing["version"])
            if expected_version != current_version:
                raise ValueError("project manager state changed concurrently")
            stored = state.model_copy(update={"version": current_version + 1})
            cursor = connection.execute(
                """
                UPDATE project_manager_state SET
                    session_id = ?, handle_json = ?, instructions_digest = ?,
                    snapshot_digest = ?, last_event_sequence = ?,
                    last_turn_at = ?, last_error = ?, version = ?, updated_at = ?
                WHERE manager_id = ? AND version = ?
                """,
                (
                    stored.session_id,
                    stored.handle.model_dump_json(),
                    stored.instructions_digest,
                    stored.snapshot_digest,
                    stored.last_event_sequence,
                    _iso(stored.last_turn_at) if stored.last_turn_at else None,
                    redact_text(stored.last_error) if stored.last_error else None,
                    stored.version,
                    _iso(now),
                    stored.manager_id,
                    current_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("project manager state changed concurrently")
            return stored

    def reset_manager_state(
        self,
        state: ProjectManagerState,
    ) -> ProjectManagerState:
        """Replace the manager handle when AGENT.md changes."""

        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM project_manager_state WHERE manager_id = ?",
                (state.manager_id,),
            )
            stored = state.model_copy(update={"version": 0})
            connection.execute(
                """
                INSERT INTO project_manager_state (
                    manager_id, session_id, handle_json, instructions_digest,
                    snapshot_digest, last_event_sequence, last_turn_at,
                    last_error, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _manager_values(stored),
            )
        return stored

    def overview(
        self,
        *,
        repository_id: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = _utc(now or utc_now())
        today = timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
        week = today - timedelta(days=today.weekday())
        task = self.get_task()
        latest = self.latest_run()
        manager = self.get_manager_state()
        repositories = self.list_repositories(repository_id=repository_id)
        all_repositories = self.list_repositories()

        candidate_counts = {
            "total": self.count_candidates(repository_id=repository_id),
            "matched": self.count_candidates(
                repository_id=repository_id,
                eligible=True,
            ),
            "filtered": self.count_candidates(
                repository_id=repository_id,
                eligible=False,
            ),
        }
        screening_counts = self.screening_counts(repository_id=repository_id)
        work_counts = self.work_counts(repository_id=repository_id)

        with self._connect() as connection:
            if repository_id is None:
                scan_row = connection.execute(
                    """
                    SELECT COALESCE(SUM(issues_seen), 0) AS issues_seen,
                           COUNT(*) AS run_count
                    FROM polling_runs
                    WHERE status != ?
                    """,
                    (PollingRunStatus.RUNNING.value,),
                ).fetchone()
            else:
                scan_row = connection.execute(
                    """
                    SELECT COALESCE(SUM(issues_seen), 0) AS issues_seen,
                           COUNT(DISTINCT run_id) AS run_count
                    FROM polling_run_repositories
                    WHERE repository_id = ? AND status != ?
                    """,
                    (repository_id, RepositoryScanStatus.RUNNING.value),
                ).fetchone()

            time_clauses = ["completed_at IS NOT NULL"]
            time_values: list[Any] = []
            if repository_id is not None:
                time_clauses.append("repository_id = ?")
                time_values.append(repository_id)
            processed_row = connection.execute(
                "SELECT "
                "SUM(CASE WHEN completed_at >= ? THEN 1 ELSE 0 END) AS today, "
                "SUM(CASE WHEN completed_at >= ? THEN 1 ELSE 0 END) AS week "
                "FROM work_items WHERE " + " AND ".join(time_clauses),
                [_iso(today), _iso(week), *time_values],
            ).fetchone()

            latest_scan_rows = connection.execute(
                """
                SELECT repository.repository_id,
                       scan.run_id,
                       COALESCE(scan.window_start, run.cutoff) AS cutoff,
                       COALESCE(
                           scan.window_end,
                           scan.completed_at,
                           scan.started_at
                       ) AS window_end,
                       COALESCE(scan.scan_mode, 'rolling') AS scan_mode,
                       scan.status,
                       scan.issues_seen,
                       scan.candidates_matched,
                       scan.work_items_queued,
                       scan.started_at,
                       scan.completed_at,
                       scan.error
                FROM polling_repositories AS repository
                JOIN polling_run_repositories AS scan
                  ON scan.repository_id = repository.repository_id
                  OR (
                      scan.repository_id IS NULL
                      AND scan.repository = repository.repository COLLATE NOCASE
                  )
                JOIN polling_runs AS run ON run.run_id = scan.run_id
                WHERE scan.rowid = (
                      SELECT latest.rowid
                      FROM polling_run_repositories AS latest
                      WHERE latest.repository_id = repository.repository_id
                         OR (
                             latest.repository_id IS NULL
                             AND latest.repository = repository.repository
                                 COLLATE NOCASE
                         )
                      ORDER BY latest.started_at DESC, latest.run_id DESC
                      LIMIT 1
                  )
                """
            ).fetchall()
            latest_scans = {
                int(row["repository_id"]): row for row in latest_scan_rows
            }
            coverage_scan_rows = connection.execute(
                """
                SELECT repository.repository_id,
                       scan.run_id,
                       COALESCE(scan.window_start, run.cutoff) AS cutoff,
                       COALESCE(
                           scan.window_end,
                           scan.completed_at,
                           scan.started_at
                       ) AS window_end,
                       COALESCE(scan.scan_mode, 'rolling') AS scan_mode,
                       scan.status,
                       scan.issues_seen,
                       scan.candidates_matched,
                       scan.work_items_queued,
                       scan.started_at,
                       scan.completed_at,
                       scan.error
                FROM polling_repositories AS repository
                JOIN polling_run_repositories AS scan
                  ON scan.repository_id = repository.repository_id
                  OR (
                      scan.repository_id IS NULL
                      AND scan.repository = repository.repository COLLATE NOCASE
                  )
                JOIN polling_runs AS run ON run.run_id = scan.run_id
                WHERE scan.status IN (?, ?)
                  AND scan.rowid = (
                      SELECT coverage.rowid
                      FROM polling_run_repositories AS coverage
                      WHERE (
                          coverage.repository_id = repository.repository_id
                          OR (
                              coverage.repository_id IS NULL
                              AND coverage.repository = repository.repository
                                  COLLATE NOCASE
                          )
                      )
                      AND coverage.status IN (?, ?)
                      ORDER BY coverage.started_at DESC, coverage.run_id DESC
                      LIMIT 1
                  )
                """,
                (
                    RepositoryScanStatus.COMPLETED.value,
                    RepositoryScanStatus.PARTIAL.value,
                    RepositoryScanStatus.COMPLETED.value,
                    RepositoryScanStatus.PARTIAL.value,
                ),
            ).fetchall()
            coverage_scans = {
                int(row["repository_id"]): row for row in coverage_scan_rows
            }

            repository_stats: list[dict[str, Any]] = []
            for repository in all_repositories:
                counts = connection.execute(
                    """
                    SELECT COUNT(*) AS issue_count,
                           SUM(CASE WHEN eligible = 1 THEN 1 ELSE 0 END) AS matched,
                           SUM(CASE WHEN eligible = 0 THEN 1 ELSE 0 END) AS filtered
                    FROM polling_candidates WHERE repository_id = ?
                    """,
                    (repository.repository_id,),
                ).fetchone()
                scans = connection.execute(
                    """
                    SELECT COALESCE(SUM(issues_seen), 0) AS issues_seen
                    FROM polling_run_repositories
                    WHERE repository_id = ? AND status != ?
                    """,
                    (
                        repository.repository_id,
                        RepositoryScanStatus.RUNNING.value,
                    ),
                ).fetchone()
                latest_scan = latest_scans.get(repository.repository_id)
                coverage_scan = coverage_scans.get(repository.repository_id)
                coverage_results = {
                    "seen": 0,
                    "retained": 0,
                    "matched": 0,
                    "filtered": 0,
                    "selected": 0,
                    "work": 0,
                    "done": 0,
                }
                results_updated_at: str | None = None
                if coverage_scan is not None:
                    batch = connection.execute(
                        """
                        SELECT COUNT(batch.candidate_id) AS retained,
                               SUM(CASE WHEN batch.eligible = 1 THEN 1 ELSE 0 END)
                                   AS matched,
                               SUM(CASE WHEN batch.eligible = 0 THEN 1 ELSE 0 END)
                                   AS filtered,
                               SUM(CASE WHEN screening.decision = ? THEN 1 ELSE 0 END)
                                   AS selected,
                               SUM(CASE WHEN work.work_item_id IS NOT NULL THEN 1 ELSE 0 END)
                                   AS work_items,
                               SUM(CASE WHEN work.status = ? THEN 1 ELSE 0 END)
                                   AS done,
                               MAX(batch.observed_at) AS observed_at,
                               MAX(screening.screened_at) AS screened_at,
                               MAX(work.updated_at) AS work_updated_at
                        FROM polling_run_candidates AS batch
                        LEFT JOIN issue_screening_current AS screening
                          ON screening.candidate_id = batch.candidate_id
                         AND screening.candidate_snapshot_digest =
                             batch.snapshot_digest
                        LEFT JOIN work_items AS work
                          ON work.candidate_id = batch.candidate_id
                        WHERE batch.run_id = ? AND batch.repository_id = ?
                        """,
                        (
                            ScreeningDecision.SELECT.value,
                            WorkStatus.DONE.value,
                            coverage_scan["run_id"],
                            repository.repository_id,
                        ),
                    ).fetchone()
                    coverage_results = {
                        "seen": int(coverage_scan["issues_seen"]),
                        "retained": int(batch["retained"] or 0),
                        "matched": int(batch["matched"] or 0),
                        "filtered": int(batch["filtered"] or 0),
                        "selected": int(batch["selected"] or 0),
                        "work": int(batch["work_items"] or 0),
                        "done": int(batch["done"] or 0),
                    }
                    update_values = [
                        value
                        for value in (
                            coverage_scan["completed_at"],
                            coverage_scan["started_at"],
                            batch["observed_at"],
                            batch["screened_at"],
                            batch["work_updated_at"],
                        )
                        if value
                    ]
                    if update_values:
                        results_updated_at = max(
                            _parse(str(value)) for value in update_values
                        ).isoformat()
                repository_stats.append({
                    "repository_id": repository.repository_id,
                    "repository": repository.repository,
                    "html_url": repository.html_url,
                    "last_scanned_at": (
                        repository.last_scanned_at.isoformat()
                        if repository.last_scanned_at
                        else None
                    ),
                    "last_status": (
                        repository.last_status.value if repository.last_status else None
                    ),
                    "issues_seen": int(scans["issues_seen"]),
                    "issue_count": int(counts["issue_count"]),
                    "matched": int(counts["matched"] or 0),
                    "filtered": int(counts["filtered"] or 0),
                    "screening": self.screening_counts(
                        repository_id=repository.repository_id
                    ),
                    "work_items": self.count_work_items(
                        repository_id=repository.repository_id
                    ),
                    "latest_scan": (
                        _repository_scan_payload(latest_scan)
                        if latest_scan is not None
                        else None
                    ),
                    "coverage_scan": (
                        _repository_scan_payload(coverage_scan)
                        if coverage_scan is not None
                        else None
                    ),
                    "coverage_stale": bool(
                        latest_scan is not None
                        and (
                            coverage_scan is None
                            or latest_scan["run_id"] != coverage_scan["run_id"]
                        )
                    ),
                    "coverage_results": coverage_results,
                    "results_updated_at": results_updated_at,
                })

        return {
            "task": task.model_dump(mode="json"),
            "latest_run": (
                latest.model_dump(mode="json") if latest is not None else None
            ),
            "selected_repository_id": repository_id,
            "repository_count": len(all_repositories),
            "repositories_in_view": len(repositories),
            "issues_seen": int(scan_row["issues_seen"]),
            "polling_run_count": int(scan_row["run_count"]),
            "candidate_counts": candidate_counts,
            "screening_counts": screening_counts,
            "work_counts": work_counts,
            "processed": {
                "today": int(processed_row["today"] or 0),
                "this_week": int(processed_row["week"] or 0),
            },
            "repository_stats": repository_stats,
            "manager": (
                {
                    "session_id": manager.session_id,
                    "runtime_status": manager.handle.status.value,
                    "instructions_digest": manager.instructions_digest,
                    "last_turn_at": (
                        manager.last_turn_at.isoformat()
                        if manager.last_turn_at
                        else None
                    ),
                    "last_error": manager.last_error,
                }
                if manager is not None
                else None
            ),
        }

    def _record_event_tx(
        self,
        connection: sqlite3.Connection,
        work_item_id_value: str | None,
        event_type: str,
        payload: dict[str, Any],
        *,
        created_at: datetime,
    ) -> WorkEvent:
        safe_payload = redact_data(payload)
        if not isinstance(safe_payload, dict):
            safe_payload = {"value": safe_payload}
        seed = json.dumps(
            {
                "work_item_id": work_item_id_value,
                "event_type": event_type,
                "payload": safe_payload,
                "created_at": _iso(created_at),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        )
        event_id = "work-event-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
        cursor = connection.execute(
            """
            INSERT INTO work_events (
                event_id, work_item_id, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                event_id,
                work_item_id_value,
                event_type,
                _json(safe_payload),
                _iso(created_at),
            ),
        )
        return WorkEvent(
            sequence=int(cursor.lastrowid),
            event_id=event_id,
            work_item_id=work_item_id_value,
            event_type=event_type,
            payload=safe_payload,
            created_at=created_at,
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
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
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


def open_polling_store(config: PollingConfig) -> SqlitePollingStore:
    store = SqlitePollingStore(config.sqlite_path)
    store.configure_task(
        enabled=config.enabled,
        interval_seconds=config.interval_seconds,
    )
    store.recover_interrupted_run()
    return store


def candidate_id(repository_id: int, issue_number: int) -> str:
    payload = f"{repository_id}\0{issue_number}".encode("utf-8")
    return "poll-candidate-" + hashlib.sha256(payload).hexdigest()[:24]


def candidate_snapshot_digest(candidate: PollingCandidate) -> str:
    """Return the stable identity screened by an independent Subagent.

    Discovery timestamps and polling-run ids are deliberately excluded. A
    substantive Issue, evidence, or mechanical-filter change invalidates the
    prior screening decision; merely seeing the same Issue again does not.
    """

    payload = {
        "repository_id": candidate.repository_id,
        "repository": candidate.repository,
        "issue_number": candidate.issue_number,
        "issue_url": candidate.issue_url,
        "title": candidate.title,
        "body": candidate.body,
        "labels": candidate.labels,
        "author": candidate.author,
        "comments": candidate.comments,
        "evidence_score": candidate.evidence_score,
        "eligible": candidate.eligible,
        "filter_reasons": candidate.filter_reasons,
        "created_at": candidate.created_at.isoformat(),
        "updated_at": candidate.updated_at.isoformat(),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def work_item_id(candidate_id_value: str) -> str:
    return "work-" + hashlib.sha256(candidate_id_value.encode("utf-8")).hexdigest()[:24]


def _pending_work_count_tx(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS total FROM work_items
        WHERE status IN (?, ?)
        """,
        (WorkStatus.QUEUED.value, WorkStatus.PLANNING.value),
    ).fetchone()
    return int(row["total"])


def _operator_selected_tx(
    connection: sqlite3.Connection,
    work_item_id_value: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1 FROM work_events
        WHERE work_item_id = ? AND event_type = 'work.operator_selected'
        LIMIT 1
        """,
        (work_item_id_value,),
    ).fetchone()
    return row is not None


def _require_running_run(
    connection: sqlite3.Connection,
    run_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM polling_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown polling run: {run_id}")
    if row["status"] != PollingRunStatus.RUNNING.value:
        raise ValueError("polling run is already final")
    return row


def _require_work_tx(
    connection: sqlite3.Connection,
    work_item_id_value: str,
) -> WorkItem:
    row = connection.execute(
        "SELECT * FROM work_items WHERE work_item_id = ?",
        (work_item_id_value,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown work item: {work_item_id_value}")
    return _work_from_row(row)


def _task_from_row(row: sqlite3.Row) -> PollingTask:
    return PollingTask(
        task_id=row["task_id"],
        enabled=bool(row["enabled"]),
        interval_seconds=int(row["interval_seconds"]),
        status=PollingTaskStatus(row["status"]),
        next_run_at=_parse_optional(row["next_run_at"]),
        last_run_id=row["last_run_id"],
        last_started_at=_parse_optional(row["last_started_at"]),
        last_completed_at=_parse_optional(row["last_completed_at"]),
        last_error=row["last_error"],
        created_at=_parse(row["created_at"]),
        updated_at=_parse(row["updated_at"]),
    )


def _repository_from_row(row: sqlite3.Row) -> PollingRepository:
    return PollingRepository(
        repository_id=int(row["repository_id"]),
        repository=row["repository"],
        enabled=bool(row["enabled"]),
        default_branch=row["default_branch"],
        html_url=row["html_url"],
        last_scanned_at=_parse_optional(row["last_scanned_at"]),
        last_status=(
            RepositoryScanStatus(row["last_status"]) if row["last_status"] else None
        ),
        last_error=row["last_error"],
        created_at=_parse(row["created_at"]),
        updated_at=_parse(row["updated_at"]),
    )


def _run_from_row(row: sqlite3.Row) -> PollingRun:
    return PollingRun(
        run_id=row["run_id"],
        task_id=row["task_id"],
        status=PollingRunStatus(row["status"]),
        cutoff=_parse(row["cutoff"]),
        repositories_requested=int(row["repositories_requested"]),
        repositories_scanned=int(row["repositories_scanned"]),
        repositories_failed=int(row["repositories_failed"]),
        issues_seen=int(row["issues_seen"]),
        candidates_matched=int(row["candidates_matched"]),
        work_items_queued=int(row["work_items_queued"]),
        started_at=_parse(row["started_at"]),
        completed_at=_parse_optional(row["completed_at"]),
        error=row["error"],
    )


def _run_repository_from_row(row: sqlite3.Row) -> PollingRunRepository:
    return PollingRunRepository(
        run_id=row["run_id"],
        repository_id=(
            int(row["repository_id"]) if row["repository_id"] is not None else None
        ),
        repository=row["repository"],
        status=RepositoryScanStatus(row["status"]),
        window_start=_parse_optional(row["window_start"]),
        window_end=_parse_optional(row["window_end"]),
        scan_mode=row["scan_mode"],
        issues_seen=int(row["issues_seen"]),
        candidates_matched=int(row["candidates_matched"]),
        work_items_queued=int(row["work_items_queued"]),
        started_at=_parse(row["started_at"]),
        completed_at=_parse_optional(row["completed_at"]),
        error=row["error"],
    )


def _repository_scan_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "cutoff": row["cutoff"],
        "window_end": row["window_end"],
        "scan_mode": row["scan_mode"],
        "status": row["status"],
        "issues_seen": int(row["issues_seen"]),
        "candidates_matched": int(row["candidates_matched"]),
        "work_items_queued": int(row["work_items_queued"]),
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "error": row["error"],
    }


def _candidate_from_row(row: sqlite3.Row) -> PollingCandidate:
    return PollingCandidate(
        candidate_id=row["candidate_id"],
        repository_id=int(row["repository_id"]),
        repository=row["repository"],
        issue_number=int(row["issue_number"]),
        issue_url=row["issue_url"],
        title=row["title"],
        body=row["body"],
        labels=json.loads(row["labels_json"]),
        author=row["author"],
        comments=int(row["comments"]),
        evidence_score=int(row["evidence_score"]),
        eligible=bool(row["eligible"]),
        filter_reasons=json.loads(row["filter_reasons_json"]),
        created_at=_parse(row["created_at"]),
        updated_at=_parse(row["updated_at"]),
        first_seen_at=_parse(row["first_seen_at"]),
        last_seen_at=_parse(row["last_seen_at"]),
        latest_run_id=row["latest_run_id"],
    )


def _screening_from_row(row: sqlite3.Row) -> IssueScreening:
    columns = set(row.keys())
    return IssueScreening(
        candidate_id=row["candidate_id"],
        candidate_snapshot_digest=row["candidate_snapshot_digest"],
        decision=ScreeningDecision(row["decision"]),
        machine_compatibility=MachineCompatibility(row["machine_compatibility"]),
        task_kind=ScreeningTaskKind(row["task_kind"]),
        reason=row["reason"],
        required_environment=ScreeningEnvironmentRequirements.model_validate_json(
            row["required_environment_json"]
        ),
        evidence=json.loads(row["evidence_json"]),
        uncertainties=json.loads(row["uncertainties_json"]),
        model_provider=row["model_provider"],
        model=row["model"],
        session_id=row["session_id"],
        profile_digest=row["profile_digest"],
        soul_digest=row["soul_digest"],
        agent_digest=row["agent_digest"],
        revision=int(row["revision"]) if "revision" in columns else 1,
        screening_trigger=(
            str(row["screening_trigger"])
            if "screening_trigger" in columns
            else "initial"
        ),
        requested_by=(row["requested_by"] if "requested_by" in columns else None),
        request_reason=(row["request_reason"] if "request_reason" in columns else None),
        probe_evidence=(
            json.loads(row["probe_evidence_json"])
            if "probe_evidence_json" in columns
            else []
        ),
        screened_at=_parse(row["screened_at"]),
    )


def _work_from_row(row: sqlite3.Row) -> WorkItem:
    return WorkItem(
        work_item_id=row["work_item_id"],
        candidate_id=row["candidate_id"],
        repository_id=int(row["repository_id"]),
        repository=row["repository"],
        issue_number=int(row["issue_number"]),
        issue_url=row["issue_url"],
        title=row["title"],
        status=WorkStatus(row["status"]),
        current_step=row["current_step"],
        plan=(
            WorkPlan.model_validate_json(row["plan_json"]) if row["plan_json"] else None
        ),
        environment_status=EnvironmentStatus(row["environment_status"]),
        environment_verified_at=_parse_optional(row["environment_verified_at"]),
        named_baseline=row["named_baseline"],
        resource_requirements=(
            WorkResourceRequirements.model_validate_json(
                row["resource_requirements_json"]
            )
            if row["resource_requirements_json"]
            else None
        ),
        task_id=row["task_id"],
        run_id=row["run_id"],
        execution_id=row["execution_id"],
        internal_candidate_id=row["internal_candidate_id"],
        blocked_reason=row["blocked_reason"],
        last_error=row["last_error"],
        execution_attempt=int(row["execution_attempt"]),
        retry_not_before=_parse_optional(row["retry_not_before"]),
        queued_at=_parse(row["queued_at"]),
        planning_started_at=_parse_optional(row["planning_started_at"]),
        started_at=_parse_optional(row["started_at"]),
        review_started_at=_parse_optional(row["review_started_at"]),
        completed_at=_parse_optional(row["completed_at"]),
        updated_at=_parse(row["updated_at"]),
    )


def _event_from_row(row: sqlite3.Row) -> WorkEvent:
    return WorkEvent(
        sequence=int(row["sequence"]),
        event_id=row["event_id"],
        work_item_id=row["work_item_id"],
        event_type=row["event_type"],
        payload=json.loads(row["payload_json"]),
        created_at=_parse(row["created_at"]),
    )


def _manager_from_row(row: sqlite3.Row) -> ProjectManagerState:
    return ProjectManagerState(
        manager_id=row["manager_id"],
        session_id=row["session_id"],
        handle=RuntimeHandle.model_validate_json(row["handle_json"]),
        instructions_digest=row["instructions_digest"],
        snapshot_digest=row["snapshot_digest"],
        last_event_sequence=int(row["last_event_sequence"]),
        last_turn_at=_parse_optional(row["last_turn_at"]),
        last_error=row["last_error"],
        version=int(row["version"]),
        created_at=_parse(row["created_at"]),
        updated_at=_parse(row["updated_at"]),
    )


def _run_values(run: PollingRun) -> tuple[Any, ...]:
    return (
        run.run_id,
        run.task_id,
        run.status.value,
        _iso(run.cutoff),
        run.repositories_requested,
        run.repositories_scanned,
        run.repositories_failed,
        run.issues_seen,
        run.candidates_matched,
        run.work_items_queued,
        _iso(run.started_at),
        _iso(run.completed_at) if run.completed_at else None,
        run.error,
    )


def _run_repository_values(item: PollingRunRepository) -> tuple[Any, ...]:
    return (
        item.run_id,
        item.repository_id,
        item.repository,
        item.status.value,
        _iso(item.window_start) if item.window_start else None,
        _iso(item.window_end) if item.window_end else None,
        item.scan_mode,
        item.issues_seen,
        item.candidates_matched,
        item.work_items_queued,
        _iso(item.started_at),
        _iso(item.completed_at) if item.completed_at else None,
        item.error,
    )


def _repository_values(repository: PollingRepository) -> tuple[Any, ...]:
    return (
        repository.repository_id,
        repository.repository,
        int(repository.enabled),
        repository.default_branch,
        repository.html_url,
        _iso(repository.last_scanned_at) if repository.last_scanned_at else None,
        repository.last_status.value if repository.last_status else None,
        repository.last_error,
        _iso(repository.created_at),
        _iso(repository.updated_at),
    )


def _candidate_values(candidate: PollingCandidate) -> tuple[Any, ...]:
    return (
        candidate.candidate_id,
        candidate.repository_id,
        candidate.repository,
        candidate.issue_number,
        candidate.issue_url,
        candidate.title,
        candidate.body,
        _json(candidate.labels),
        candidate.author,
        candidate.comments,
        candidate.evidence_score,
        int(candidate.eligible),
        _json(candidate.filter_reasons),
        candidate_snapshot_digest(candidate),
        _iso(candidate.created_at),
        _iso(candidate.updated_at),
        _iso(candidate.first_seen_at),
        _iso(candidate.last_seen_at),
        candidate.latest_run_id,
    )


def _screening_values(screening: IssueScreening) -> tuple[Any, ...]:
    return (
        screening.candidate_id,
        screening.candidate_snapshot_digest,
        screening.decision.value,
        screening.machine_compatibility.value,
        screening.task_kind.value,
        screening.reason,
        screening.required_environment.model_dump_json(),
        _json(screening.evidence),
        _json(screening.uncertainties),
        screening.model_provider,
        screening.model,
        screening.session_id,
        screening.profile_digest,
        screening.soul_digest,
        screening.agent_digest,
        _iso(screening.screened_at),
    )


def _screening_current_values(screening: IssueScreening) -> tuple[Any, ...]:
    return (
        screening.candidate_id,
        screening.candidate_snapshot_digest,
        screening.revision,
        screening.decision.value,
        screening.machine_compatibility.value,
        screening.task_kind.value,
        screening.reason,
        screening.required_environment.model_dump_json(),
        _json(screening.evidence),
        _json(screening.uncertainties),
        screening.model_provider,
        screening.model,
        screening.session_id,
        screening.profile_digest,
        screening.soul_digest,
        screening.agent_digest,
        screening.screening_trigger,
        screening.requested_by,
        screening.request_reason,
        _json(screening.probe_evidence),
        _iso(screening.screened_at),
    )


def _insert_initial_screening_projection(
    connection: sqlite3.Connection,
    screening: IssueScreening,
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO issue_screening_current (
            candidate_id, candidate_snapshot_digest, revision, decision,
            machine_compatibility, task_kind, reason,
            required_environment_json, evidence_json, uncertainties_json,
            model_provider, model, session_id, profile_digest, soul_digest,
            agent_digest, screening_trigger, requested_by, request_reason,
            probe_evidence_json, screened_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        _screening_current_values(screening),
    )


def _replace_current_screening_projection(
    connection: sqlite3.Connection,
    screening: IssueScreening,
) -> None:
    connection.execute(
        """
        INSERT INTO issue_screening_current (
            candidate_id, candidate_snapshot_digest, revision, decision,
            machine_compatibility, task_kind, reason,
            required_environment_json, evidence_json, uncertainties_json,
            model_provider, model, session_id, profile_digest, soul_digest,
            agent_digest, screening_trigger, requested_by, request_reason,
            probe_evidence_json, screened_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(candidate_id, candidate_snapshot_digest) DO UPDATE SET
            revision = excluded.revision,
            decision = excluded.decision,
            machine_compatibility = excluded.machine_compatibility,
            task_kind = excluded.task_kind,
            reason = excluded.reason,
            required_environment_json = excluded.required_environment_json,
            evidence_json = excluded.evidence_json,
            uncertainties_json = excluded.uncertainties_json,
            model_provider = excluded.model_provider,
            model = excluded.model,
            session_id = excluded.session_id,
            profile_digest = excluded.profile_digest,
            soul_digest = excluded.soul_digest,
            agent_digest = excluded.agent_digest,
            screening_trigger = excluded.screening_trigger,
            requested_by = excluded.requested_by,
            request_reason = excluded.request_reason,
            probe_evidence_json = excluded.probe_evidence_json,
            screened_at = excluded.screened_at
        """,
        _screening_current_values(screening),
    )


def _manager_values(state: ProjectManagerState) -> tuple[Any, ...]:
    return (
        state.manager_id,
        state.session_id,
        state.handle.model_dump_json(),
        state.instructions_digest,
        state.snapshot_digest,
        state.last_event_sequence,
        _iso(state.last_turn_at) if state.last_turn_at else None,
        redact_text(state.last_error) if state.last_error else None,
        state.version,
        _iso(state.created_at),
        _iso(state.updated_at),
    )


def _candidate_filters(
    *,
    repository_id: int | None,
    repository: str | None,
    eligible: bool | None,
    run_id: str | None,
    search: str | None,
    work_statuses: Sequence[WorkStatus] | None,
    screening_decisions: Sequence[ScreeningDecision | Literal["PENDING"]] | None,
) -> tuple[list[str], list[Any]]:
    clauses = ["1 = 1"]
    values: list[Any] = []
    if repository_id is not None:
        clauses.append("repository_id = ?")
        values.append(repository_id)
    if repository:
        clauses.append("repository LIKE ? COLLATE NOCASE")
        values.append(f"%{repository}%")
    if eligible is not None:
        clauses.append("eligible = ?")
        values.append(int(eligible))
    if run_id:
        clauses.append("latest_run_id = ?")
        values.append(run_id)
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        clauses.append(
            "(repository LIKE ? COLLATE NOCASE "
            "OR title LIKE ? COLLATE NOCASE "
            "OR CAST(issue_number AS TEXT) LIKE ?)"
        )
        values.extend((pattern, pattern, pattern))
    if work_statuses:
        clauses.append(
            "EXISTS (SELECT 1 FROM work_items issue_work "
            "WHERE issue_work.candidate_id = polling_candidates.candidate_id "
            "AND issue_work.status IN (" + ",".join("?" for _ in work_statuses) + "))"
        )
        values.extend(status.value for status in work_statuses)
    if screening_decisions:
        requested = {
            value.value if isinstance(value, ScreeningDecision) else value
            for value in screening_decisions
        }
        pending = "PENDING" in requested
        decided = sorted(requested - {"PENDING"})
        decision_clause = (
            "EXISTS (SELECT 1 FROM issue_screening_current issue_screening "
            "WHERE issue_screening.candidate_id = "
            "polling_candidates.candidate_id AND "
            "issue_screening.candidate_snapshot_digest = "
            "polling_candidates.snapshot_digest"
            + (
                " AND issue_screening.decision IN ("
                + ",".join("?" for _ in decided)
                + ")"
                if decided
                else ""
            )
            + ")"
        )
        pending_clause = (
            "NOT EXISTS (SELECT 1 FROM issue_screening_current issue_screening "
            "WHERE issue_screening.candidate_id = "
            "polling_candidates.candidate_id AND "
            "issue_screening.candidate_snapshot_digest = "
            "polling_candidates.snapshot_digest)"
        )
        if pending and decided:
            clauses.append(f"({pending_clause} OR {decision_clause})")
        elif pending:
            clauses.append(pending_clause)
        else:
            clauses.append(decision_clause)
        values.extend(decided)
    return clauses, values


def _current_screening_tx(
    connection: sqlite3.Connection,
    candidate_id_value: str,
    snapshot_digest: str,
) -> IssueScreening | None:
    row = connection.execute(
        "SELECT * FROM issue_screening_current WHERE candidate_id = ? "
        "AND candidate_snapshot_digest = ?",
        (candidate_id_value, snapshot_digest),
    ).fetchone()
    return _screening_from_row(row) if row is not None else None


def _screening_allows_admission(
    screening: IssueScreening | None,
) -> bool:
    """Require a current SELECT with no unresolved external dependency."""

    return bool(
        screening is not None
        and screening.decision is ScreeningDecision.SELECT
        and not screening.required_environment.external_dependencies
    )


def _validate_page(limit: int, offset: int, *, maximum: int) -> None:
    if limit < 1 or limit > maximum or offset < 0:
        raise ValueError("pagination is out of range")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return _utc(parsed)


def _parse_optional(value: str | None) -> datetime | None:
    return _parse(value) if value else None


def _json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


__all__ = [
    "EnvironmentStatus",
    "IssueScreening",
    "MachineCompatibility",
    "PollingCandidate",
    "PollingRepository",
    "PollingRun",
    "PollingRunRepository",
    "PollingRunStatus",
    "PollingTask",
    "PollingTaskStatus",
    "ProjectManagerState",
    "RepositoryScanStatus",
    "ScreeningDecision",
    "ScreeningEnvironmentRequirements",
    "ScreeningTaskKind",
    "SqlitePollingStore",
    "TERMINAL_WORK_STATUSES",
    "WorkEvent",
    "WorkItem",
    "WorkPlan",
    "WorkResourceRequirements",
    "WorkStatus",
    "candidate_id",
    "candidate_snapshot_digest",
    "open_polling_store",
    "work_item_id",
]
