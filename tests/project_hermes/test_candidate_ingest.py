from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from project_hermes.candidate_ingest import (
    CandidateIngestor,
    WorkerCandidatePayload,
    WorkerReportedCheck,
)
from project_hermes.execution import (
    ExecutionObservation,
    ExecutionRecord,
    ExecutionRequest,
    ExecutionStatus,
    SqliteExecutionStore,
)
from project_hermes.kubernetes_jobs import CodexWorkerMetadata
from project_hermes.models import IssueTask
from project_hermes.publication import (
    InternalCandidateCheckConclusion,
    InternalCandidateLockState,
    InternalPullRequestCommit,
    InternalPullRequestFile,
    SqliteInternalPullRequestCandidateStore,
)
from project_hermes.resources import ExecutionEnvironment
from project_hermes.store import SqliteRunStore
from project_hermes.supply_chain import (
    WorkerArtifactArchive,
    WorkerArtifactEntry,
)
from project_hermes.work_graph import LifecycleStatus


NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40
IMAGE_DIGEST = "sha256:" + "3" * 64
RELEASE_DIGEST = "4" * 64
SOURCE_BUNDLE_DIGEST = "sha256:" + "5" * 64
CANDIDATE_DIGEST = "6" * 64
EXECUTION_ID = "execution-1"
RUN_ID = "run-1"


def _payload(
    task: IssueTask,
    *,
    execution_id: str = EXECUTION_ID,
) -> bytes:
    payload = WorkerCandidatePayload(
        execution_id=execution_id,
        task_id=task.task_id,
        repository="acme/kernel",
        source_candidate_digest=CANDIDATE_DIGEST,
        base_ref="main",
        base_sha=BASE_SHA,
        head_ref=f"project-hermes/{task.task_id}",
        head_sha=HEAD_SHA,
        title="Repair the kernel result",
        body="Implements the locked correction and retains local evidence.",
        commits=[
            InternalPullRequestCommit(
                sha=HEAD_SHA,
                message="Fix the kernel boundary",
                author_name="ProjectHermes Worker",
                author_email="worker@project-hermes.invalid",
                authored_at=NOW,
            )
        ],
        files=[
            InternalPullRequestFile(
                path="src/kernel.py",
                status="M",
                added=1,
                removed=1,
                diff="@@ -1 +1 @@\n-old_result\n+new_result\n",
                necessity="Corrects the locked issue's result boundary.",
            )
        ],
        checks=[
            WorkerReportedCheck(
                name="focused validation",
                passed=True,
                summary="The focused regression passed locally.",
            )
        ],
    )
    return payload.model_dump_json().encode("utf-8")


def test_worker_candidate_accepts_production_testing_directory(
    issue_task: IssueTask,
) -> None:
    payload = json.loads(_payload(issue_task))
    payload["files"][0]["path"] = "torch/csrc/jit/testing/file_check.cpp"

    candidate = WorkerCandidatePayload.model_validate(payload)

    assert candidate.files[0].path == "torch/csrc/jit/testing/file_check.cpp"


@pytest.mark.parametrize(
    "path",
    [
        "test/test_kernel.py",
        "src/tests/test_kernel.py",
        "src\\test\\test_kernel.py",
        "src/kernel_test.py",
    ],
)
def test_worker_candidate_rejects_local_test_overlay(
    issue_task: IssueTask,
    path: str,
) -> None:
    payload = json.loads(_payload(issue_task))
    payload["files"][0]["path"] = path

    with pytest.raises(
        ValueError,
        match="local test overlays cannot enter production candidate files",
    ):
        WorkerCandidatePayload.model_validate(payload)


