from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from project_hermes.assurance import (
    CandidateFileAssessment,
    CompletionEntry,
    CompletionLayer,
    CompletionStatus,
    DependencyChange,
    EvidenceRecord,
    LocalTestOverlay,
    MinimalDiffAssessment,
    MinimalDiffVerdict,
    ReviewProgress,
    ReviewRecord,
    ReviewPacket,
    ReviewRole,
    ReviewVerdict,
)
from project_hermes.assurance_store import (
    SqliteAssuranceStore,
    SqliteCandidateAssuranceStore,
)
from project_hermes.controller import ProjectHermesController
from project_hermes.knowledge import (
    ComparisonFinding,
    ComparisonFindingCategory,
    SqliteLearningStore,
)
from project_hermes.models import IssueTask, ProjectRole
from project_hermes.publication import (
    CandidateValidation,
    PostLockComparator,
    PullRequestCandidate,
    TargetPullRequestOracle,
)
from project_hermes.store import SqliteRunStore


def _overlay() -> LocalTestOverlay:
    return LocalTestOverlay.from_files(
        overlay_id="overlay-1",
        task_id="task-1",
        code_diff_sha="abcdef1234",
        files={
            "tests/test_fix.py": (
                "def test_regression():\n"
                "    assert repaired_result() == 1\n"
            )
        },
        command=["python", "-m", "pytest", "tests/test_fix.py"],
        passed=True,
        results={"exit_code": 0, "passed": True, "tests_passed": 1},
    )


def _assessment(
    overlay: LocalTestOverlay,
    evidence_ids: list[str],
    *,
    verdict: MinimalDiffVerdict = MinimalDiffVerdict.APPROVE,
    unnecessary_paths: list[str] | None = None,
) -> MinimalDiffAssessment:
    return MinimalDiffAssessment(
        assessment_id="assessment-1",
        task_id="task-1",
        repository="acme/kernel",
        code_diff_sha="abcdef1234",
        goal_revision=0,
        candidate_files=[
            CandidateFileAssessment(
                path="src/fix.py",
                additions=2,
                deletions=1,
                production_code_lines=3,
                necessity="Repairs the incorrect production result.",
            ),
            CandidateFileAssessment(
                path="pyproject.toml",
                additions=1,
                deletions=1,
                production_code_lines=0,
                necessity="Declares the runtime package required by the fix.",
            ),
        ],
        dependency_changes=[
            DependencyChange(
                path="pyproject.toml",
                package="required-runtime",
                change="Add the required runtime dependency.",
                necessity="The production fix imports this package.",
            )
        ],
        local_test_overlay=overlay.summary(),
        correctness_evidence_ids=evidence_ids,
        correctness_passed=True,
        unnecessary_paths=unnecessary_paths or [],
        reviewer_session_id="minimal-session",
        reviewer_explanation=(
            "Both production files are necessary; the regression test remains "
            "a local validation overlay."
        ),
        verdict=verdict,
    )


def test_minimal_diff_keeps_local_tests_out_of_candidate(tmp_path: Path) -> None:
    store = SqliteCandidateAssuranceStore(tmp_path / "assurance.db")
    overlay = store.put_test_overlay(_overlay())
    assessment = store.put_minimal_diff_assessment(
        _assessment(overlay, ["evidence-implemented"])
    )
    candidate = PullRequestCandidate.from_assessment(
        candidate_id="candidate-1",
        assessment=assessment,
        base_sha="0123456789abcdef",
        head_sha="fedcba9876543210",
        branch="project-hermes/task-1",
        body="Repair the production result with the smallest required change.",
        includes_untracked=True,
    )

    assert candidate.files == ["src/fix.py", "pyproject.toml"]
    assert "tests/test_fix.py" not in candidate.files
    assert candidate.checks is not None
    assert candidate.checks.local_test_overlay is not None
    assert (
        candidate.checks.local_test_overlay.content_digest
        == overlay.content_digest
    )
    assert candidate.checks.local_test_overlay.results["tests_passed"] == 1
    assert assessment.candidate_line_count == 5
    assert assessment.production_code_line_count == 3

    with pytest.raises(ValidationError, match="avoidable edits"):
        _assessment(
            overlay,
            ["evidence-implemented"],
            unnecessary_paths=["src/cleanup.py"],
        )
    rejected = _assessment(
        overlay,
        ["evidence-implemented"],
        verdict=MinimalDiffVerdict.REJECT,
        unnecessary_paths=["src/cleanup.py"],
    )
    with pytest.raises(PermissionError, match="minimal-diff approval"):
        PullRequestCandidate.from_assessment(
            candidate_id="rejected",
            assessment=rejected,
            base_sha="0123456789abcdef",
            head_sha="fedcba9876543210",
            branch="project-hermes/task-1",
            body="Rejected candidate.",
            includes_untracked=True,
        )


