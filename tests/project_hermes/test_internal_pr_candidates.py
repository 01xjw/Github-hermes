from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from project_hermes.config import ProjectHermesConfig
from project_hermes.publication import (
    InternalCandidateAccounting,
    InternalCandidateAccountingRound,
    InternalCandidateCheckConclusion,
    InternalCandidateCheckStatus,
    InternalCandidateComparison,
    InternalCandidateLockState,
    InternalPullRequestCandidate,
    InternalPullRequestCheck,
    InternalPullRequestCommit,
    InternalPullRequestFile,
    InternalPullRequestReview,
    SqliteInternalPullRequestCandidateStore,
)
from project_hermes.web_integration import mount_project_hermes


NOW = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)


def _candidate() -> InternalPullRequestCandidate:
    return InternalPullRequestCandidate(
        candidate_id="candidate-17",
        task_id="task-17",
        title="Fix the kernel boundary",
        body="## Summary\n\nKeep the kernel launch within its valid range.",
        repository="acme/kernel",
        base_ref="main",
        base_sha="a" * 40,
        head_ref="project-hermes/issue-17",
        head_sha="b" * 40,
        commits=[
            InternalPullRequestCommit(
                sha="b" * 40,
                message="Fix the kernel boundary",
                author_name="ProjectHermes",
                authored_at=NOW,
            )
        ],
        files=[
            InternalPullRequestFile(
                path="src/kernel.py",
                status="M",
                added=1,
                removed=1,
                diff=(
                    "--- a/src/kernel.py\n"
                    "+++ b/src/kernel.py\n"
                    "@@ -1 +1 @@\n"
                    "-LIMIT = 0\n"
                    "+LIMIT = 1"
                ),
                necessity="Correct the issue's production boundary.",
            )
        ],
        checks=[
            InternalPullRequestCheck(
                name="focused tests",
                status=InternalCandidateCheckStatus.COMPLETED,
                conclusion=InternalCandidateCheckConclusion.SUCCESS,
                summary="12 tests passed",
                evidence_ids=["evidence-tests"],
                validation_overlay_digest="sha256:" + "c" * 64,
                started_at=NOW,
                completed_at=NOW,
            )
        ],
        reviews=[
            InternalPullRequestReview(
                review_id="review-completion",
                role="completion-auditor",
                reviewer="reviewer-1",
                verdict="APPROVE",
                summary="The issue goal is satisfied.",
                submitted_at=NOW,
            ),
            InternalPullRequestReview(
                review_id="review-minimal",
                role="minimal-diff-reviewer",
                reviewer="reviewer-2",
                verdict="APPROVE",
                summary="Every production line is necessary.",
                submitted_at=NOW,
            ),
        ],
        accounting=InternalCandidateAccounting(
            wall_clock_duration_ms=4_000,
            active_duration_ms=2_000,
            estimated_llm_cost_usd=0.12,
            cost_complete=True,
            rounds=[
                InternalCandidateAccountingRound(
                    round_id="round-1",
                    kind="model_turn",
                    role="codex",
                    outcome="completed",
                    duration_ms=2_000,
                    input_tokens=100,
                    output_tokens=20,
                    estimated_cost_usd=0.12,
                )
            ],
        ),
        created_at=NOW,
        updated_at=NOW,
    )