def _write_archive(
    archive_root: Path,
    task: IssueTask,
    *,
    result: bytes,
    outcome: str,
) -> WorkerArtifactArchive:
    scope = archive_root / "execution-scopes" / EXECUTION_ID
    artifacts = {
        "stdout.log": (
            b'{"type":"turn.completed"}\n'
            if outcome == "succeeded"
            else (
                b'{"type":"turn.failed","error":{"message":'
                b'"429 Too Many Requests"}}\n'
            )
        ),
        "stderr.log": b"worker transport ended\n",
        "result.json": result,
        "usage.json": json.dumps(
            {
                "input_tokens": 1200,
                "output_tokens": 300,
                "estimated_cost_usd": 0.42,
                "cost_complete": False,
            },
            sort_keys=True,
        ).encode("utf-8"),
    }
    entries: list[WorkerArtifactEntry] = []
    for name, contents in artifacts.items():
        digest = hashlib.sha256(contents).hexdigest()
        object_path = Path("sha256") / digest
        destination = scope / object_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents)
        entries.append(
            WorkerArtifactEntry(
                name=name,
                digest=f"sha256:{digest}",
                byte_size=len(contents),
                object_path=object_path.as_posix(),
            )
        )
    archive = WorkerArtifactArchive(
        execution_id=EXECUTION_ID,
        task_id=task.task_id,
        release_digest=RELEASE_DIGEST,
        image_digest=IMAGE_DIGEST,
        source_bundle_digest=SOURCE_BUNDLE_DIGEST,
        outcome=outcome,
        exit_code=0 if outcome == "succeeded" else 1,
        entries=entries,
    )
    manifest = archive.model_dump_json().encode("utf-8")
    execution_root = scope / "executions" / EXECUTION_ID
    execution_root.mkdir(parents=True)
    (execution_root / "manifest.json").write_bytes(manifest)
    (execution_root / "complete").write_text(
        hashlib.sha256(manifest).hexdigest() + "\n",
        encoding="ascii",
    )
    return archive


def _ingestor(
    tmp_path: Path,
    issue_task: IssueTask,
    *,
    status: ExecutionStatus,
    outcome: str,
    failure_kind: str | None,
    result: bytes | None = None,
    archive_verified: bool = True,
) -> tuple[
    CandidateIngestor,
    SqliteRunStore,
    SqliteInternalPullRequestCandidateStore,
]:
    task = issue_task.model_copy(
        update={"named_baseline": f"main@{BASE_SHA}"}
    )
    runs = SqliteRunStore(tmp_path / "runs.db")
    runs.create_run(task, run_id=RUN_ID)
    archive_root = tmp_path / "worker-artifacts"
    archive = _write_archive(
        archive_root,
        task,
        result=result if result is not None else _payload(task),
        outcome=outcome,
    )
    worker = CodexWorkerMetadata(
        release_digest=RELEASE_DIGEST,
        source_bundle_request_id="source-1",
        source_bundle_digest=SOURCE_BUNDLE_DIGEST,
        base_sha=BASE_SHA,
        model_profile="codex-primary",
        model="glm-5.2",
        reasoning_effort="xhigh",
        context_window=262_144,
    )
    request = ExecutionRequest(
        request_id="request-1",
        task_id=task.task_id,
        run_id=RUN_ID,
        node_id="implement",
        repository="acme/kernel",
        workspace_lease_id="workspace-1",
        candidate_digest=CANDIDATE_DIGEST,
        command=["codex", "exec"],
        environment=ExecutionEnvironment.KUBERNETES,
        image_repository="example.invalid/project-hermes-worker",
        image_digest=IMAGE_DIGEST,
        timeout_seconds=3600,
        network_access=False,
        idempotency_key="task-1-worker",
        metadata={"codex_worker": worker.model_dump(mode="json")},
    )
    observation_result: dict[str, object] = {
        "archive_verified": archive_verified,
        "verified_archive": archive.model_dump(mode="json"),
    }
    if failure_kind is not None:
        observation_result["failure_kind"] = failure_kind
    execution = ExecutionRecord(
        execution_id=EXECUTION_ID,
        request=request,
        status=status,
        observation=ExecutionObservation(
            execution_id=EXECUTION_ID,
            status=status,
            terminated=True,
            exit_code=archive.exit_code,
            result=observation_result,
            started_at=NOW,
            completed_at=NOW,
            duration_ms=12_000,
            observed_at=NOW,
        ),
    )
    executions = SqliteExecutionStore(tmp_path / "executions.db")
    executions.create(execution)
    candidates = SqliteInternalPullRequestCandidateStore(
        tmp_path / "candidates.db"
    )
    return (
        CandidateIngestor(
            executions,
            runs,
            candidates,
            artifact_archive_root=archive_root,
        ),
        runs,
        candidates,
    )


