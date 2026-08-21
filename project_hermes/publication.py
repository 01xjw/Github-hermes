"""Minimal pull-request candidate and remote verification gates."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from pathlib import PurePosixPath
from threading import RLock
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal, Mapping, Protocol

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from project_hermes.assurance import (
    EvidenceRecord,
    LocalTestOverlaySummary,
    MinimalDiffAssessment,
    MinimalDiffVerdict,
    ReviewGate,
    ReviewRole,
)
from project_hermes.config import (
    ControlPlaneConfig,
    ControlPlaneMode,
    assert_private_directory,
)
from project_hermes.knowledge import (
    ComparisonFinding,
    ComparisonFindingCategory,
    ComparisonRecord,
    LearningStore,
    ReusableKnowledge,
    extract_reusable_knowledge,
)
from project_hermes.models import IssueTask, StrictModel, utc_now

if TYPE_CHECKING:
    from project_hermes.controller import CandidateGateResult


_PULL_REQUEST_URL = re.compile(
    r"^https://github\.com/([^/]+/[^/]+)/pull/([1-9][0-9]*)$"
)


def _sha256_json(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_sha256(value: str) -> str:
    if (
        not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError("digest must be a lowercase sha256 value")
    return value


class CiConclusion(StrEnum):
    """Normalized CI conclusion."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class CiAttribution(StrEnum):
    """Independent causal classification for a non-successful check."""

    RELATED = "related"
    FLAKE = "flake"
    MAIN_REGRESSION = "main_regression"
    UNKNOWN = "unknown"


class DependencyPrState(StrEnum):
    """State of a prerequisite repository change."""

    DRAFT = "draft"
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class PullRequestDependency(StrictModel):
    """One upstream repository change required by this candidate."""

    repository: str
    pull_request_url: str
    head_sha: str
    state: DependencyPrState
    required_state: DependencyPrState = DependencyPrState.MERGED


class CandidateChecks(StrictModel):
    """Checks evidence retained outside the production candidate file set."""

    evidence_ids: list[str] = Field(min_length=1)
    local_test_overlay: LocalTestOverlaySummary | None = None


class PullRequestCandidate(StrictModel):
    """Frozen one-repository publication candidate."""

    schema_version: Literal[
        "pull-request-candidate.v1",
        "pull-request-candidate.v2",
    ] = (
        "pull-request-candidate.v2"
    )
    candidate_id: str
    task_id: str
    repository: str
    goal_revision: int = Field(ge=0)
    base_sha: str
    head_sha: str
    code_diff_sha: str
    branch: str
    files: list[str] = Field(min_length=1)
    includes_untracked: bool
    body: str = Field(min_length=1)
    draft: bool = True
    dependencies: list[PullRequestDependency] = Field(default_factory=list)
    minimal_diff_assessment_digest: str | None = None
    checks: CandidateChecks | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("files")
    @classmethod
    def validate_files(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(values))
        for value in normalized:
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("pull-request files must be repository-relative")
        return normalized

    @model_validator(mode="after")
    def dependencies_are_cross_repository(
        self,
    ) -> "PullRequestCandidate":
        if any(
            dependency.repository == self.repository
            for dependency in self.dependencies
        ):
            raise ValueError(
                "same-repository ordering belongs in the candidate diff"
            )
        if self.schema_version == "pull-request-candidate.v2":
            if self.minimal_diff_assessment_digest is None or self.checks is None:
                raise ValueError(
                    "v2 candidates require minimal-diff and checks evidence"
                )
            _validate_sha256(self.minimal_diff_assessment_digest)
            if self.checks.local_test_overlay is not None:
                if any(
                    path in self.body
                    for path in self.checks.local_test_overlay.files
                ):
                    raise ValueError(
                        "local test overlay paths cannot enter the candidate body"
                    )
        return self

    @classmethod
    def from_assessment(
        cls,
        *,
        candidate_id: str,
        assessment: MinimalDiffAssessment,
        base_sha: str,
        head_sha: str,
        branch: str,
        body: str,
        includes_untracked: bool,
        draft: bool = True,
        dependencies: list[PullRequestDependency] | None = None,
    ) -> "PullRequestCandidate":
        """Build a candidate only from reviewer-classified production paths."""

        if assessment.verdict is not MinimalDiffVerdict.APPROVE:
            raise PermissionError(
                "candidate construction requires minimal-diff approval"
            )
        return cls(
            schema_version="pull-request-candidate.v2",
            candidate_id=candidate_id,
            task_id=assessment.task_id,
            repository=assessment.repository,
            goal_revision=assessment.goal_revision,
            base_sha=base_sha,
            head_sha=head_sha,
            code_diff_sha=assessment.code_diff_sha,
            branch=branch,
            files=[item.path for item in assessment.candidate_files],
            includes_untracked=includes_untracked,
            body=body,
            draft=draft,
            dependencies=dependencies or [],
            minimal_diff_assessment_digest=assessment.content_hash,
            checks=CandidateChecks(
                evidence_ids=assessment.correctness_evidence_ids,
                local_test_overlay=assessment.local_test_overlay,
            ),
        )

    def fingerprint(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={"created_at"},
        )
        return _sha256_json(payload).split(":", 1)[1]


