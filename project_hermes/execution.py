"""Controller-owned execution admission and reconciliation."""

from __future__ import annotations

import sqlite3
import re
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterator, Literal, Protocol
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from project_hermes.accounting import (
    AccountingStore,
    build_execution_accounting_record,
)
from project_hermes.config import assert_private_directory
from project_hermes.models import StrictModel, utc_now
from project_hermes.redaction import redact_data, redact_text
from project_hermes.resource_managers import GpuPool
from project_hermes.resources import (
    ArtifactManifest,
    ExecutionEnvironment,
    GpuLease,
    GpuRequest,
)

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CANDIDATE_RE = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ExecutionStatus(StrEnum):
    """Controller-visible execution lifecycle."""

    QUEUED = "QUEUED"
    WAITING_ARTIFACT = "WAITING_ARTIFACT"
    WAITING_RESOURCE = "WAITING_RESOURCE"
    ADMITTING = "ADMITTING"
    RUNNING = "RUNNING"
    TERMINATING = "TERMINATING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DefinitiveExecutionStartError(RuntimeError):
    """External admission failed after absence was authoritatively confirmed."""


class ExecutionRequest(StrictModel):
    """An execution shape requested without a predicted duration."""

    schema_version: Literal["execution-request.v1"] = "execution-request.v1"
    request_id: str
    task_id: str
    run_id: str | None = None
    node_id: str
    repository: str
    workspace_lease_id: str
    candidate_digest: str
    command: list[str] = Field(min_length=1)
    environment: ExecutionEnvironment
    image_repository: str | None = None
    image_digest: str
    artifact_request_ids: list[str] = Field(default_factory=list)
    artifact_byte_limit: int | None = Field(default=None, ge=1)
    gpu: GpuRequest | None = None
    timeout_seconds: int = Field(default=1800, ge=1, le=86_400)
    network_access: bool = False
    idempotency_key: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("command")
    @classmethod
    def validate_command(cls, values: list[str]) -> list[str]:
        if any(not value or "\x00" in value for value in values):
            raise ValueError("command arguments must be non-empty and NUL-free")
        return values

    @field_validator("image_repository")
    @classmethod
    def validate_image_repository(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not value
            or len(value) > 255
            or value != value.casefold()
            or any(ord(character) < 33 for character in value)
            or "://" in value
            or "@" in value
        ):
            raise ValueError(
                "image_repository must be a lowercase OCI repository "
                "without a scheme, tag, or digest"
            )
        last_component = value.rsplit("/", 1)[-1]
        if ":" in last_component:
            raise ValueError(
                "image_repository must not include an image tag"
            )
        return value

    @field_validator("artifact_request_ids")
    @classmethod
    def unique_artifacts(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @field_validator("image_digest")
    @classmethod
    def validate_image_digest(cls, value: str) -> str:
        if not _DIGEST_RE.fullmatch(value):
            raise ValueError("image_digest must be a lowercase sha256 digest")
        return value

    @field_validator("candidate_digest")
    @classmethod
    def validate_candidate_digest(cls, value: str) -> str:
        if not _CANDIDATE_RE.fullmatch(value):
            raise ValueError(
                "candidate_digest must be a lowercase SHA-256 hex digest"
            )
        return value

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if not _REPOSITORY_RE.fullmatch(value):
            raise ValueError("repository must use owner/name form")
        return value

    @model_validator(mode="after")
    def gpu_belongs_to_execution_task(self) -> "ExecutionRequest":
        if self.gpu is not None and self.gpu.task_id != self.task_id:
            raise ValueError("GPU request belongs to a different task")
        if self.gpu is not None and self.gpu.image_digest not in {
            None,
            self.image_digest,
        }:
            raise ValueError(
                "GPU and execution image digests must describe one image"
            )
        return self


class BackendExecution(StrictModel):
    """Immutable identity returned after backend admission."""

    execution_id: str
    backend: str
    native_id: str
    submitted_at: datetime = Field(default_factory=utc_now)


class ExecutionObservation(StrictModel):
    """One authoritative observation of an external execution object."""

    execution_id: str
    status: ExecutionStatus
    terminated: bool = False
    exit_code: int | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    log_refs: list[str] = Field(default_factory=list)
    environment: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    observed_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def terminal_observation_has_termination_fact(
        self,
    ) -> "ExecutionObservation":
        terminal = {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }
        if self.status in terminal and not self.terminated:
            raise ValueError(
                "terminal execution observations require termination proof"
            )
        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("execution completion cannot precede its start")
        return self


class ExecutionRecord(StrictModel):
    """Durable execution projection."""

    schema_version: Literal["execution-record.v1"] = "execution-record.v1"
    execution_id: str
    request: ExecutionRequest
    status: ExecutionStatus = ExecutionStatus.QUEUED
    backend_execution: BackendExecution | None = None
    gpu_lease: GpuLease | None = None
    artifact_manifests: list[ArtifactManifest] = Field(default_factory=list)
    observation: ExecutionObservation | None = None
    blocker: str | None = None
    version: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=utc_now)