def test_provider_capacity_failure_recovers_strict_completed_result(
    tmp_path: Path,
    issue_task: IssueTask,
) -> None:
    ingestor, runs, candidates = _ingestor(
        tmp_path,
        issue_task,
        status=ExecutionStatus.FAILED,
        outcome="failed",
        failure_kind="provider_capacity_exhausted",
    )

    [result] = ingestor.reconcile_all()

    assert result.status == "candidate_saved"
    assert result.candidate_id is not None
    candidate = candidates.get(result.candidate_id)
    assert candidate.lock_state is InternalCandidateLockState.APPROVAL_PENDING
    assert candidate.accounting.rounds[0].outcome == "failed"
    transport = next(
        check
        for check in candidate.checks
        if check.name == "worker final response transport"
    )
    assert transport.conclusion is InternalCandidateCheckConclusion.NEUTRAL
    assert "provider capacity" in transport.summary
    assert runs.get_run(RUN_ID).status is LifecycleStatus.COMPLETED
    event = next(
        item
        for item in runs.list_events(RUN_ID)
        if item.event_type == "worker.candidate_reconciled"
    )
    assert event.payload["status"] == LifecycleStatus.COMPLETED.value
    assert "recovered" in event.payload["reason"]

    [repeated] = ingestor.reconcile_all()

    assert repeated.candidate_id == result.candidate_id
    assert candidates.count() == 1


def test_successful_execution_keeps_normal_candidate_evidence(
    tmp_path: Path,
    issue_task: IssueTask,
) -> None:
    ingestor, runs, candidates = _ingestor(
        tmp_path,
        issue_task,
        status=ExecutionStatus.SUCCEEDED,
        outcome="succeeded",
        failure_kind=None,
    )

    [result] = ingestor.reconcile_all()

    assert result.status == "candidate_saved"
    candidate = candidates.get(result.candidate_id or "")
    assert not any(
        check.name == "worker final response transport"
        for check in candidate.checks
    )
    assert candidate.accounting.rounds[0].outcome == "succeeded"
    assert runs.get_run(RUN_ID).status is LifecycleStatus.COMPLETED


@pytest.mark.parametrize(
    ("failure_kind", "archive_verified"),
    [
        (None, True),
        ("worker_error", True),
        ("provider_capacity_exhausted", False),
    ],
)
def test_failed_execution_without_full_recovery_boundary_is_not_ingested(
    tmp_path: Path,
    issue_task: IssueTask,
    failure_kind: str | None,
    archive_verified: bool,
) -> None:
    ingestor, runs, candidates = _ingestor(
        tmp_path,
        issue_task,
        status=ExecutionStatus.FAILED,
        outcome="failed",
        failure_kind=failure_kind,
        archive_verified=archive_verified,
    )

    [result] = ingestor.reconcile_all()

    assert result.status in {"execution_failed", "archive_rejected"}
    assert candidates.count() == 0
    assert runs.get_run(RUN_ID).status is LifecycleStatus.FAILED


@pytest.mark.parametrize(
    "result",
    [
        b"{}",
        pytest.param(None, id="wrong-execution-identity"),
    ],
)
def test_provider_capacity_recovery_rejects_invalid_or_mismatched_result(
    tmp_path: Path,
    issue_task: IssueTask,
    result: bytes | None,
) -> None:
    task = issue_task.model_copy(
        update={"named_baseline": f"main@{BASE_SHA}"}
    )
    candidate_result = (
        result
        if result is not None
        else _payload(task, execution_id="execution-other")
    )
    ingestor, runs, candidates = _ingestor(
        tmp_path,
        task,
        status=ExecutionStatus.FAILED,
        outcome="failed",
        failure_kind="provider_capacity_exhausted",
        result=candidate_result,
    )

    [ingested] = ingestor.reconcile_all()

    assert ingested.status == "archive_rejected"
    assert candidates.count() == 0
    assert runs.get_run(RUN_ID).status is LifecycleStatus.FAILED


def test_provider_capacity_recovery_requires_failed_archive_outcome(
    tmp_path: Path,
    issue_task: IssueTask,
) -> None:
    ingestor, runs, candidates = _ingestor(
        tmp_path,
        issue_task,
        status=ExecutionStatus.FAILED,
        outcome="succeeded",
        failure_kind="provider_capacity_exhausted",
    )

    [result] = ingestor.reconcile_all()

    assert result.status == "archive_rejected"
    assert result.error == "verified archive projection is inconsistent"
    assert candidates.count() == 0
    assert runs.get_run(RUN_ID).status is LifecycleStatus.FAILED