def test_candidate_store_persists_and_enforces_post_lock_comparison(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control-plane.db"
    store = SqliteInternalPullRequestCandidateStore(path)
    draft = _candidate()
    store.save(draft)

    reopened = SqliteInternalPullRequestCandidateStore(path)
    assert reopened.get(draft.candidate_id) == draft
    with pytest.raises(PermissionError, match="reference PR withheld"):
        reopened.record_comparison(
            draft.candidate_id,
            InternalCandidateComparison(summary="Would reveal too early."),
        )

    locked = draft.immutable_copy(approved_by="operator", approved_at=NOW)
    reopened.save(locked)
    compared = reopened.record_comparison(
        draft.candidate_id,
        InternalCandidateComparison(
            summary="The internal candidate covers the same boundary.",
            coverage=["Both solutions validate the upper bound."],
            minimality=["The internal candidate changes one production file."],
            compared_at=NOW,
        ),
    )

    changed_draft = InternalPullRequestCandidate.model_validate(
        {**draft.model_dump(), "title": "Replace the locked solution"}
    )
    changed_locked = changed_draft.immutable_copy(
        approved_by="operator",
        approved_at=NOW,
    )
    with pytest.raises(ValueError, match="immutable candidate solution"):
        reopened.save(changed_locked)

    assert compared.lock_digest == locked.solution_digest()
    assert compared.post_lock_comparison is not None
    assert reopened.count() == 1


def test_candidate_store_appends_independent_reviews_to_frozen_input(
    tmp_path: Path,
) -> None:
    store = SqliteInternalPullRequestCandidateStore(tmp_path / "control.db")
    candidate = _candidate().model_copy(
        update={
            "reviews": [],
            "lock_state": InternalCandidateLockState.APPROVAL_PENDING,
        }
    )
    store.save(candidate)
    input_digest = candidate.review_input_digest()
    completion = InternalPullRequestReview(
        review_id="review-completion-new",
        role="completion-auditor",
        reviewer="minimax/session-1",
        verdict="APPROVE",
        summary="The locked acceptance criteria are supported.",
        submitted_at=NOW,
    )
    minimal = InternalPullRequestReview(
        review_id="review-minimal-new",
        role="minimal-diff-reviewer",
        reviewer="minimax/session-2",
        verdict="APPROVE",
        summary="Every production hunk is necessary.",
        submitted_at=NOW,
    )

    first = store.append_review(
        candidate.candidate_id,
        completion,
        expected_input_digest=input_digest,
    )
    second = store.append_review(
        candidate.candidate_id,
        minimal,
        expected_input_digest=input_digest,
    )

    assert [review.role for review in first.reviews] == [
        "completion-auditor"
    ]
    assert [review.role for review in second.reviews] == [
        "completion-auditor",
        "minimal-diff-reviewer",
    ]
    with pytest.raises(ValueError, match="already has a review"):
        store.append_review(
            candidate.candidate_id,
            minimal.model_copy(update={"review_id": "replacement"}),
            expected_input_digest=input_digest,
        )
    with pytest.raises(ValueError, match="review input changed"):
        store.append_review(
            candidate.candidate_id,
            minimal.model_copy(
                update={
                    "review_id": "stale",
                    "role": "another-reviewer",
                    "reviewer": "minimax/session-3",
                }
            ),
            expected_input_digest="sha256:" + "0" * 64,
        )


def test_authenticated_candidate_list_detail_and_file_diff_api(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "project-hermes.yaml"
    config = ProjectHermesConfig(
        project_root=tmp_path,
        control_plane={
            "mode": "sqlite",
            "sqlite_path": tmp_path / "control-plane.db",
        },
        codex={
            "enabled": False,
            "runtime_root": tmp_path / "runtime",
        },
    )
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )

    def require_token(request: Request) -> None:
        if request.headers.get("X-Test-Token") != "allowed":
            raise HTTPException(status_code=401, detail="authentication required")

    app = FastAPI()
    services = mount_project_hermes(
        app,
        require_dashboard_token=require_token,
        config_path=config_path,
        test_mode=True,
    )
    assert services is not None
    services.candidates.save(_candidate())
    client = TestClient(app)
    root = "/api/v2/project-hermes/pull-request-candidates"
    headers = {"X-Test-Token": "allowed"}

    assert client.get(root).status_code == 401
    listed = client.get(root, headers=headers)
    detail = client.get(f"{root}/candidate-17", headers=headers)
    diff = client.get(
        f"{root}/candidate-17/files/diff",
        params={"path": "src/kernel.py"},
        headers=headers,
    )

    assert listed.status_code == 200
    assert listed.json()["candidates"][0]["title"] == "Fix the kernel boundary"
    assert detail.status_code == 200
    assert detail.json()["post_lock_comparison"] is None
    assert "diff" not in detail.json()["files"][0]
    assert diff.status_code == 200
    assert "+LIMIT = 1" in diff.json()["diff"]

    publish_path = f"{root}/candidate-17/publish"
    confirmation = {
        "schema_version": "publish-internal-pr-request.v1",
        "confirmed_lock_digest": "sha256:" + "0" * 64,
        "confirm": True,
    }
    unlocked = client.post(
        publish_path,
        json=confirmation,
        headers=headers,
    )
    assert unlocked.status_code == 409
    assert "approved and immutable" in unlocked.json()["detail"]

    locked = _candidate().immutable_copy(
        approved_by="operator",
        approved_at=NOW,
    )
    services.candidates.save(locked)
    changed_approval = client.post(
        publish_path,
        json=confirmation,
        headers=headers,
    )
    assert changed_approval.status_code == 409
    assert "approval changed" in changed_approval.json()["detail"]
