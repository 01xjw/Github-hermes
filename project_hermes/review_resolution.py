"""Independent review finding resolution and publication gates."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from project_hermes.assurance import (
    FindingSeverity,
    ReviewFinding,
    ReviewRecord,
)
from project_hermes.models import StrictModel, utc_now


class FindingDisposition(StrEnum):
    """Allowed Hermes Lead dispositions for reviewer findings."""

    FIXED = "fixed"
    ACCEPTED_RISK = "accepted_risk"
    REJECTED_WITH_EVIDENCE = "rejected_with_evidence"
    ESCALATED = "escalated"


class ReviewResolution(StrictModel):
    """Auditable disposition of exactly one review finding."""

    schema_version: Literal["review-resolution.v1"] = "review-resolution.v1"
    resolution_id: str
    task_id: str
    review_id: str
    finding_id: str
    reviewed_diff_sha: str
    goal_revision: int = Field(ge=0)
    disposition: FindingDisposition
    rationale: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    replacement_diff_sha: str | None = None
    resolved_by_session_id: str
    independently_decided_by: str | None = None
    decided_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def disposition_has_required_provenance(self) -> "ReviewResolution":
        if self.disposition is FindingDisposition.FIXED:
            if not self.replacement_diff_sha or not self.evidence_ids:
                raise ValueError(
                    "fixed findings require a replacement diff and evidence"
                )
            if self.replacement_diff_sha == self.reviewed_diff_sha:
                raise ValueError("a fixed finding must produce a new diff")
        elif self.disposition in {
            FindingDisposition.ACCEPTED_RISK,
            FindingDisposition.REJECTED_WITH_EVIDENCE,
        }:
            if not self.independently_decided_by or not self.evidence_ids:
                raise ValueError(
                    "risk acceptance and rejection require independent "
                    "decision evidence"
                )
        elif self.disposition is FindingDisposition.ESCALATED:
            if not self.independently_decided_by:
                raise ValueError(
                    "escalated findings require an escalation reference"
                )
        return self


class ResolutionGateResult(StrictModel):
    """Fail-closed result for one frozen review set."""

    schema_version: Literal["resolution-gate-result.v1"] = (
        "resolution-gate-result.v1"
    )
    approved: bool
    reasons: list[str] = Field(default_factory=list)
    resolved_finding_ids: list[str] = Field(default_factory=list)
    requires_fresh_review: bool = False


class ReviewResolutionGate:
    """Ensure no implementer can silently dismiss a blocking finding."""

    _BLOCKING = {
        FindingSeverity.MEDIUM,
        FindingSeverity.HIGH,
        FindingSeverity.CRITICAL,
    }

    def evaluate(
        self,
        reviews: list[ReviewRecord],
        resolutions: list[ReviewResolution],
        *,
        task_id: str,
        reviewed_diff_sha: str,
        goal_revision: int,
    ) -> ResolutionGateResult:
        reasons: list[str] = []
        resolved: list[str] = []
        requires_fresh_review = False
        by_key: dict[tuple[str, str], list[ReviewResolution]] = {}
        for resolution in resolutions:
            key = (resolution.review_id, resolution.finding_id)
            by_key.setdefault(key, []).append(resolution)

        for review in reviews:
            if (
                review.task_id != task_id
                or review.code_diff_sha != reviewed_diff_sha
                or review.goal_revision != goal_revision
            ):
                reasons.append(f"review {review.review_id} is outside the gate")
                continue
            for finding in review.findings:
                if finding.severity not in self._BLOCKING:
                    continue
                records = by_key.get((review.review_id, finding.finding_id), [])
                if len(records) != 1:
                    reasons.append(
                        f"finding {finding.finding_id} requires one resolution"
                    )
                    continue
                resolution = records[0]
                if (
                    resolution.task_id != task_id
                    or resolution.reviewed_diff_sha != reviewed_diff_sha
                    or resolution.goal_revision != goal_revision
                ):
                    reasons.append(
                        f"resolution {resolution.resolution_id} is stale"
                    )
                    continue
                if resolution.disposition is FindingDisposition.ESCALATED:
                    reasons.append(
                        f"finding {finding.finding_id} awaits escalation"
                    )
                    continue
                if resolution.disposition is FindingDisposition.FIXED:
                    requires_fresh_review = True
                resolved.append(finding.finding_id)

        return ResolutionGateResult(
            approved=not reasons,
            reasons=reasons,
            resolved_finding_ids=sorted(set(resolved)),
            requires_fresh_review=requires_fresh_review,
        )


def blocking_findings(review: ReviewRecord) -> list[ReviewFinding]:
    """Return findings that prevent publication without disposition."""

    return [
        finding
        for finding in review.findings
        if finding.severity
        in {
            FindingSeverity.MEDIUM,
            FindingSeverity.HIGH,
            FindingSeverity.CRITICAL,
        }
    ]
