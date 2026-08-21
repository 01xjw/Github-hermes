from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from project_hermes.assurance import ReviewRole
from project_hermes.candidate_review import (
    InternalCandidateReviewService,
    _load_reviewer_profile,
)
from project_hermes.config import ProjectHermesConfig
from project_hermes.models import IssueTask
from project_hermes.publication import (
    InternalCandidateCheckConclusion,
    InternalCandidateCheckStatus,
    InternalCandidateLockState,
    InternalPullRequestCandidate,
    InternalPullRequestCheck,
    InternalPullRequestCommit,
    InternalPullRequestFile,
    InternalPullRequestReview,
    SqliteInternalPullRequestCandidateStore,
)
from project_hermes.runtime.base import RuntimeRequest
from project_hermes.store import SqliteRunStore


NOW = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)


class _ReviewerSession:
    def __init__(self, request: RuntimeRequest) -> None:
        assert request.session_id is not None
        self.session_id = request.session_id
        self.request = request
        self.closed = False

    def run_runtime_turn(self, request: RuntimeRequest) -> dict[str, Any]:
        assert request is self.request
        role = request.context["review_role"]
        summary = (
            "The candidate and checks satisfy the locked goal."
            if role == "completion-auditor"
            else "The single production hunk is necessary and scoped."
        )
        return {
            "final_response": json.dumps(
                {
                    "verdict": "APPROVE",
                    "summary": summary,
                    "findings": [],
                }
            )
        }

    def close(self) -> None:
        self.closed = True


class _ReviewerFactory:
    def __init__(self) -> None:
        self.sessions: list[_ReviewerSession] = []

    def create_reviewer(self, request: RuntimeRequest) -> _ReviewerSession:
        session = _ReviewerSession(request)
        self.sessions.append(session)
        return session


def _candidate(task: IssueTask) -> InternalPullRequestCandidate:
    return InternalPullRequestCandidate(
        candidate_id="candidate-review-1",
        task_id=task.task_id,
        title="Repair the kernel result",
        body="The focused change repairs the locked correctness boundary.",
        repository="acme/kernel",
        base_ref="main",
        base_sha="a" * 40,
        head_ref="project-hermes/task-1",
        head_sha="b" * 40,
        commits=[
            InternalPullRequestCommit(
                sha="b" * 40,
                message="Repair the kernel result",
                author_name="ProjectHermes Worker",
                authored_at=NOW,
            )
        ],
        files=[
            InternalPullRequestFile(
                path="src/kernel.py",
                status="M",
                added=1,
                removed=1,
                diff="@@ -1 +1 @@\n-old\n+new\n",
                necessity="Repairs the locked result boundary.",
            )
        ],
        checks=[
            InternalPullRequestCheck(
                name="focused regression",
                status=InternalCandidateCheckStatus.COMPLETED,
                conclusion=InternalCandidateCheckConclusion.SUCCESS,
                summary="The regression passes.",
                evidence_ids=["artifact://worker/result.json"],
                started_at=NOW,
                completed_at=NOW,
            )
        ],
        lock_state=InternalCandidateLockState.APPROVAL_PENDING,
        created_at=NOW,
        updated_at=NOW,
    )


def test_candidate_review_service_uses_two_fresh_minimax_sessions(
    tmp_path: Path,
    issue_task: IssueTask,
) -> None:
    config = ProjectHermesConfig(
        project_root=tmp_path,
        control_plane={
            "mode": "sqlite",
            "sqlite_path": tmp_path / "control.db",
        },
        codex={"enabled": False, "runtime_root": tmp_path / "runtime"},
    )
    runs = SqliteRunStore(tmp_path / "runs.db")
    runs.create_run(issue_task, run_id="run-review-1")
    candidates = SqliteInternalPullRequestCandidateStore(
        tmp_path / "candidates.db"
    )
    candidate = _candidate(issue_task)
    candidates.save(candidate)
    factory = _ReviewerFactory()
    service = InternalCandidateReviewService(
        config,
        runs,
        candidates,
        factory,
    )

    first = service.review_next()
    second = service.review_next()
    complete = service.review_next()

    assert first is not None and first.role.value == "completion-auditor"
    assert second is not None and second.role.value == "minimal-diff-reviewer"
    assert complete is None
    assert len(factory.sessions) == 2
    assert all(session.closed for session in factory.sessions)
    assert len({session.session_id for session in factory.sessions}) == 2
    assert all(
        session.request.model_route is not None
        and session.request.model_route.profile == "reviewer"
        and session.request.model_route.provider_wire_api == "chat"
        for session in factory.sessions
    )
    first_request, second_request = [
        session.request for session in factory.sessions
    ]
    assert "# Completion Auditor Soul" in first_request.prompt
    assert "# Completion Auditor Contract" in first_request.prompt
    assert "# Minimal-Diff Reviewer Soul" not in first_request.prompt
    assert "# Minimal-Diff Reviewer Soul" in second_request.prompt
    assert "# Minimal-Diff Reviewer Contract" in second_request.prompt
    assert "# Completion Auditor Soul" not in second_request.prompt
    profile_digests = {
        request.context["review_role"]: request.context[
            "reviewer_profile_digest"
        ]
        for request in (first_request, second_request)
    }
    assert len(profile_digests) == 2
    assert all(len(digest) == 64 for digest in profile_digests.values())
    assert all(
        request.context["reviewer_profile_digest"]
        == request.metadata["reviewer_profile_digest"]
        for request in (first_request, second_request)
    )
    reviewed = candidates.get(candidate.candidate_id)
    assert [review.role for review in reviewed.reviews] == [
        "completion-auditor",
        "minimal-diff-reviewer",
    ]
    assert all(review.verdict == "APPROVE" for review in reviewed.reviews)
    assert all(
        profile_digests[review.role] in review.reviewer
        for review in reviewed.reviews
    )
    assert reviewed.review_input_digest() == candidate.review_input_digest()
    assert reviewed.lock_state is InternalCandidateLockState.IMMUTABLE_APPROVED
    assert reviewed.locked_by is not None
    assert reviewed.locked_by.startswith("project-hermes-dual-review:")