class ExecutionBackend(Protocol):
    """Backend controlled by the outer loop."""

    name: str

    def start(
        self,
        execution_id: str,
        request: ExecutionRequest,
        *,
        gpu_ids: tuple[str, ...],
        artifacts: tuple[ArtifactManifest, ...],
    ) -> BackendExecution:
        """Create the external execution object."""

    def observe(
        self,
        execution: BackendExecution,
    ) -> ExecutionObservation:
        """Read authoritative job or pod state."""

    def cancel(self, execution: BackendExecution) -> None:
        """Request deletion or cancellation without claiming termination."""


class ExecutionStore(Protocol):
    """Durable execution persistence port."""

    def create(self, record: ExecutionRecord) -> ExecutionRecord:
        """Create an idempotent execution record."""

    def get(self, execution_id: str) -> ExecutionRecord:
        """Read one execution."""

    def update(
        self,
        record: ExecutionRecord,
        *,
        expected_version: int,
    ) -> ExecutionRecord:
        """Compare-and-set one execution projection."""

    def list_records(
        self,
        *,
        statuses: set[ExecutionStatus] | None = None,
    ) -> list[ExecutionRecord]:
        """List durable executions in deterministic request order."""

    def reserve_capacity(
        self,
        execution_id: str,
        *,
        max_jobs: int,
        max_gpus: int,
    ) -> ExecutionRecord:
        """Atomically reserve one global Job/GPU admission slot."""