class CandidateValidation(StrictModel):
    """Successful local validation tied to an exact candidate digest."""

    schema_version: Literal["candidate-validation.v1"] = (
        "candidate-validation.v1"
    )
    candidate_digest: str
    successful: bool
    evidence_ids: list[str] = Field(min_length=1)
    results: dict[str, Any] = Field(min_length=1)
    local_test_overlay_digest: str | None = None
    completed_at: datetime = Field(default_factory=utc_now)

    @field_validator("candidate_digest")
    @classmethod
    def validate_candidate_digest(cls, value: str) -> str:
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError(
                "candidate_digest must be a lowercase SHA-256 hex digest"
            )
        return value

    @field_validator("local_test_overlay_digest")
    @classmethod
    def validate_overlay_digest(cls, value: str | None) -> str | None:
        return _validate_sha256(value) if value is not None else None

    @computed_field
    @property
    def content_hash(self) -> str:
        return _sha256_json(
            self.model_dump(mode="json", exclude={"content_hash"})
        )


class CandidateLock(StrictModel):
    """Immutable approval lock that gates external target reveal."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        frozen=True,
    )

    schema_version: Literal["candidate-lock.v1"] = "candidate-lock.v1"
    lock_id: str
    task_id: str
    candidate_id: str
    candidate_digest: str
    code_diff_sha: str
    goal_revision: int = Field(ge=0)
    minimal_diff_assessment_digest: str
    validation: CandidateValidation
    review_approvals: dict[ReviewRole, str]
    locked_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def lock_is_reveal_ready(self) -> "CandidateLock":
        if self.validation.candidate_digest != self.candidate_digest:
            raise ValueError("validation belongs to another candidate digest")
        if not self.validation.successful:
            raise ValueError("candidate lock requires successful validation")
        if set(self.review_approvals) != set(ReviewRole):
            raise ValueError("candidate lock requires both reviewer approvals")
        if any(not review_id for review_id in self.review_approvals.values()):
            raise ValueError("candidate lock review identifiers cannot be empty")
        _validate_sha256(self.minimal_diff_assessment_digest)
        return self

    @computed_field
    @property
    def content_hash(self) -> str:
        return _sha256_json(
            self.model_dump(mode="json", exclude={"content_hash"})
        )

    def assert_candidate(self, candidate: PullRequestCandidate) -> None:
        if (
            candidate.candidate_id != self.candidate_id
            or candidate.task_id != self.task_id
            or candidate.code_diff_sha != self.code_diff_sha
            or candidate.goal_revision != self.goal_revision
            or candidate.minimal_diff_assessment_digest
            != self.minimal_diff_assessment_digest
            or candidate.fingerprint() != self.candidate_digest
        ):
            raise PermissionError(
                "candidate no longer matches its immutable approval lock"
            )


class CandidateLockStore(Protocol):
    """Read boundary required by post-lock oracle and comparison."""

    def get_candidate_lock(self, lock_id: str) -> CandidateLock:
        """Read one immutable candidate lock."""

    def get_minimal_diff_assessment(
        self,
        content_hash: str,
    ) -> MinimalDiffAssessment:
        """Read the independent assessment bound into the lock."""


class CandidateRevealEvidenceStore(Protocol):
    """Fresh assurance evidence required at the oracle boundary."""

    def review_gate(
        self,
        task_id: str,
        *,
        code_diff_sha: str,
        goal_revision: int,
    ) -> ReviewGate:
        """Read the exact dual-review gate."""

    def get_evidence(self, evidence_id: str) -> EvidenceRecord:
        """Read one immutable validation evidence record."""


class TargetPullRequestFile(StrictModel):
    """Ephemeral target file returned only after lock verification."""

    path: str
    status: str
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    patch: str = ""


class TargetPullRequestSnapshot(StrictModel):
    """Ephemeral target PR content; never accepted by persistence stores."""

    schema_version: Literal["target-pr-snapshot.v1"] = "target-pr-snapshot.v1"
    lock_id: str
    candidate_digest: str
    repository: str
    number: int = Field(ge=1)
    url: str
    state: str
    title: str
    body: str
    base_sha: str | None = None
    head_sha: str | None = None
    files: list[TargetPullRequestFile] = Field(default_factory=list)
    revealed_at: datetime = Field(default_factory=utc_now)

    @computed_field
    @property
    def content_digest(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={
                "lock_id",
                "candidate_digest",
                "revealed_at",
                "content_digest",
            },
        )
        return _sha256_json(payload)


TargetPullRequestFetcher = Callable[[str, int], Mapping[str, Any]]


class TargetPullRequestOracle:
    """The only target-content gateway, fail-closed on the persisted lock."""

    def __init__(
        self,
        lock_store: CandidateLockStore,
        evidence_store: CandidateRevealEvidenceStore,
        fetcher: TargetPullRequestFetcher,
    ) -> None:
        self.lock_store = lock_store
        self.evidence_store = evidence_store
        self.fetcher = fetcher

    def reveal(
        self,
        *,
        lock_id: str,
        candidate: PullRequestCandidate,
        target_pr_url: str,
    ) -> TargetPullRequestSnapshot:
        match = _PULL_REQUEST_URL.fullmatch(target_pr_url)
        if match is None:
            raise ValueError("target PR URL must be a canonical GitHub pull URL")
        lock = self.lock_store.get_candidate_lock(lock_id)
        lock.assert_candidate(candidate)
        gate = self.evidence_store.review_gate(
            lock.task_id,
            code_diff_sha=lock.code_diff_sha,
            goal_revision=lock.goal_revision,
        )
        approved, reasons = gate.evaluate()
        reviews = {review.role: review for review in gate.reviews}
        if (
            not approved
            or {
                role: review.review_id for role, review in reviews.items()
            }
            != lock.review_approvals
        ):
            detail = "; ".join(reasons) or "approval identifiers differ"
            raise PermissionError(
                "target PR access denied by dual review: " + detail
            )
        minimal_assessment = self.lock_store.get_minimal_diff_assessment(
            lock.minimal_diff_assessment_digest
        )
        if (
            minimal_assessment.verdict is not MinimalDiffVerdict.APPROVE
            or minimal_assessment.reviewer_session_id
            != reviews[ReviewRole.MINIMAL_DIFF_REVIEWER].reviewer_session_id
        ):
            raise PermissionError(
                "target PR access denied by minimal-diff review"
            )
        for evidence_id in lock.validation.evidence_ids:
            evidence = self.evidence_store.get_evidence(evidence_id)
            if (
                evidence.task_id != lock.task_id
                or evidence.code_diff_sha != lock.code_diff_sha
                or evidence.goal_revision != lock.goal_revision
                or not _validation_result_succeeded(evidence.result)
            ):
                raise PermissionError(
                    "target PR access denied by validation evidence"
                )
        repository = match.group(1)
        number = int(match.group(2))
        raw = self.fetcher(repository, number)
        files = [
            TargetPullRequestFile(
                path=str(item["path"]),
                status=str(item.get("status") or "unknown"),
                additions=int(item.get("additions") or 0),
                deletions=int(item.get("deletions") or 0),
                patch=str(item.get("patch") or ""),
            )
            for item in raw.get("files", [])
        ]
        return TargetPullRequestSnapshot(
            lock_id=lock.lock_id,
            candidate_digest=lock.candidate_digest,
            repository=repository,
            number=number,
            url=target_pr_url,
            state=str(raw.get("state") or "unknown"),
            title=str(raw.get("title") or ""),
            body=str(raw.get("body") or ""),
            base_sha=raw.get("base_sha"),
            head_sha=raw.get("head_sha"),
            files=files,
        )


class PostLockComparator:
    """Compare after reveal without exposing a candidate mutation path."""

    def __init__(
        self,
        lock_store: CandidateLockStore,
        learning_store: LearningStore,
    ) -> None:
        self.lock_store = lock_store
        self.learning_store = learning_store

    def compare(
        self,
        *,
        lock_id: str,
        candidate: PullRequestCandidate,
        target: TargetPullRequestSnapshot,
        findings: list[ComparisonFinding] | None = None,
    ) -> tuple[ComparisonRecord, ReusableKnowledge]:
        lock = self.lock_store.get_candidate_lock(lock_id)
        lock.assert_candidate(candidate)
        candidate_digest = candidate.fingerprint()
        if (
            target.lock_id != lock.lock_id
            or target.candidate_digest != candidate_digest
        ):
            raise PermissionError(
                "target snapshot belongs to another immutable candidate lock"
            )

        candidate_paths = set(candidate.files)
        target_paths = {item.path for item in target.files}
        target_has_tests = any(_is_test_path(path) for path in target_paths)
        candidate_has_overlay = bool(
            candidate.checks
            and candidate.checks.local_test_overlay is not None
        )
        sanitized_findings = list(findings or [])
        if target_has_tests and not candidate_has_overlay:
            sanitized_findings.append(
                ComparisonFinding(
                    category=ComparisonFindingCategory.VALIDATION_STRATEGY,
                    summary=(
                        "Add an explicit local regression overlay when the "
                        "reference solution demonstrates missing coverage."
                    ),
                )
            )
        if len(target_paths) > len(candidate_paths):
            sanitized_findings.append(
                ComparisonFinding(
                    category=ComparisonFindingCategory.MISSED_BOUNDARY,
                    summary=(
                        "Review adjacent compatibility boundaries when the "
                        "reference change spans more files."
                    ),
                )
            )
        if len(candidate_paths) > len(target_paths):
            sanitized_findings.append(
                ComparisonFinding(
                    category=ComparisonFindingCategory.MINIMAL_DIFF,
                    summary=(
                        "Recheck whether every production path is necessary "
                        "when the reference change has a smaller file set."
                    ),
                )
            )
        if not sanitized_findings:
            sanitized_findings.append(
                ComparisonFinding(
                    category=ComparisonFindingCategory.VALIDATION_STRATEGY,
                    summary=(
                        "Retain the successful correctness checks for similar "
                        "future issues."
                    ),
                )
            )
        if any(
            _finding_leaks_target_content(item.summary, target)
            for item in sanitized_findings
        ):
            raise ValueError(
                "comparison findings cannot quote target PR content"
            )
        record = ComparisonRecord(
            comparison_id=f"comparison-{lock.lock_id}",
            task_id=lock.task_id,
            repository=candidate.repository,
            lock_id=lock.lock_id,
            candidate_digest=candidate_digest,
            target_reference=target.url,
            target_content_digest=target.content_digest,
            candidate_file_count=len(candidate_paths),
            target_file_count=len(target_paths),
            path_overlap_count=len(candidate_paths & target_paths),
            candidate_has_local_test_overlay=candidate_has_overlay,
            target_has_tests=target_has_tests,
            findings=list(
                {
                    (item.category, item.summary): item
                    for item in sanitized_findings
                }.values()
            ),
        )
        if candidate.fingerprint() != candidate_digest:
            raise RuntimeError("post-lock comparison mutated the candidate")
        persisted = self.learning_store.put_comparison(record)
        knowledge = extract_reusable_knowledge(persisted)
        return persisted, self.learning_store.put_knowledge(knowledge)


def _is_test_path(path: str) -> bool:
    normalized = path.casefold()
    return (
        normalized.startswith(("test/", "tests/"))
        or "/test" in normalized
        or normalized.endswith(("_test.py", ".spec.ts", ".test.ts"))
    )


def _validation_result_succeeded(result: dict[str, Any]) -> bool:
    if result.get("exit_code") == 0:
        return True
    return any(
        result.get(key) is True
        for key in ("passed", "success", "fix_confirmed")
    )


def _finding_leaks_target_content(
    summary: str,
    target: TargetPullRequestSnapshot,
) -> bool:
    normalized_summary = " ".join(summary.casefold().split())
    target_values = [
        target.title,
        target.body,
        *(item.path for item in target.files),
        *(item.patch for item in target.files),
    ]
    for value in target_values:
        for line in value.splitlines():
            normalized_line = " ".join(
                line.lstrip("+- ").casefold().split()
            )
            if len(normalized_line) >= 12 and normalized_line in normalized_summary:
                return True
        target_tokens = re.findall(r"[a-z0-9]+", value.casefold())
        summary_tokens = re.findall(r"[a-z0-9]+", normalized_summary)
        if len(summary_tokens) < 3 or len(target_tokens) < 3:
            continue
        spans = {
            tuple(target_tokens[index : index + 3])
            for index in range(len(target_tokens) - 2)
        }
        if any(
            tuple(summary_tokens[index : index + 3]) in spans
            for index in range(len(summary_tokens) - 2)
        ):
            return True
    return False


class InternalCandidateLockState(StrEnum):
    """Approval and immutability state of an internal PR candidate."""

    DRAFT = "draft"
    APPROVAL_PENDING = "approval_pending"
    IMMUTABLE_APPROVED = "immutable_approved"


class InternalCandidateCheckStatus(StrEnum):
    """GitHub-style lifecycle of a projected validation check."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class InternalCandidateCheckConclusion(StrEnum):
    """Terminal result of a projected validation check."""

    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    NEUTRAL = "neutral"


