"""Verified worker archive to internal pull-request candidate ingestion."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from project_hermes.execution import (
    ExecutionRecord,
    ExecutionStatus,
    ExecutionStore,
)
from project_hermes.kubernetes_jobs import CodexWorkerMetadata
from project_hermes.models import StrictModel
from project_hermes.publication import (
    InternalCandidateAccounting,
    InternalCandidateAccountingRound,
    InternalCandidateCheckConclusion,
    InternalCandidateCheckStatus,
    InternalCandidateLockState,
    InternalPullRequestCandidate,
    InternalPullRequestCandidateStore,
    InternalPullRequestCheck,
    InternalPullRequestCommit,
    InternalPullRequestFile,
)
from project_hermes.redaction import redact_data, redact_text
from project_hermes.store import RunStore
from project_hermes.supply_chain import (
    WorkerArtifactArchive,
    WorkerArtifactEntry,
    verify_worker_artifact_archive,
)
from project_hermes.work_graph import LifecycleStatus, TERMINAL_STATUSES

_SHA256_RE = "sha256:"
_MAX_RESULT_BYTES = 4 * 1024 * 1024
_MAX_USAGE_BYTES = 1024 * 1024
_PROVIDER_CAPACITY_EXHAUSTED = "provider_capacity_exhausted"


class WorkerReportedCheck(StrictModel):
    """One worker-reported check retained as unapproved candidate evidence."""

    name: str = Field(min_length=1)
    passed: bool
    summary: str = ""
    validation_overlay_digest: str | None = None

    @field_validator("validation_overlay_digest")
    @classmethod
    def validate_overlay_digest(cls, value: str | None) -> str | None:
        if value is not None and not _is_sha256(value):
            raise ValueError(
                "validation_overlay_digest must be lowercase SHA-256"
            )
        return value


class WorkerCandidatePayload(StrictModel):
    """Strict candidate result written by the isolated Codex worker."""

    schema_version: Literal["project-hermes-worker-candidate.v1"] = (
        "project-hermes-worker-candidate.v1"
    )
    execution_id: str
    task_id: str
    repository: str
    source_candidate_digest: str
    base_ref: str = Field(min_length=1)
    base_sha: str
    head_ref: str = Field(
        min_length=1,
        pattern=r"^project-hermes/",
        description="Controller-scoped candidate ref beginning with project-hermes/.",
    )
    head_sha: str
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    commits: list[InternalPullRequestCommit] = Field(min_length=1)
    files: list[InternalPullRequestFile] = Field(
        min_length=1,
        description=(
            "Production-scope files only. Exclude local test overlays, including "
            "test/ and tests/ paths, *_test.py, .spec.ts, and .test.ts files."
        ),
    )
    checks: list[WorkerReportedCheck] = Field(default_factory=list)

    @field_validator("source_candidate_digest")
    @classmethod
    def validate_source_candidate_digest(cls, value: str) -> str:
        if (
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(
                "source_candidate_digest must be lowercase SHA-256 hex"
            )
        return value

    @model_validator(mode="after")
    def validate_candidate_identity(self) -> "WorkerCandidatePayload":
        if self.commits[-1].sha != self.head_sha:
            raise ValueError("last candidate commit must match head_sha")
        if self.base_sha == self.head_sha:
            raise ValueError("candidate head must differ from its baseline")
        if not self.head_ref.startswith("project-hermes/"):
            raise ValueError("candidate head_ref must be controller-scoped")
        if any(_is_test_path(item.path) for item in self.files):
            raise ValueError(
                "local test overlays cannot enter production candidate files"
            )
        if any(not item.diff and not item.is_binary for item in self.files):
            raise ValueError("candidate files require a diff or binary marker")
        return self


class CandidateIngestResult(StrictModel):
    """Idempotent outcome of reconciling one terminal execution."""

    execution_id: str
    run_id: str
    status: Literal[
        "candidate_saved",
        "execution_failed",
        "archive_rejected",
    ]
    candidate_id: str | None = None
    error: str | None = None


class CandidateIngestor:
    """Create approval-pending candidates only from verified worker archives."""

    def __init__(
        self,
        execution_store: ExecutionStore,
        run_store: RunStore,
        candidate_store: InternalPullRequestCandidateStore,
        *,
        artifact_archive_root: str | Path,
    ) -> None:
        self.execution_store = execution_store
        self.run_store = run_store
        self.candidate_store = candidate_store
        self.artifact_archive_root = Path(artifact_archive_root).resolve()

    def reconcile_all(self) -> list[CandidateIngestResult]:
        """Reconcile every terminal execution without duplicating candidates."""

        records = self.execution_store.list_records(
            statuses={
                ExecutionStatus.SUCCEEDED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            }
        )
        results: list[CandidateIngestResult] = []
        for record in records:
            if record.request.run_id is None:
                continue
            results.append(self._reconcile_record(record))
        return results

    def _reconcile_record(
        self,
        record: ExecutionRecord,
    ) -> CandidateIngestResult:
        run_id = str(record.request.run_id)
        recover_capacity_result = _is_recoverable_capacity_result(record)
        if (
            record.status is not ExecutionStatus.SUCCEEDED
            and not recover_capacity_result
        ):
            self._finish_run(
                record,
                status=(
                    LifecycleStatus.CANCELLED
                    if record.status is ExecutionStatus.CANCELLED
                    else LifecycleStatus.FAILED
                ),
                reason=record.blocker or f"Worker ended {record.status.value}.",
            )
            return CandidateIngestResult(
                execution_id=record.execution_id,
                run_id=run_id,
                status="execution_failed",
                error=record.blocker,
            )
        try:
            candidate = self._candidate_from_verified_archive(
                record,
                recover_capacity_result=recover_capacity_result,
            )
        except Exception as exc:
            error = redact_text(str(exc)) or "worker archive was rejected"
            self._finish_run(
                record,
                status=LifecycleStatus.FAILED,
                reason=error,
            )
            return CandidateIngestResult(
                execution_id=record.execution_id,
                run_id=run_id,
                status="archive_rejected",
                error=error,
            )

        try:
            try:
                stored = self.candidate_store.get(candidate.candidate_id)
            except KeyError:
                stored = self.candidate_store.save(candidate)
            else:
                if (
                    stored.task_id != candidate.task_id
                    or stored.solution_digest() != candidate.solution_digest()
                ):
                    raise ValueError(
                        "content-addressed candidate identity collision"
                    )
        except Exception as exc:
            error = redact_text(str(exc)) or "candidate persistence failed"
            self._finish_run(
                record,
                status=LifecycleStatus.FAILED,
                reason=error,
            )
            return CandidateIngestResult(
                execution_id=record.execution_id,
                run_id=run_id,
                status="archive_rejected",
                error=error,
            )
        self._finish_run(
            record,
            status=LifecycleStatus.COMPLETED,
            reason=(
                f"Internal candidate {stored.candidate_id} was recovered "
                "from a verified result completed before provider capacity "
                "exhaustion."
                if recover_capacity_result
                else (
                    f"Internal candidate {stored.candidate_id} was "
                    "persisted."
                )
            ),
        )
        return CandidateIngestResult(
            execution_id=record.execution_id,
            run_id=run_id,
            status="candidate_saved",
            candidate_id=stored.candidate_id,
        )

    def _candidate_from_verified_archive(
        self,
        record: ExecutionRecord,
        *,
        recover_capacity_result: bool = False,
    ) -> InternalPullRequestCandidate:
        observation = record.observation
        if (
            observation is None
            or not observation.terminated
            or observation.result.get("archive_verified") is not True
        ):
            raise ValueError("execution has no verified worker archive")
        raw_archive = observation.result.get("verified_archive")
        if not isinstance(raw_archive, dict):
            raise ValueError("verified archive identity is missing")
        reported = WorkerArtifactArchive.model_validate(raw_archive)
        worker = CodexWorkerMetadata.model_validate(
            record.request.metadata.get("codex_worker")
        )
        scope = (
            self.artifact_archive_root
            / "execution-scopes"
            / record.execution_id
        )
        archive = verify_worker_artifact_archive(
            scope,
            execution_id=record.execution_id,
            task_id=record.request.task_id,
            release_digest=worker.release_digest,
            image_digest=record.request.image_digest,
            source_bundle_digest=worker.source_bundle_digest,
        )
        expected_outcome = (
            "failed" if recover_capacity_result else "succeeded"
        )
        if archive != reported or archive.outcome != expected_outcome:
            raise ValueError("verified archive projection is inconsistent")

        result_entry = _archive_entry(archive, "result.json")
        payload = WorkerCandidatePayload.model_validate_json(
            _read_entry(scope, result_entry, max_bytes=_MAX_RESULT_BYTES)
        )
        task = self.run_store.get_task(str(record.request.run_id))
        baseline_ref, baseline_sha = _named_baseline(task.named_baseline)
        if (
            payload.execution_id != record.execution_id
            or payload.task_id != task.task_id
            or payload.repository != record.request.repository
            or payload.source_candidate_digest
            != record.request.candidate_digest
            or payload.base_ref != baseline_ref
            or payload.base_sha != baseline_sha
            or payload.base_sha != worker.base_sha
        ):
            raise ValueError("worker candidate identity differs from locked task")
        if payload.repository not in {
            responsibility.repository for responsibility in task.repositories
        }:
            raise ValueError("worker candidate is outside task responsibility")
        payload_data = payload.model_dump(mode="json")
        if redact_data(payload_data) != payload_data:
            raise ValueError("worker candidate contains credential-shaped data")

        usage = _read_usage(scope, archive)
        evidence_id = (
            f"artifact://worker/{record.execution_id}/result.json"
            f"#{result_entry.digest}"
        )
        checks = [
            InternalPullRequestCheck(
                name="isolated worker archive",
                status=InternalCandidateCheckStatus.COMPLETED,
                conclusion=InternalCandidateCheckConclusion.SUCCESS,
                summary=(
                    "Worker archive identity and all core artifact digests "
                    "were verified."
                ),
                evidence_ids=[evidence_id],
                started_at=observation.started_at,
                completed_at=observation.completed_at,
            ),
            *(
                [
                    InternalPullRequestCheck(
                        name="worker final response transport",
                        status=InternalCandidateCheckStatus.COMPLETED,
                        conclusion=InternalCandidateCheckConclusion.NEUTRAL,
                        summary=(
                            "The worker exhausted provider capacity after "
                            "completing its strict result artifact. The "
                            "controller recovered the artifact only after "
                            "verifying its archive and locked identities; "
                            "independent review is still required."
                        ),
                        evidence_ids=[evidence_id],
                        started_at=observation.started_at,
                        completed_at=observation.completed_at,
                    )
                ]
                if recover_capacity_result
                else []
            ),
            InternalPullRequestCheck(
                name="mechanical minimal-diff boundary",
                status=InternalCandidateCheckStatus.COMPLETED,
                conclusion=InternalCandidateCheckConclusion.SUCCESS,
                summary=(
                    "Production files are unique, carry necessity statements, "
                    "exclude local test overlays, and hash to "
                    f"{worker_file_digest(payload.files)}."
                ),
                evidence_ids=[evidence_id],
                started_at=observation.started_at,
                completed_at=observation.completed_at,
            ),
            *[
                InternalPullRequestCheck(
                    name=check.name,
                    status=InternalCandidateCheckStatus.COMPLETED,
                    conclusion=(
                        InternalCandidateCheckConclusion.SUCCESS
                        if check.passed
                        else InternalCandidateCheckConclusion.FAILURE
                    ),
                    summary=check.summary,
                    evidence_ids=[evidence_id],
                    validation_overlay_digest=(
                        check.validation_overlay_digest
                    ),
                    started_at=observation.started_at,
                    completed_at=observation.completed_at,
                )
                for check in payload.checks
            ],
        ]
        completed_at = observation.completed_at or observation.observed_at
        duration_ms = observation.duration_ms or 0
        accounting = InternalCandidateAccounting(
            wall_clock_duration_ms=duration_ms,
            active_duration_ms=duration_ms,
            estimated_llm_cost_usd=float(
                usage.get("estimated_cost_usd") or 0
            ),
            cost_complete=bool(usage.get("cost_complete", False)),
            rounds=[
                InternalCandidateAccountingRound(
                    round_id=f"worker-{record.execution_id}",
                    kind="isolated_worker",
                    role="codex",
                    outcome=archive.outcome,
                    duration_ms=duration_ms,
                    input_tokens=_nonnegative_int(
                        usage.get("input_tokens")
                    ),
                    output_tokens=_nonnegative_int(
                        usage.get("output_tokens")
                    ),
                    estimated_cost_usd=(
                        float(usage["estimated_cost_usd"])
                        if usage.get("estimated_cost_usd") is not None
                        else None
                    ),
                )
            ],
        )
        candidate_id = (
            "internal-"
            + result_entry.digest.removeprefix(_SHA256_RE)[:48]
        )
        return InternalPullRequestCandidate(
            candidate_id=candidate_id,
            task_id=payload.task_id,
            title=payload.title,
            body=payload.body,
            repository=payload.repository,
            base_ref=payload.base_ref,
            base_sha=payload.base_sha,
            head_ref=payload.head_ref,
            head_sha=payload.head_sha,
            commits=payload.commits,
            files=payload.files,
            checks=checks,
            accounting=accounting,
            lock_state=InternalCandidateLockState.APPROVAL_PENDING,
            created_at=completed_at,
            updated_at=completed_at,
        )

    def _finish_run(
        self,
        record: ExecutionRecord,
        *,
        status: LifecycleStatus,
        reason: str,
    ) -> None:
        run_id = str(record.request.run_id)
        run = self.run_store.get_run(run_id)
        if run.status in TERMINAL_STATUSES:
            return
        self.run_store.record_event(
            run_id,
            "worker.candidate_reconciled",
            {
                "execution_id": record.execution_id,
                "status": status.value,
                "reason": redact_text(reason),
            },
            node_id=record.request.node_id,
        )
        self.run_store.complete_run(run_id, status=status)


def _is_recoverable_capacity_result(record: ExecutionRecord) -> bool:
    """Return whether a failed execution may attempt strict result recovery."""

    observation = record.observation
    return (
        record.status is ExecutionStatus.FAILED
        and observation is not None
        and observation.terminated
        and observation.result.get("failure_kind")
        == _PROVIDER_CAPACITY_EXHAUSTED
    )


def worker_file_digest(files: list[InternalPullRequestFile]) -> str:
    """Return the canonical digest required in a worker candidate payload."""

    encoded = json.dumps(
        [item.model_dump(mode="json") for item in files],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _archive_entry(
    archive: WorkerArtifactArchive,
    name: str,
) -> WorkerArtifactEntry:
    try:
        return next(entry for entry in archive.entries if entry.name == name)
    except StopIteration as exc:  # pragma: no cover - archive model enforces set
        raise ValueError(f"worker archive lacks {name}") from exc


def _read_entry(
    scope: Path,
    entry: WorkerArtifactEntry,
    *,
    max_bytes: int,
) -> bytes:
    if entry.byte_size > max_bytes:
        raise ValueError(f"worker artifact exceeds size limit: {entry.name}")
    root = scope.resolve()
    candidate = scope
    for part in Path(entry.object_path).parts:
        candidate /= part
        if candidate.is_symlink():
            raise ValueError(
                f"worker artifact path contains a symlink: {entry.name}"
            )
    if (
        not candidate.is_file()
        or not candidate.resolve().is_relative_to(root)
        or candidate.stat().st_size != entry.byte_size
    ):
        raise ValueError(f"worker artifact is not a confined file: {entry.name}")
    contents = candidate.read_bytes()
    digest = "sha256:" + hashlib.sha256(contents).hexdigest()
    if digest != entry.digest:
        raise ValueError(f"worker artifact digest mismatch: {entry.name}")
    return contents


def _read_usage(
    scope: Path,
    archive: WorkerArtifactArchive,
) -> dict[str, Any]:
    entry = _archive_entry(archive, "usage.json")
    raw = json.loads(
        _read_entry(scope, entry, max_bytes=_MAX_USAGE_BYTES)
    )
    if not isinstance(raw, dict):
        raise ValueError("worker usage artifact must be an object")
    return raw


def _named_baseline(value: str) -> tuple[str, str]:
    branch, separator, sha = value.rpartition("@")
    if (
        not separator
        or not branch
        or len(sha) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in sha)
    ):
        raise ValueError("locked task named_baseline is malformed")
    return branch, sha


def _is_sha256(value: str) -> bool:
    return (
        value.startswith(_SHA256_RE)
        and len(value) == 71
        and all(
            character in "0123456789abcdef"
            for character in value.removeprefix(_SHA256_RE)
        )
    )


def _is_test_path(value: str) -> bool:
    normalized = value.replace("\\", "/").casefold()
    path_segments = normalized.split("/")
    return (
        any(segment in {"test", "tests"} for segment in path_segments)
        or normalized.endswith(("_test.py", ".spec.ts", ".test.ts"))
    )


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


__all__ = [
    "CandidateIngestResult",
    "CandidateIngestor",
    "WorkerCandidatePayload",
    "WorkerReportedCheck",
    "worker_file_digest",
]