class SqliteExecutionStore:
    """Local durable execution projection."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS project_hermes_executions (
        execution_id    TEXT PRIMARY KEY,
        request_id      TEXT NOT NULL UNIQUE,
        task_id         TEXT NOT NULL,
        node_id         TEXT NOT NULL,
        status          TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        version         INTEGER NOT NULL,
        record_json     TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        UNIQUE(task_id, idempotency_key)
    );
    CREATE INDEX IF NOT EXISTS idx_project_hermes_executions_status
    ON project_hermes_executions(status, updated_at);
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        assert_private_directory(self.path.parent)
        self._lock = RLock()
        with self._connect() as connection:
            connection.executescript(self._SCHEMA)

    def create(self, record: ExecutionRecord) -> ExecutionRecord:
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT record_json FROM project_hermes_executions
                WHERE task_id = ? AND idempotency_key = ?
                """,
                (
                    record.request.task_id,
                    record.request.idempotency_key,
                ),
            ).fetchone()
            if existing is not None:
                restored = ExecutionRecord.model_validate_json(
                    existing["record_json"]
                )
                if restored.request != record.request:
                    raise ValueError(
                        "execution idempotency key was reused with "
                        "different input"
                    )
                return restored
            connection.execute(
                """
                INSERT INTO project_hermes_executions (
                    execution_id, request_id, task_id, node_id, status,
                    idempotency_key, version, record_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.execution_id,
                    record.request.request_id,
                    record.request.task_id,
                    record.request.node_id,
                    record.status.value,
                    record.request.idempotency_key,
                    record.version,
                    record.model_dump_json(),
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def get(self, execution_id: str) -> ExecutionRecord:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT record_json FROM project_hermes_executions
                WHERE execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown execution: {execution_id}")
        return ExecutionRecord.model_validate_json(row["record_json"])

    def update(
        self,
        record: ExecutionRecord,
        *,
        expected_version: int,
    ) -> ExecutionRecord:
        updated = record.model_copy(
            update={
                "version": expected_version + 1,
                "updated_at": utc_now(),
            }
        )
        updated = ExecutionRecord.model_validate(updated.model_dump())
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE project_hermes_executions
                SET status = ?, version = ?, record_json = ?, updated_at = ?
                WHERE execution_id = ? AND version = ?
                """,
                (
                    updated.status.value,
                    updated.version,
                    updated.model_dump_json(),
                    updated.updated_at.isoformat(),
                    updated.execution_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("execution changed concurrently")
        return updated

    def list_records(
        self,
        *,
        statuses: set[ExecutionStatus] | None = None,
    ) -> list[ExecutionRecord]:
        query = """
            SELECT record_json FROM project_hermes_executions
        """
        parameters: tuple[Any, ...] = ()
        if statuses is not None:
            if not statuses:
                return []
            placeholders = ", ".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            parameters = tuple(
                status.value for status in sorted(statuses, key=str)
            )
        query += " ORDER BY updated_at, execution_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            ExecutionRecord.model_validate_json(row["record_json"])
            for row in rows
        ]

    def reserve_capacity(
        self,
        execution_id: str,
        *,
        max_jobs: int,
        max_gpus: int,
    ) -> ExecutionRecord:
        """Reserve global capacity before creating an external Job."""

        if max_jobs < 1:
            raise ValueError("max_jobs must be positive")
        if max_gpus < 0:
            raise ValueError("max_gpus cannot be negative")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT record_json FROM project_hermes_executions
                WHERE execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown execution: {execution_id}")
            record = ExecutionRecord.model_validate_json(row["record_json"])
            if (
                record.backend_execution is not None
                or record.status is ExecutionStatus.ADMITTING
                or record.status
                in {
                    ExecutionStatus.SUCCEEDED,
                    ExecutionStatus.FAILED,
                    ExecutionStatus.CANCELLED,
                }
            ):
                return record
            if record.status not in {
                ExecutionStatus.QUEUED,
                ExecutionStatus.WAITING_RESOURCE,
                ExecutionStatus.WAITING_ARTIFACT,
            }:
                return record

            active_rows = connection.execute(
                """
                SELECT record_json FROM project_hermes_executions
                WHERE status IN (?, ?, ?)
                """,
                (
                    ExecutionStatus.ADMITTING.value,
                    ExecutionStatus.RUNNING.value,
                    ExecutionStatus.TERMINATING.value,
                ),
            ).fetchall()
            active = [
                ExecutionRecord.model_validate_json(item["record_json"])
                for item in active_rows
                if item["record_json"]
            ]
            requested_gpus = (
                record.request.gpu.count
                if record.request.gpu is not None
                else 0
            )
            active_gpus = sum(
                item.request.gpu.count
                for item in active
                if item.request.gpu is not None
            )
            blockers: list[str] = []
            if len(active) >= max_jobs:
                blockers.append(f"global Job limit ({max_jobs}) is in use")
            if active_gpus + requested_gpus > max_gpus:
                blockers.append(f"global GPU limit ({max_gpus}) is in use")
            target = (
                ExecutionStatus.WAITING_RESOURCE
                if blockers
                else ExecutionStatus.ADMITTING
            )
            reserved = record.model_copy(
                update={
                    "status": target,
                    "blocker": "; ".join(blockers) if blockers else None,
                }
            )
            return self._update_tx(
                connection,
                ExecutionRecord.model_validate(reserved.model_dump()),
                expected_version=record.version,
            )

    @staticmethod
    def _update_tx(
        connection: sqlite3.Connection,
        record: ExecutionRecord,
        *,
        expected_version: int,
    ) -> ExecutionRecord:
        updated = record.model_copy(
            update={
                "version": expected_version + 1,
                "updated_at": utc_now(),
            }
        )
        updated = ExecutionRecord.model_validate(updated.model_dump())
        cursor = connection.execute(
            """
            UPDATE project_hermes_executions
            SET status = ?, version = ?, record_json = ?, updated_at = ?
            WHERE execution_id = ? AND version = ?
            """,
            (
                updated.status.value,
                updated.version,
                updated.model_dump_json(),
                updated.updated_at.isoformat(),
                updated.execution_id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("execution changed concurrently")
        return updated

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


class ExecutionCoordinator:
    """Admit work only after artifacts and GPUs are ready."""

    def __init__(
        self,
        store: ExecutionStore,
        backend: ExecutionBackend,
        *,
        artifact_lookup: Callable[[str], ArtifactManifest | None],
        gpu_pool: GpuPool | None = None,
        accounting: AccountingStore | None = None,
        max_jobs: int = 8,
        max_gpus: int = 6,
    ) -> None:
        if max_jobs < 1:
            raise ValueError("max_jobs must be positive")
        if max_gpus < 0:
            raise ValueError("max_gpus cannot be negative")
        self.store = store
        self.backend = backend
        self.artifact_lookup = artifact_lookup
        self.gpu_pool = gpu_pool
        self.accounting = accounting
        self.max_jobs = max_jobs
        self.max_gpus = max_gpus
        self._lock = RLock()
        if self.gpu_pool is not None:
            for record in self.store.list_records(
                statuses={
                    ExecutionStatus.ADMITTING,
                    ExecutionStatus.RUNNING,
                    ExecutionStatus.TERMINATING,
                }
            ):
                if record.gpu_lease is not None:
                    self.gpu_pool.restore(record.gpu_lease)

    def submit(self, request: ExecutionRequest) -> ExecutionRecord:
        record = self.store.create(
            ExecutionRecord(
                execution_id=f"execution-{uuid4().hex}",
                request=request,
            )
        )
        if record.status is not ExecutionStatus.QUEUED:
            return record
        return self.reconcile(record.execution_id)

    def reconcile(self, execution_id: str) -> ExecutionRecord:
        with self._lock:
            record = self.store.get(execution_id)
            if record.status in {
                ExecutionStatus.SUCCEEDED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            }:
                return record
            if record.backend_execution is None:
                return self._admit(record)

            observation = self.backend.observe(record.backend_execution)
            if observation.execution_id != record.execution_id:
                raise ValueError(
                    "backend observation belongs to another execution"
                )
            observation = ExecutionObservation.model_validate(
                redact_data(observation.model_dump(mode="json"))
            )
            status = observation.status
            updated = record.model_copy(
                update={
                    "status": status,
                    "observation": observation,
                    "blocker": None,
                }
            )
            updated = self.store.update(
                ExecutionRecord.model_validate(updated.model_dump()),
                expected_version=record.version,
            )
            if observation.terminated and updated.gpu_lease is not None:
                if self.gpu_pool is None:
                    raise RuntimeError(
                        "execution has a GPU lease but no configured pool"
                    )
                released = self.gpu_pool.release(
                    updated.gpu_lease.lease_id,
                    execution_terminated=True,
                )
                updated = self.store.update(
                    updated.model_copy(update={"gpu_lease": released}),
                    expected_version=updated.version,
                )
            if observation.terminated:
                self._record_execution_accounting(updated)
            if (
                observation.terminated
                and observation.result.get("archive_verified") is True
            ):
                delete_verified = getattr(
                    self.backend,
                    "delete_verified",
                    None,
                )
                if not callable(delete_verified):
                    raise RuntimeError(
                        "archive-aware backend lacks verified cleanup"
                    )
                try:
                    delete_verified(record.backend_execution)
                except Exception:
                    # The terminal observation and verified archive identity are
                    # already durable. Retain the Job for the next operator or
                    # cleanup pass rather than losing candidate ingestion.
                    pass
            return updated

    def reconcile_all(self) -> list[ExecutionRecord]:
        """Reconcile every non-terminal execution, including old Jobs."""

        statuses = set(ExecutionStatus) - {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }
        return [
            self.reconcile(record.execution_id)
            for record in self.store.list_records(statuses=statuses)
        ]

    def cancel(self, execution_id: str) -> ExecutionRecord:
        with self._lock:
            record = self.store.get(execution_id)
            if record.backend_execution is None:
                cancelled = record.model_copy(
                    update={"status": ExecutionStatus.CANCELLED}
                )
                return self.store.update(
                    ExecutionRecord.model_validate(cancelled.model_dump()),
                    expected_version=record.version,
                )
            self.backend.cancel(record.backend_execution)
            terminating = record.model_copy(
                update={"status": ExecutionStatus.TERMINATING}
            )
            return self.store.update(
                ExecutionRecord.model_validate(terminating.model_dump()),
                expected_version=record.version,
            )

    def _admit(self, record: ExecutionRecord) -> ExecutionRecord:
        manifests: list[ArtifactManifest] = []
        missing: list[str] = []
        for request_id in record.request.artifact_request_ids:
            manifest = self.artifact_lookup(request_id)
            if manifest is None:
                missing.append(request_id)
            else:
                if manifest.task_id != record.request.task_id:
                    raise ValueError(
                        "artifact manifest belongs to another task"
                    )
                manifests.append(manifest)
        if missing:
            waiting = record.model_copy(
                update={
                    "status": ExecutionStatus.WAITING_ARTIFACT,
                    "blocker": "artifacts are not ready: "
                    + ", ".join(sorted(missing)),
                }
            )
            return self.store.update(
                ExecutionRecord.model_validate(waiting.model_dump()),
                expected_version=record.version,
            )
        if (
            record.request.artifact_byte_limit is not None
            and sum(manifest.byte_size for manifest in manifests)
            > record.request.artifact_byte_limit
        ):
            failed = record.model_copy(
                update={
                    "status": ExecutionStatus.FAILED,
                    "blocker": (
                        "verified artifacts exceed the locked byte limit"
                    ),
                }
            )
            return self.store.update(
                ExecutionRecord.model_validate(failed.model_dump()),
                expected_version=record.version,
            )

        record = self.store.reserve_capacity(
            record.execution_id,
            max_jobs=self.max_jobs,
            max_gpus=self.max_gpus,
        )
        if record.status is not ExecutionStatus.ADMITTING:
            return record

        lease: GpuLease | None = None
        if record.request.gpu is not None:
            if self.gpu_pool is None:
                raise RuntimeError("GPU execution requires a configured pool")
            lease = record.gpu_lease
            if lease is not None:
                self.gpu_pool.restore(lease)
            else:
                lease = self.gpu_pool.allocate(
                    record.request.gpu,
                    execution_id=record.execution_id,
                )
            if lease is None:
                waiting = record.model_copy(
                    update={
                        "status": ExecutionStatus.WAITING_RESOURCE,
                        "blocker": "requested GPU shape is unavailable",
                    }
                )
                return self.store.update(
                    ExecutionRecord.model_validate(waiting.model_dump()),
                    expected_version=record.version,
                )

        if (
            record.gpu_lease != lease
            or record.artifact_manifests != manifests
        ):
            prepared = record.model_copy(
                update={
                    "gpu_lease": lease,
                    "artifact_manifests": manifests,
                }
            )
            record = self.store.update(
                ExecutionRecord.model_validate(prepared.model_dump()),
                expected_version=record.version,
            )

        try:
            backend_execution = self.backend.start(
                record.execution_id,
                record.request,
                gpu_ids=tuple(lease.gpu_ids) if lease else (),
                artifacts=tuple(manifests),
            )
        except (
            DefinitiveExecutionStartError,
            PermissionError,
            ValueError,
        ) as exc:
            released = lease
            if lease is not None and self.gpu_pool is not None:
                released = self.gpu_pool.release(
                    lease.lease_id,
                    execution_terminated=True,
                )
            failed = record.model_copy(
                update={
                    "status": ExecutionStatus.FAILED,
                    "gpu_lease": released,
                    "blocker": (
                        redact_text(str(exc))
                        or "external execution admission failed"
                    ),
                }
            )
            return self.store.update(
                ExecutionRecord.model_validate(failed.model_dump()),
                expected_version=record.version,
            )
        except Exception:
            uncertain = record.model_copy(
                update={
                    "status": ExecutionStatus.ADMITTING,
                    "blocker": (
                        "external execution admission requires "
                        "deterministic reconciliation"
                    ),
                }
            )
            return self.store.update(
                ExecutionRecord.model_validate(uncertain.model_dump()),
                expected_version=record.version,
            )
        if backend_execution.execution_id != record.execution_id:
            if lease is not None and self.gpu_pool is not None:
                self.gpu_pool.release(
                    lease.lease_id,
                    execution_terminated=True,
                )
            raise ValueError(
                "backend execution identity does not match controller record"
            )
        running = record.model_copy(
            update={
                "status": ExecutionStatus.RUNNING,
                "backend_execution": backend_execution,
                "gpu_lease": lease,
                "artifact_manifests": manifests,
                "blocker": None,
            }
        )
        return self.store.update(
            ExecutionRecord.model_validate(running.model_dump()),
            expected_version=record.version,
        )

    def _record_execution_accounting(self, record: ExecutionRecord) -> None:
        if (
            self.accounting is None
            or record.request.run_id is None
            or record.observation is None
            or record.observation.started_at is None
            or record.observation.completed_at is None
        ):
            return
        self.accounting.append(
            build_execution_accounting_record(
                task_id=record.request.task_id,
                run_id=record.request.run_id,
                node_id=record.request.node_id,
                execution_id=record.execution_id,
                outcome=record.status.value,
                started_at=record.observation.started_at,
                completed_at=record.observation.completed_at,
            )
        )