class InternalPullRequestCommit(StrictModel):
    """One commit displayed in the internal candidate timeline."""

    sha: str
    message: str = Field(min_length=1)
    author_name: str
    author_email: str | None = None
    authored_at: datetime

    @field_validator("sha")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        normalized = value.lower()
        if not 7 <= len(normalized) <= 64 or any(
            character not in "0123456789abcdef"
            for character in normalized
        ):
            raise ValueError("candidate commit identifiers must be hexadecimal")
        return normalized


class InternalPullRequestFile(StrictModel):
    """One production-scope file and its immutable review diff."""

    path: str
    status: Literal["?", "A", "C", "D", "M", "R", "T", "U"]
    added: int = Field(default=0, ge=0)
    removed: int = Field(default=0, ge=0)
    diff: str
    is_binary: bool = False
    production_scope: Literal[True] = True
    necessity: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value in {"", "."}:
            raise ValueError("candidate files must be repository-relative")
        return value


class InternalPullRequestCheck(StrictModel):
    """One candidate validation check, including local-overlay evidence."""

    name: str = Field(min_length=1)
    status: InternalCandidateCheckStatus
    conclusion: InternalCandidateCheckConclusion | None = None
    summary: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    validation_overlay_digest: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def terminal_state_has_conclusion(self) -> "InternalPullRequestCheck":
        if (
            self.status is InternalCandidateCheckStatus.COMPLETED
            and self.conclusion is None
        ):
            raise ValueError("completed checks require a conclusion")
        if (
            self.status is not InternalCandidateCheckStatus.COMPLETED
            and self.conclusion is not None
        ):
            raise ValueError("non-terminal checks cannot have a conclusion")
        return self