def test_lock_oracle_comparison_and_knowledge_are_fail_closed(
    tmp_path: Path,
    issue_task: IssueTask,
) -> None:
    database = tmp_path / "assurance.db"
    runs = SqliteRunStore(tmp_path / "runs.db")
    assurance = SqliteAssuranceStore(database)
    candidate_store = SqliteCandidateAssuranceStore(database)
    controller = ProjectHermesController(
        runs,
        assurance,
        candidate_assurance_store=candidate_store,
    )
    controller.create_task_run(issue_task, run_id="run-1")

    matrix = assurance.get_completion_matrix("task-1")
    completion_evidence_ids: list[str] = []
    for layer in matrix.goal_paths["correctness"].required_layers:
        evidence_id = f"evidence-{layer.value}"
        completion_evidence_ids.append(evidence_id)
        assurance.put_evidence(
            EvidenceRecord(
                evidence_id=evidence_id,
                task_id="task-1",
                node_id="validation",
                path_id="correctness",
                repository="acme/kernel",
                completion_layer=layer,
                producer_role=ProjectRole.RUNNER,
                base_sha="0123456789abcdef",
                code_diff_sha="abcdef1234",
                goal_revision=0,
                environment={"runner": "fixture"},
                command=["python", "-m", "pytest"],
                result={"exit_code": 0, "passed": True},
            )
        )
        matrix.update(
            CompletionEntry(
                layer=layer,
                status=CompletionStatus.SATISFIED,
                evidence_ids=[evidence_id],
                code_diff_sha="abcdef1234",
                goal_revision=0,
            ),
            path_id="correctness",
        )
    assurance.put_completion_matrix(matrix)
    packet = assurance.put_review_packet(
        ReviewPacket(
            packet_id="packet-1",
            task_id="task-1",
            repository="acme/kernel",
            absolute_repository_path=str(tmp_path),
            base_sha="0123456789abcdef",
            branch="project-hermes/task-1",
            code_diff_sha="abcdef1234",
            goal_revision=0,
            complete_diff_ref="artifact://diff/abcdef1234",
            includes_untracked=True,
            completion_matrix_digest=matrix.fingerprint(),
            target_hardware={"architecture": "gfx942"},
            evidence_ids=completion_evidence_ids,
        )
    )
    for role, session in (
        (ReviewRole.COMPLETION_AUDITOR, "completion-session"),
        (ReviewRole.MINIMAL_DIFF_REVIEWER, "minimal-session"),
    ):
        review_evidence_id = f"review-evidence-{role.value}"
        assurance.put_evidence(
            EvidenceRecord(
                evidence_id=review_evidence_id,
                task_id="task-1",
                node_id=f"review-{role.value}",
                path_id="correctness",
                repository="acme/kernel",
                completion_layer=CompletionLayer.CI_REVIEWED,
                producer_role=ProjectRole.REVIEWER,
                base_sha="0123456789abcdef",
                code_diff_sha="abcdef1234",
                goal_revision=0,
                environment={"reviewer": role.value},
                command=["review", role.value],
                result={"passed": True},
            )
        )
        assurance.add_review(
            ReviewRecord(
                review_id=f"review-{role.value}",
                task_id="task-1",
                role=role,
                review_cycle=1,
                reviewer_session_id=session,
                packet_id=packet.packet_id,
                packet_digest=packet.content_hash,
                code_diff_sha="abcdef1234",
                goal_revision=0,
                verdict=ReviewVerdict.APPROVE,
                evidence_ids=[review_evidence_id],
                summary="The exact candidate passes this independent gate.",
                progress=(
                    ReviewProgress.IMPROVED
                    if role is ReviewRole.COMPLETION_AUDITOR
                    else None
                ),
                progress_explanation=(
                    "The exact diff satisfies the frozen correctness goal."
                    if role is ReviewRole.COMPLETION_AUDITOR
                    else None
                ),
                assessed_goal_path_ids=(
                    ["correctness"]
                    if role is ReviewRole.COMPLETION_AUDITOR
                    else []
                ),
            )
        )

    overlay = candidate_store.put_test_overlay(_overlay())
    assessment = candidate_store.put_minimal_diff_assessment(
        _assessment(overlay, completion_evidence_ids)
    )
    candidate = PullRequestCandidate.from_assessment(
        candidate_id="candidate-1",
        assessment=assessment,
        base_sha="0123456789abcdef",
        head_sha="fedcba9876543210",
        branch="project-hermes/task-1",
        body="Repair the production result with the smallest required change.",
        includes_untracked=True,
    )
    validation = CandidateValidation(
        candidate_digest=candidate.fingerprint(),
        successful=True,
        evidence_ids=completion_evidence_ids,
        results={"exit_code": 0, "passed": True},
        local_test_overlay_digest=overlay.content_digest,
    )
    fetch_calls: list[tuple[str, int]] = []

    def fetch_target(repository: str, number: int) -> dict[str, object]:
        fetch_calls.append((repository, number))
        return {
            "state": "merged",
            "title": "Reference title must remain ephemeral",
            "body": "Reference body must remain ephemeral",
            "base_sha": "0123456789abcdef",
            "head_sha": "1111111111111111",
            "files": [
                {
                    "path": "src/fix.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 1,
                    "patch": "-return broken\n+return repaired",
                },
                {
                    "path": "tests/test_fix.py",
                    "status": "added",
                    "additions": 2,
                    "deletions": 0,
                    "patch": "+def test_regression():\n+    assert repaired",
                },
            ],
        }

    oracle = TargetPullRequestOracle(
        candidate_store,
        assurance,
        fetch_target,
    )
    with pytest.raises(KeyError, match="unknown candidate lock"):
        oracle.reveal(
            lock_id="lock-candidate-1",
            candidate=candidate,
            target_pr_url="https://github.com/acme/kernel/pull/17",
        )
    assert fetch_calls == []

    lock = controller.lock_candidate(
        candidate,
        validation,
        repository_bases={"acme/kernel": "0123456789abcdef"},
        implementer_session_id="codex-session",
    )
    assert lock.candidate_digest == candidate.fingerprint()
    assert set(lock.review_approvals) == set(ReviewRole)

    tampered = candidate.model_copy(deep=True)
    tampered.body = "A post-lock mutation."
    with pytest.raises(PermissionError, match="immutable approval lock"):
        oracle.reveal(
            lock_id=lock.lock_id,
            candidate=tampered,
            target_pr_url="https://github.com/acme/kernel/pull/17",
        )
    assert fetch_calls == []

    target = oracle.reveal(
        lock_id=lock.lock_id,
        candidate=candidate,
        target_pr_url="https://github.com/acme/kernel/pull/17",
    )
    learning = SqliteLearningStore(tmp_path / "learning.db")
    comparator = PostLockComparator(
        candidate_store,
        learning,
    )
    with pytest.raises(ValueError, match="cannot quote target PR content"):
        comparator.compare(
            lock_id=lock.lock_id,
            candidate=candidate,
            target=target,
            findings=[
                ComparisonFinding(
                    category=ComparisonFindingCategory.DEFECT_CLASS,
                    summary="return repaired",
                )
            ],
        )
    comparison, knowledge = comparator.compare(
        lock_id=lock.lock_id,
        candidate=candidate,
        target=target,
        findings=[
            ComparisonFinding(
                category=ComparisonFindingCategory.DEFECT_CLASS,
                summary="Incorrect result propagation can survive shallow checks.",
            )
        ],
    )

    assert candidate.fingerprint() == lock.candidate_digest
    assert comparison.target_content_digest == target.content_digest
    assert learning.retrieve("acme/kernel") == [knowledge]
    with sqlite3.connect(tmp_path / "learning.db") as connection:
        serialized = "\n".join(
            str(value)
            for row in connection.execute(
                """
                SELECT comparison_json
                FROM project_hermes_comparisons
                UNION ALL
                SELECT knowledge_json
                FROM project_hermes_reusable_knowledge
                """
            )
            for value in row
        )
    assert "Reference title" not in serialized
    assert "Reference body" not in serialized
    assert "return repaired" not in serialized