def test_candidate_review_service_does_not_lock_nonapproval(
    tmp_path: Path,
    issue_task: IssueTask,
) -> None:
    class _EvidenceSession(_ReviewerSession):
        def run_runtime_turn(self, request: RuntimeRequest) -> dict[str, Any]:
            if request.context["review_role"] == "completion-auditor":
                return {
                    "final_response": json.dumps(
                        {
                            "verdict": "MORE_EVIDENCE_REQUIRED",
                            "summary": "The target regression was not executed.",
                            "findings": ["Run the frozen target regression."],
                        }
                    )
                }
            return super().run_runtime_turn(request)

    class _EvidenceFactory(_ReviewerFactory):
        def create_reviewer(self, request: RuntimeRequest) -> _ReviewerSession:
            session = _EvidenceSession(request)
            self.sessions.append(session)
            return session

    config = ProjectHermesConfig(
        project_root=tmp_path,
        control_plane={
            "mode": "sqlite",
            "sqlite_path": tmp_path / "control.db",
        },
        codex={"enabled": False, "runtime_root": tmp_path / "runtime"},
    )
    runs = SqliteRunStore(tmp_path / "runs.db")
    runs.create_run(issue_task, run_id="run-review-nonapproval")
    candidates = SqliteInternalPullRequestCandidateStore(
        tmp_path / "candidates.db"
    )
    candidate = _candidate(issue_task)
    candidates.save(candidate)
    service = InternalCandidateReviewService(
        config,
        runs,
        candidates,
        _EvidenceFactory(),
    )

    assert service.review_next() is not None
    assert service.review_next() is not None
    assert service.review_next() is None

    reviewed = candidates.get(candidate.candidate_id)
    assert reviewed.lock_state is InternalCandidateLockState.APPROVAL_PENDING
    assert {review.verdict for review in reviewed.reviews} == {
        "APPROVE",
        "MORE_EVIDENCE_REQUIRED",
    }


def test_candidate_review_service_finalizes_existing_unanimous_reviews(
    tmp_path: Path,
    issue_task: IssueTask,
) -> None:
    config = ProjectHermesConfig(
        project_root=tmp_path,
        control_plane={
            "mode": "sqlite",
            "sqlite_path": tmp_path / "control.db",
        },
        codex={"enabled": False, "runtime_root": tmp_path / "runtime"},
    )
    runs = SqliteRunStore(tmp_path / "runs.db")
    runs.create_run(issue_task, run_id="run-review-resume")
    candidates = SqliteInternalPullRequestCandidateStore(
        tmp_path / "candidates.db"
    )
    candidate = _candidate(issue_task)
    candidates.save(candidate)
    for index, role in enumerate(ReviewRole, start=1):
        candidates.append_review(
            candidate.candidate_id,
            InternalPullRequestReview(
                review_id=f"existing-review-{index}",
                role=role.value,
                reviewer=f"minimax/session-{index}",
                verdict="APPROVE",
                summary=f"{role.value} approved the frozen candidate.",
                submitted_at=NOW,
            ),
            expected_input_digest=candidate.review_input_digest(),
        )
    factory = _ReviewerFactory()
    service = InternalCandidateReviewService(
        config,
        runs,
        candidates,
        factory,
    )

    assert service.review_next() is None
    assert factory.sessions == []
    reviewed = candidates.get(candidate.candidate_id)
    assert reviewed.lock_state is InternalCandidateLockState.IMMUTABLE_APPROVED
    assert reviewed.lock_digest == reviewed.solution_digest()


def test_reviewer_role_files_are_required_and_digest_bound(
    tmp_path: Path,
) -> None:
    role_root = tmp_path / ReviewRole.COMPLETION_AUDITOR.value
    role_root.mkdir(parents=True)
    (role_root / "SOUL.md").write_text(
        "# Completion Soul\n\nBe exact.\n",
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="AGENT.md"):
        _load_reviewer_profile(tmp_path, ReviewRole.COMPLETION_AUDITOR)

    (role_root / "AGENT.md").write_text(
        "# Completion Contract\n\nAudit the evidence.\n",
        encoding="utf-8",
    )
    first = _load_reviewer_profile(
        tmp_path,
        ReviewRole.COMPLETION_AUDITOR,
    )
    (role_root / "AGENT.md").write_text(
        "# Completion Contract\n\nAudit every criterion.\n",
        encoding="utf-8",
    )
    second = _load_reviewer_profile(
        tmp_path,
        ReviewRole.COMPLETION_AUDITOR,
    )

    assert first.soul_digest == second.soul_digest
    assert first.agent_digest != second.agent_digest
    assert first.profile_digest != second.profile_digest
