from __future__ import annotations

import pytest
from pydantic import ValidationError

from project_hermes.assurance import (
    ReviewEscalationState,
    ReviewEscalationStatus,
    ReviewProgress,
    ReviewRecord,
    ReviewRole,
    ReviewVerdict,
)
from project_hermes.controller import (
    ReviewEscalationAction,
    evaluate_review_escalation,
)


def _cycle(
    cycle: int,
    progress: ReviewProgress,
    *,
    auditor_verdict: ReviewVerdict = ReviewVerdict.REVISION_REQUIRED,
    diff_suffix: int | None = None,
) -> list[ReviewRecord]:
    suffix = cycle if diff_suffix is None else diff_suffix
    diff_sha = f"{suffix:010x}"
    digest = "sha256:" + f"{suffix:064x}"
    return [
        ReviewRecord(
            review_id=f"completion-{cycle}",
            task_id="task-1",
            role=ReviewRole.COMPLETION_AUDITOR,
            review_cycle=cycle,
            reviewer_session_id=f"completion-session-{cycle}",
            packet_id=f"packet-{cycle}",
            packet_digest=digest,
            code_diff_sha=diff_sha,
            goal_revision=0,
            verdict=auditor_verdict,
            summary=f"Completion review for cycle {cycle}.",
            progress=progress,
            progress_explanation=(
                f"Cycle {cycle} was assessed against frozen goal correctness."
            ),
            assessed_goal_path_ids=["correctness"],
        ),
        ReviewRecord(
            review_id=f"minimal-{cycle}",
            task_id="task-1",
            role=ReviewRole.MINIMAL_DIFF_REVIEWER,
            review_cycle=cycle,
            reviewer_session_id=f"minimal-session-{cycle}",
            packet_id=f"packet-{cycle}",
            packet_digest=digest,
            code_diff_sha=diff_sha,
            goal_revision=0,
            verdict=ReviewVerdict.APPROVE,
            evidence_ids=[f"minimal-evidence-{cycle}"],
            summary=f"Minimal-diff review for cycle {cycle}.",
        ),
    ]


def _state(
    status: ReviewEscalationStatus = ReviewEscalationStatus.NONE,
    *,
    trigger_cycle: int | None = None,
) -> ReviewEscalationState:
    if status is ReviewEscalationStatus.NONE:
        return ReviewEscalationState(task_id="task-1", goal_revision=0)
    return ReviewEscalationState(
        task_id="task-1",
        goal_revision=0,
        status=status,
        trigger_review_cycle=trigger_cycle,
        trigger_review_ids=["completion-2", "minimal-2"],
        reviewer_explanation="Two cycles did not move toward the frozen goal.",
    )


def test_escalates_on_exactly_two_completed_no_progress_cycles() -> None:
    first = evaluate_review_escalation(
        _cycle(1, ReviewProgress.NO_PROGRESS),
        _state(),
    )
    second = evaluate_review_escalation(
        [
            *_cycle(1, ReviewProgress.NO_PROGRESS),
            *_cycle(2, ReviewProgress.REGRESSED),
        ],
        _state(),
    )

    assert first.action is ReviewEscalationAction.NONE
    assert first.consecutive_no_progress == 1
    assert second.action is ReviewEscalationAction.REQUEST_MODEL_ESCALATION
    assert second.review_cycle == 2
    assert second.consecutive_no_progress == 2


def test_improvement_resets_no_progress_sequence() -> None:
    decision = evaluate_review_escalation(
        [
            *_cycle(1, ReviewProgress.NO_PROGRESS),
            *_cycle(2, ReviewProgress.IMPROVED),
            *_cycle(3, ReviewProgress.REGRESSED),
        ],
        _state(),
    )

    assert decision.action is ReviewEscalationAction.NONE
    assert decision.consecutive_no_progress == 1


def test_more_evidence_cycle_does_not_count() -> None:
    decision = evaluate_review_escalation(
        [
            *_cycle(1, ReviewProgress.NO_PROGRESS),
            *_cycle(
                2,
                ReviewProgress.NO_PROGRESS,
                auditor_verdict=ReviewVerdict.MORE_EVIDENCE_REQUIRED,
            ),
            *_cycle(3, ReviewProgress.REGRESSED),
        ],
        _state(),
    )

    assert decision.action is ReviewEscalationAction.REQUEST_MODEL_ESCALATION
    assert decision.review_cycle == 3


def test_goal_revision_starts_a_new_escalation_sequence() -> None:
    decision = evaluate_review_escalation(
        [
            *_cycle(1, ReviewProgress.NO_PROGRESS),
            *_cycle(2, ReviewProgress.REGRESSED),
        ],
        ReviewEscalationState(task_id="task-1", goal_revision=1),
    )

    assert decision.action is ReviewEscalationAction.NONE
    assert decision.consecutive_no_progress == 0


def test_nonapproved_post_escalation_cycle_blocks_operator() -> None:
    decision = evaluate_review_escalation(
        [
            *_cycle(1, ReviewProgress.NO_PROGRESS),
            *_cycle(2, ReviewProgress.REGRESSED),
            *_cycle(3, ReviewProgress.IMPROVED),
        ],
        _state(
            ReviewEscalationStatus.REQUESTED,
            trigger_cycle=2,
        ),
    )

    assert decision.action is ReviewEscalationAction.BLOCK_OPERATOR_ATTENTION
    assert decision.review_cycle == 3
    assert "Cycle 3" in (decision.reviewer_explanation or "")


def test_completion_auditor_requires_explained_progress() -> None:
    with pytest.raises(ValidationError, match="progress"):
        ReviewRecord(
            review_id="completion-1",
            task_id="task-1",
            role=ReviewRole.COMPLETION_AUDITOR,
            review_cycle=1,
            reviewer_session_id="completion-session-1",
            packet_id="packet-1",
            packet_digest="sha256:" + "1" * 64,
            code_diff_sha="1111111",
            goal_revision=0,
            verdict=ReviewVerdict.REVISION_REQUIRED,
            summary="Missing progress fields.",
        )