class InternalPullRequestReview(StrictModel):
    """One independent review projected onto the candidate."""

    review_id: str
    role: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    verdict: Literal[
        "APPROVE",
        "REVISION_REQUIRED",
        "MORE_EVIDENCE_REQUIRED",
        "REJECT",
    ]
    summary: str
    findings: list[str] = Field(default_factory=list)
    submitted_at: datetime


class InternalCandidateAccountingRound(StrictModel):
    """One numeric work round shown without private model prose."""

    round_id: str
    kind: str
    role: str
    outcome: str
    duration_ms: int = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)


class InternalCandidateAccounting(StrictModel):
    """Stable accounting projection for an internal candidate."""

    wall_clock_duration_ms: int = Field(default=0, ge=0)
    active_duration_ms: int = Field(default=0, ge=0)
    estimated_llm_cost_usd: float = Field(default=0, ge=0)
    cost_complete: bool = False
    rounds: list[InternalCandidateAccountingRound] = Field(default_factory=list)


class InternalCandidateComparison(StrictModel):
    """Post-lock comparison summary that never contains reference PR code."""

    summary: str = Field(min_length=1)
    defects: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    minimality: list[str] = Field(default_factory=list)
    compared_at: datetime = Field(default_factory=utc_now)


class InternalPullRequestCandidate(StrictModel):
    """Persistent GitHub-style projection of one production candidate."""

    schema_version: Literal["internal-pr-candidate.v1"] = (
        "internal-pr-candidate.v1"
    )
    candidate_id: str
    task_id: str
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    repository: str
    base_ref: str = Field(min_length=1)
    base_sha: str
    head_ref: str = Field(min_length=1)
    head_sha: str
    commits: list[InternalPullRequestCommit] = Field(min_length=1)
    files: list[InternalPullRequestFile] = Field(min_length=1)
    checks: list[InternalPullRequestCheck] = Field(default_factory=list)
    reviews: list[InternalPullRequestReview] = Field(default_factory=list)
    accounting: InternalCandidateAccounting = Field(
        default_factory=InternalCandidateAccounting
    )
    lock_state: InternalCandidateLockState = InternalCandidateLockState.DRAFT
    lock_digest: str | None = None
    locked_by: str | None = None
    locked_at: datetime | None = None
    post_lock_comparison: InternalCandidateComparison | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("files")
    @classmethod
    def unique_files(
        cls,
        values: list[InternalPullRequestFile],
    ) -> list[InternalPullRequestFile]:
        paths = [item.path for item in values]
        if len(paths) != len(set(paths)):
            raise ValueError("candidate file paths must be unique")
        return values

    @field_validator("commits")
    @classmethod
    def unique_commits(
        cls,
        values: list[InternalPullRequestCommit],
    ) -> list[InternalPullRequestCommit]:
        shas = [item.sha for item in values]
        if len(shas) != len(set(shas)):
            raise ValueError("candidate commit identifiers must be unique")
        return values

    @field_validator("base_sha", "head_sha")
    @classmethod
    def validate_git_sha(cls, value: str) -> str:
        normalized = value.lower()
        if not 7 <= len(normalized) <= 64 or any(
            character not in "0123456789abcdef"
            for character in normalized
        ):
            raise ValueError("candidate commit identifiers must be hexadecimal")
        return normalized

    @model_validator(mode="after")
    def validate_lock_boundary(self) -> "InternalPullRequestCandidate":
        if self.updated_at < self.created_at:
            raise ValueError("candidate update cannot precede creation")
        if self.lock_state is InternalCandidateLockState.IMMUTABLE_APPROVED:
            if not self.lock_digest or not self.locked_by or self.locked_at is None:
                raise ValueError(
                    "immutable approval requires digest and lock provenance"
                )
            if self.lock_digest != self.solution_digest():
                raise ValueError("candidate lock digest does not match solution")
        elif any(
            value is not None
            for value in (self.lock_digest, self.locked_by, self.locked_at)
        ):
            raise ValueError("unapproved candidates cannot contain lock metadata")
        if (
            self.post_lock_comparison is not None
            and self.lock_state
            is not InternalCandidateLockState.IMMUTABLE_APPROVED
        ):
            raise ValueError("reference comparison requires immutable approval")
        return self

    def solution_digest(self) -> str:
        """Hash only the solution and approval evidence frozen by the lock."""

        payload = {
            "candidate_id": self.candidate_id,
            "task_id": self.task_id,
            "title": self.title,
            "body": self.body,
            "repository": self.repository,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
            "commits": [
                item.model_dump(mode="json") for item in self.commits
            ],
            "files": [item.model_dump(mode="json") for item in self.files],
            "checks": [item.model_dump(mode="json") for item in self.checks],
            "reviews": [item.model_dump(mode="json") for item in self.reviews],
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def review_input_digest(self) -> str:
        """Hash the immutable candidate material presented to reviewers."""

        payload = {
            "candidate_id": self.candidate_id,
            "task_id": self.task_id,
            "title": self.title,
            "body": self.body,
            "repository": self.repository,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
            "commits": [
                item.model_dump(mode="json") for item in self.commits
            ],
            "files": [item.model_dump(mode="json") for item in self.files],
            "checks": [item.model_dump(mode="json") for item in self.checks],
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def immutable_copy(
        self,
        *,
        approved_by: str,
        approved_at: datetime | None = None,
    ) -> "InternalPullRequestCandidate":
        """Return an approved copy tied to the exact solution digest."""

        locked_at = approved_at or utc_now()
        payload = self.model_dump()
        payload.update(
            {
                "lock_state": InternalCandidateLockState.IMMUTABLE_APPROVED,
                "lock_digest": self.solution_digest(),
                "locked_by": approved_by,
                "locked_at": locked_at,
                "updated_at": locked_at,
            }
        )
        return InternalPullRequestCandidate.model_validate(payload)


class RemoteCandidateObservation(StrictModel):
    """Remote GitHub facts read immediately before publication."""

    repository: str
    branch: str
    head_sha: str
    base_sha: str
    files: list[str]
    body_digest: str
    observed_at: datetime = Field(default_factory=utc_now)


class CiCheckRecord(StrictModel):
    """One check run tied to the candidate head."""

    check_name: str
    head_sha: str
    conclusion: CiConclusion
    attribution: CiAttribution | None = None
    investigator_session_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def non_success_has_independent_attribution(self) -> "CiCheckRecord":
        if self.conclusion in {
            CiConclusion.FAILURE,
            CiConclusion.CANCELLED,
        }:
            if (
                self.attribution is None
                or not self.investigator_session_id
                or not self.evidence_ids
            ):
                raise ValueError(
                    "failed or cancelled CI requires independent attribution"
                )
        return self


class PublicationApproval(StrictModel):
    """Human approval tied to one immutable candidate fingerprint."""

    schema_version: Literal["publication-approval.v1"] = (
        "publication-approval.v1"
    )
    candidate_id: str
    candidate_fingerprint: str
    approved_by: str
    reason: str = Field(min_length=1)
    approved_at: datetime = Field(default_factory=utc_now)


class PublicationGateResult(StrictModel):
    """Fail-closed publication decision."""

    schema_version: Literal["publication-gate-result.v1"] = (
        "publication-gate-result.v1"
    )
    approved: bool
    reasons: list[str] = Field(default_factory=list)


class PublicationGate:
    """Combine local assurance, remote facts, CI, and human approval."""

    def evaluate(
        self,
        *,
        task: IssueTask,
        candidate: PullRequestCandidate,
        candidate_gate: CandidateGateResult,
        remote: RemoteCandidateObservation,
        checks: list[CiCheckRecord],
        approval: PublicationApproval | None,
    ) -> PublicationGateResult:
        reasons: list[str] = []
        responsibilities = {
            responsibility.repository for responsibility in task.repositories
        }
        if candidate.task_id != task.task_id:
            reasons.append("candidate belongs to another task")
        if candidate.repository not in responsibilities:
            reasons.append("candidate repository is outside the locked scope")
        if candidate.goal_revision != task.revision:
            reasons.append("candidate uses a stale goal revision")
        if not candidate.includes_untracked:
            reasons.append("candidate diff does not account for untracked files")
        if not candidate_gate.approved:
            reasons.extend(candidate_gate.reasons)
        if (
            candidate_gate.repository_diff_shas.get(candidate.repository)
            != candidate.code_diff_sha
            or candidate_gate.goal_revision != candidate.goal_revision
        ):
            reasons.append("assurance gate belongs to another candidate")
        if (
            candidate_gate.repository_bases.get(candidate.repository)
            != candidate.base_sha
        ):
            reasons.append(
                "assurance gate uses another repository base commit"
            )

        if (
            remote.repository != candidate.repository
            or remote.branch != candidate.branch
            or remote.head_sha != candidate.head_sha
            or remote.base_sha != candidate.base_sha
        ):
            reasons.append("remote branch identity differs from the candidate")
        if sorted(remote.files) != sorted(candidate.files):
            reasons.append("remote pull-request file set differs from review")
        expected_body_digest = hashlib.sha256(
            candidate.body.encode("utf-8")
        ).hexdigest()
        if remote.body_digest != expected_body_digest:
            reasons.append("remote pull-request body differs from review")

        for dependency in candidate.dependencies:
            if dependency.state is not dependency.required_state:
                reasons.append(
                    f"dependency {dependency.pull_request_url} is "
                    f"{dependency.state.value}, expected "
                    f"{dependency.required_state.value}"
                )

        check_names = [check.check_name for check in checks]
        if len(check_names) != len(set(check_names)):
            reasons.append("remote CI observations contain duplicate names")
        required_checks = set(task.required_ci_checks)
        if not required_checks:
            reasons.append("locked task does not name required CI checks")
        missing_checks = required_checks - set(check_names)
        if missing_checks:
            reasons.append(
                "required CI checks are missing: "
                + ", ".join(sorted(missing_checks))
            )
        for check in checks:
            if check.head_sha != candidate.head_sha:
                reasons.append(f"CI check {check.check_name} is stale")
            if check.conclusion is CiConclusion.PENDING:
                reasons.append(f"CI check {check.check_name} is pending")
            elif check.conclusion is CiConclusion.SKIPPED:
                reasons.append(f"CI check {check.check_name} was skipped")
            elif check.conclusion in {
                CiConclusion.FAILURE,
                CiConclusion.CANCELLED,
            } and check.attribution in {
                CiAttribution.RELATED,
                CiAttribution.UNKNOWN,
            }:
                reasons.append(
                    f"CI check {check.check_name} has unresolved "
                    f"{check.attribution.value} failure"
                )

        if approval is None:
            reasons.append("publication requires human approval")
        elif (
            approval.candidate_id != candidate.candidate_id
            or approval.candidate_fingerprint != candidate.fingerprint()
        ):
            reasons.append("publication approval is stale")

        return PublicationGateResult(
            approved=not reasons,
            reasons=list(dict.fromkeys(reasons)),
        )


class InternalPullRequestCandidateStore(Protocol):
    """Persistence port for internal PR candidate projections."""

    def save(
        self,
        candidate: InternalPullRequestCandidate,
    ) -> InternalPullRequestCandidate:
        """Create or refresh a candidate without crossing its solution lock."""

    def get(self, candidate_id: str) -> InternalPullRequestCandidate:
        """Read one candidate projection."""

    def list_candidates(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[InternalPullRequestCandidate]:
        """Read candidates in most-recently-updated order."""

    def count(self) -> int:
        """Count persisted candidate projections."""

    def append_review(
        self,
        candidate_id: str,
        review: InternalPullRequestReview,
        *,
        expected_input_digest: str,
    ) -> InternalPullRequestCandidate:
        """Atomically append one independent review to frozen input."""

    def approve_after_dual_review(
        self,
        candidate_id: str,
        *,
        expected_input_digest: str,
        approved_by: str,
        approved_at: datetime | None = None,
    ) -> InternalPullRequestCandidate:
        """Lock one candidate only after both mandatory roles approve."""

    def record_comparison(
        self,
        candidate_id: str,
        comparison: InternalCandidateComparison,
    ) -> InternalPullRequestCandidate:
        """Attach the immutable candidate's post-lock comparison once."""


_SQLITE_CANDIDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_hermes_internal_pr_candidates (
    candidate_id    TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    repository      TEXT NOT NULL,
    lock_state      TEXT NOT NULL,
    candidate_json  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_hermes_internal_pr_candidates_updated
ON project_hermes_internal_pr_candidates(updated_at DESC, candidate_id);
"""

_POSTGRES_CANDIDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_hermes_internal_pr_candidates (
    candidate_id    TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    repository      TEXT NOT NULL,
    lock_state      TEXT NOT NULL,
    candidate_json  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_hermes_internal_pr_candidates_updated
ON project_hermes_internal_pr_candidates(updated_at DESC, candidate_id);
"""

_LOCK_ORDER = {
    InternalCandidateLockState.DRAFT: 0,
    InternalCandidateLockState.APPROVAL_PENDING: 1,
    InternalCandidateLockState.IMMUTABLE_APPROVED: 2,
}


class SqliteInternalPullRequestCandidateStore:
    """SQLite-backed candidate projection for local control planes."""

    _integrity_errors: tuple[type[BaseException], ...] = (
        sqlite3.IntegrityError,
    )

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        assert_private_directory(self.path.parent)
        self._lock = RLock()
        with self._connect() as connection:
            connection.executescript(_SQLITE_CANDIDATE_SCHEMA)

    def save(
        self,
        candidate: InternalPullRequestCandidate,
    ) -> InternalPullRequestCandidate:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT candidate_json
                FROM project_hermes_internal_pr_candidates
                WHERE candidate_id = ?
                """,
                (candidate.candidate_id,),
            ).fetchone()
            if row is not None:
                stored = InternalPullRequestCandidate.model_validate_json(
                    row["candidate_json"]
                )
                self._validate_update(stored, candidate)
                self._write_tx(connection, candidate, update=True)
            else:
                try:
                    self._write_tx(connection, candidate, update=False)
                except self._integrity_errors as exc:
                    raise ValueError("candidate already exists") from exc
        return candidate

    def get(self, candidate_id: str) -> InternalPullRequestCandidate:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT candidate_json
                FROM project_hermes_internal_pr_candidates
                WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown internal PR candidate: {candidate_id}")
        return InternalPullRequestCandidate.model_validate_json(
            row["candidate_json"]
        )

    def list_candidates(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[InternalPullRequestCandidate]:
        if limit < 1 or offset < 0:
            raise ValueError("candidate pagination is out of range")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT candidate_json
                FROM project_hermes_internal_pr_candidates
                ORDER BY updated_at DESC, candidate_id ASC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [
            InternalPullRequestCandidate.model_validate_json(
                row["candidate_json"]
            )
            for row in rows
        ]

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS candidate_count
                FROM project_hermes_internal_pr_candidates
                """
            ).fetchone()
        return int(row["candidate_count"])

    def append_review(
        self,
        candidate_id: str,
        review: InternalPullRequestReview,
        *,
        expected_input_digest: str,
    ) -> InternalPullRequestCandidate:
        """Append exactly one verdict per role without replacing input."""

        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT candidate_json
                FROM project_hermes_internal_pr_candidates
                WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(
                    f"unknown internal PR candidate: {candidate_id}"
                )
            stored = InternalPullRequestCandidate.model_validate_json(
                row["candidate_json"]
            )
            if (
                stored.lock_state
                is not InternalCandidateLockState.APPROVAL_PENDING
            ):
                raise ValueError(
                    "reviews require an approval-pending candidate"
                )
            if stored.review_input_digest() != expected_input_digest:
                raise ValueError("candidate review input changed")
            for existing in stored.reviews:
                if existing.review_id == review.review_id:
                    if existing != review:
                        raise ValueError("candidate review identity collision")
                    return stored
                if existing.role == review.role:
                    raise ValueError(
                        "candidate already has a review for this role"
                    )
                if existing.reviewer == review.reviewer:
                    raise ValueError(
                        "mandatory review roles require independent sessions"
                    )
            updated = stored.model_copy(
                update={
                    "reviews": [*stored.reviews, review],
                    "updated_at": max(stored.updated_at, review.submitted_at),
                }
            )
            self._write_tx(connection, updated, update=True)
            return updated

    def approve_after_dual_review(
        self,
        candidate_id: str,
        *,
        expected_input_digest: str,
        approved_by: str,
        approved_at: datetime | None = None,
    ) -> InternalPullRequestCandidate:
        """Atomically lock an approval-pending candidate after dual approval."""

        actor = approved_by.strip()
        if not actor:
            raise ValueError("dual-review approval requires provenance")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT candidate_json
                FROM project_hermes_internal_pr_candidates
                WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(
                    f"unknown internal PR candidate: {candidate_id}"
                )
            stored = InternalPullRequestCandidate.model_validate_json(
                row["candidate_json"]
            )
            if stored.review_input_digest() != expected_input_digest:
                raise ValueError("candidate review input changed")
            if (
                stored.lock_state
                is InternalCandidateLockState.IMMUTABLE_APPROVED
            ):
                return stored
            if (
                stored.lock_state
                is not InternalCandidateLockState.APPROVAL_PENDING
            ):
                raise ValueError(
                    "dual-review approval requires an approval-pending candidate"
                )

            reviews_by_role = {review.role: review for review in stored.reviews}
            required_roles = {role.value for role in ReviewRole}
            if set(reviews_by_role) != required_roles:
                raise ValueError(
                    "dual-review approval requires both mandatory roles"
                )
            if any(
                review.verdict != "APPROVE"
                for review in reviews_by_role.values()
            ):
                raise ValueError(
                    "dual-review approval requires unanimous approval"
                )
            if len(
                {review.reviewer for review in reviews_by_role.values()}
            ) != len(required_roles):
                raise ValueError(
                    "dual-review approval requires independent reviewers"
                )

            approved = stored.immutable_copy(
                approved_by=actor,
                approved_at=approved_at,
            )
            self._write_tx(connection, approved, update=True)
            return approved

    def record_comparison(
        self,
        candidate_id: str,
        comparison: InternalCandidateComparison,
    ) -> InternalPullRequestCandidate:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT candidate_json
                FROM project_hermes_internal_pr_candidates
                WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(
                    f"unknown internal PR candidate: {candidate_id}"
                )
            stored = InternalPullRequestCandidate.model_validate_json(
                row["candidate_json"]
            )
            if (
                stored.lock_state
                is not InternalCandidateLockState.IMMUTABLE_APPROVED
            ):
                raise PermissionError(
                    "reference PR withheld until immutable approval"
                )
            if stored.post_lock_comparison is not None:
                if stored.post_lock_comparison != comparison:
                    raise ValueError("candidate comparison is already recorded")
                return stored
            payload = stored.model_dump()
            payload.update(
                {
                    "post_lock_comparison": comparison,
                    "updated_at": max(
                        stored.updated_at,
                        utc_now(),
                        comparison.compared_at,
                    ),
                }
            )
            updated = InternalPullRequestCandidate.model_validate(payload)
            self._write_tx(connection, updated, update=True)
            return updated

    @staticmethod
    def _validate_update(
        stored: InternalPullRequestCandidate,
        candidate: InternalPullRequestCandidate,
    ) -> None:
        if _LOCK_ORDER[candidate.lock_state] < _LOCK_ORDER[stored.lock_state]:
            raise ValueError("candidate lock state cannot regress")
        if (
            stored.lock_state
            is InternalCandidateLockState.IMMUTABLE_APPROVED
        ):
            if (
                candidate.lock_state
                is not InternalCandidateLockState.IMMUTABLE_APPROVED
                or candidate.lock_digest != stored.lock_digest
                or candidate.solution_digest() != stored.solution_digest()
            ):
                raise ValueError("immutable candidate solution cannot change")
            if (
                stored.post_lock_comparison is not None
                and candidate.post_lock_comparison
                != stored.post_lock_comparison
            ):
                raise ValueError("candidate comparison cannot be replaced")
        if candidate.updated_at < stored.updated_at:
            raise ValueError("candidate projection cannot move backward")

    @staticmethod
    def _write_tx(
        connection: Any,
        candidate: InternalPullRequestCandidate,
        *,
        update: bool,
    ) -> None:
        values = (
            candidate.task_id,
            candidate.repository,
            candidate.lock_state.value,
            candidate.model_dump_json(),
            candidate.created_at.isoformat(),
            candidate.updated_at.isoformat(),
            candidate.candidate_id,
        )
        if update:
            connection.execute(
                """
                UPDATE project_hermes_internal_pr_candidates
                SET task_id = ?, repository = ?, lock_state = ?,
                    candidate_json = ?, created_at = ?, updated_at = ?
                WHERE candidate_id = ?
                """,
                values,
            )
            return
        connection.execute(
            """
            INSERT INTO project_hermes_internal_pr_candidates (
                task_id, repository, lock_state, candidate_json,
                created_at, updated_at, candidate_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )

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


class PostgresInternalPullRequestCandidateStore(
    SqliteInternalPullRequestCandidateStore
):
    """PostgreSQL-backed candidate projection for production control planes."""

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
                "PostgreSQL candidates require the project-hermes "
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
            for statement in _POSTGRES_CANDIDATE_SCHEMA.split(";"):
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


def open_internal_pull_request_candidate_store(
    config: ControlPlaneConfig,
) -> InternalPullRequestCandidateStore:
    """Open candidate persistence alongside the configured control plane."""

    if config.mode is ControlPlaneMode.SQLITE:
        return SqliteInternalPullRequestCandidateStore(config.sqlite_path)
    if config.mode is ControlPlaneMode.POSTGRES:
        if not config.database_url:
            raise ValueError("postgres mode requires database_url")
        return PostgresInternalPullRequestCandidateStore(config.database_url)
    raise NotImplementedError(
        f"{config.mode.value} control-plane mode requires a registered "
        "InternalPullRequestCandidateStore adapter"
    )
