"""Evidence, completion, review, and knowledge lifecycle contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping

from pydantic import Field, computed_field, field_validator, model_validator

from project_hermes.models import ProjectRole, StrictModel, utc_now


class CompletionLayer(StrEnum):
    """The nine independently auditable completion layers."""

    IMPLEMENTED = "implemented"
    GATE_VERIFIED = "gate_verified"
    OPERATOR_CORRECT = "operator_correct"
    OPERATOR_PERF = "operator_perf"
    SERVING_SELECTED = "serving_selected"
    SERVING_EXECUTED = "serving_executed"
    FINAL_E2E = "final_e2e"
    PACKAGED = "packaged"
    CI_REVIEWED = "ci_reviewed"


class CompletionStatus(StrEnum):
    """State of one completion layer."""

    PENDING = "PENDING"
    SATISFIED = "SATISFIED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    INVALIDATED = "INVALIDATED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ReviewRole(StrEnum):
    """Independent mandatory review roles."""

    COMPLETION_AUDITOR = "completion-auditor"
    MINIMAL_DIFF_REVIEWER = "minimal-diff-reviewer"


class ReviewVerdict(StrEnum):
    """Fail-closed review verdicts."""

    APPROVE = "APPROVE"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    MORE_EVIDENCE_REQUIRED = "MORE_EVIDENCE_REQUIRED"
    REJECT = "REJECT"


class ReviewProgress(StrEnum):
    """Completion-auditor judgment against the frozen issue goals."""

    IMPROVED = "improved"
    NO_PROGRESS = "no_progress"
    REGRESSED = "regressed"


class ReviewEscalationStatus(StrEnum):
    """One-time automatic model escalation state for a goal revision."""

    NONE = "none"
    REQUESTED = "requested"
    BLOCKED = "blocked"


class FindingSeverity(StrEnum):
    """Finding severity used by mandatory review gates."""

    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class KnowledgeOutcome(StrEnum):
    """Knowledge category selected from the task's terminal outcome."""

    POSITIVE = "positive"
    NEGATIVE = "negative"


class CleanupStatus(StrEnum):
    """Crash-recoverable cleanup tombstone state."""

    PREPARED = "PREPARED"
    COMPLETED = "COMPLETED"


class MinimalDiffVerdict(StrEnum):
    """Independent correctness-first minimal-diff decision."""

    APPROVE = "APPROVE"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    REJECT = "REJECT"


def _validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or value != path.as_posix()
    ):
        raise ValueError("paths must be normalized and repository-relative")
    return value


def _validate_sha256(value: str) -> str:
    if (
        not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError("digest must be a lowercase sha256 value")
    return value


class LocalTestOverlayFile(StrictModel):
    """One explicitly local-only validation file."""

    path: str
    content: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_relative_path(value)

    @computed_field
    @property
    def line_count(self) -> int:
        return len(self.content.splitlines())


class LocalTestOverlaySummary(StrictModel):
    """Checks-safe projection of a separately stored test overlay."""

    overlay_id: str
    content_digest: str
    files: dict[str, int] = Field(min_length=1)
    total_lines: int = Field(ge=0)
    command: list[str] = Field(min_length=1)
    passed: bool
    results: dict[str, Any]

    @field_validator("content_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("files")
    @classmethod
    def validate_files(cls, values: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for path, line_count in values.items():
            normalized[_validate_relative_path(path)] = line_count
        if len(normalized) != len(values):
            raise ValueError("local test overlay paths must be unique")
        return normalized

    @model_validator(mode="after")
    def totals_match_files(self) -> "LocalTestOverlaySummary":
        if self.total_lines != sum(self.files.values()):
            raise ValueError("overlay total_lines must equal its file metrics")
        return self


class LocalTestOverlay(StrictModel):
    """Content-addressed local tests plus their validation result."""

    schema_version: Literal["local-test-overlay.v1"] = "local-test-overlay.v1"
    overlay_id: str
    task_id: str
    code_diff_sha: str
    files: list[LocalTestOverlayFile] = Field(min_length=1)
    command: list[str] = Field(min_length=1)
    passed: bool
    results: dict[str, Any]
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("code_diff_sha")
    @classmethod
    def validate_diff_sha(cls, value: str) -> str:
        if len(value) < 7 or any(
            character not in "0123456789abcdef"
            for character in value.lower()
        ):
            raise ValueError("code_diff_sha must be a hexadecimal git object id")
        return value.lower()

    @model_validator(mode="after")
    def files_are_unique(self) -> "LocalTestOverlay":
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("local test overlay paths must be unique")
        return self

    @classmethod
    def from_files(
        cls,
        *,
        overlay_id: str,
        task_id: str,
        code_diff_sha: str,
        files: Mapping[str, str],
        command: list[str],
        passed: bool,
        results: dict[str, Any],
    ) -> "LocalTestOverlay":
        """Build an overlay without inferring test scope from path names."""

        return cls(
            overlay_id=overlay_id,
            task_id=task_id,
            code_diff_sha=code_diff_sha,
            files=[
                LocalTestOverlayFile(path=path, content=content)
                for path, content in sorted(files.items())
            ],
            command=command,
            passed=passed,
            results=results,
        )

    @computed_field
    @property
    def content_digest(self) -> str:
        payload = [
            {"path": item.path, "content": item.content}
            for item in sorted(self.files, key=lambda item: item.path)
        ]
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def summary(self) -> LocalTestOverlaySummary:
        return LocalTestOverlaySummary(
            overlay_id=self.overlay_id,
            content_digest=self.content_digest,
            files={item.path: item.line_count for item in self.files},
            total_lines=sum(item.line_count for item in self.files),
            command=self.command,
            passed=self.passed,
            results=self.results,
        )


class CandidateFileAssessment(StrictModel):
    """Line metrics and necessity for one production candidate path."""

    path: str
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    production_code_lines: int = Field(ge=0)
    necessity: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_relative_path(value)

    @model_validator(mode="after")
    def production_lines_fit_change(self) -> "CandidateFileAssessment":
        if self.production_code_lines > self.additions + self.deletions:
            raise ValueError(
                "production_code_lines cannot exceed total changed lines"
            )
        return self


class DependencyChange(StrictModel):
    """One necessary dependency-manifest or lockfile edit."""

    path: str
    package: str = Field(min_length=1)
    change: str = Field(min_length=1)
    necessity: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_relative_path(value)


class MinimalDiffAssessment(StrictModel):
    """Frozen correctness-first assessment of one exact candidate diff."""

    schema_version: Literal["minimal-diff-assessment.v1"] = (
        "minimal-diff-assessment.v1"
    )
    assessment_id: str
    task_id: str
    repository: str
    code_diff_sha: str
    goal_revision: int = Field(ge=0)
    candidate_files: list[CandidateFileAssessment] = Field(min_length=1)
    dependency_changes: list[DependencyChange] = Field(default_factory=list)
    local_test_overlay: LocalTestOverlaySummary | None = None
    correctness_evidence_ids: list[str] = Field(min_length=1)
    correctness_passed: bool
    unnecessary_paths: list[str] = Field(default_factory=list)
    reviewer_session_id: str
    reviewer_explanation: str = Field(min_length=1)
    verdict: MinimalDiffVerdict
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("code_diff_sha")
    @classmethod
    def validate_diff_sha(cls, value: str) -> str:
        if len(value) < 7 or any(
            character not in "0123456789abcdef"
            for character in value.lower()
        ):
            raise ValueError("code_diff_sha must be a hexadecimal git object id")
        return value.lower()

    @field_validator("unnecessary_paths")
    @classmethod
    def validate_unnecessary_paths(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_validate_relative_path(value) for value in values))

    @model_validator(mode="after")
    def approval_is_correct_and_minimal(self) -> "MinimalDiffAssessment":
        candidate_paths = [item.path for item in self.candidate_files]
        if len(candidate_paths) != len(set(candidate_paths)):
            raise ValueError("candidate file assessments must be unique")
        candidate_path_set = set(candidate_paths)
        unknown_dependencies = {
            item.path for item in self.dependency_changes
        } - candidate_path_set
        if unknown_dependencies:
            raise ValueError(
                "dependency changes must name candidate files: "
                + ", ".join(sorted(unknown_dependencies))
            )
        overlay_paths = (
            set(self.local_test_overlay.files)
            if self.local_test_overlay is not None
            else set()
        )
        overlap = candidate_path_set & overlay_paths
        if overlap:
            raise ValueError(
                "local test overlay files cannot enter the candidate: "
                + ", ".join(sorted(overlap))
            )
        if self.verdict is MinimalDiffVerdict.APPROVE:
            if not self.correctness_passed:
                raise ValueError(
                    "minimal-diff approval requires successful correctness evidence"
                )
            if self.local_test_overlay is not None and not self.local_test_overlay.passed:
                raise ValueError(
                    "minimal-diff approval requires a successful local test overlay"
                )
            if self.unnecessary_paths:
                raise ValueError(
                    "minimal-diff approval cannot retain avoidable edits"
                )
        return self

    @computed_field
    @property
    def candidate_line_count(self) -> int:
        return sum(
            item.additions + item.deletions for item in self.candidate_files
        )

    @computed_field
    @property
    def production_code_line_count(self) -> int:
        return sum(item.production_code_lines for item in self.candidate_files)

    @computed_field
    @property
    def content_hash(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={"content_hash"},
        )
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


class EvidenceRecord(StrictModel):
    """Immutable evidence bound to exact code, goal, and environment."""

    schema_version: Literal["evidence-record.v3"] = "evidence-record.v3"
    evidence_id: str
    task_id: str
    node_id: str
    path_id: str
    repository: str
    completion_layer: CompletionLayer
    producer_role: ProjectRole
    base_sha: str
    code_diff_sha: str
    goal_revision: int = Field(ge=0)
    image_digest: str | None = None
    dependency_versions: dict[str, str] = Field(default_factory=dict)
    hardware: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any]
    command: list[str] = Field(min_length=1)
    result: dict[str, Any]
    named_baseline: str | None = None
    reference: str | None = None
    tolerance: str | None = None
    artifact_digests: list[str] = Field(default_factory=list)
    log_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("base_sha", "code_diff_sha")
    @classmethod
    def validate_diff_sha(cls, value: str) -> str:
        if len(value) < 7 or any(character not in "0123456789abcdef" for character in value.lower()):
            raise ValueError("code_diff_sha must be a hexadecimal git object id")
        return value.lower()

    @field_validator("artifact_digests")
    @classmethod
    def validate_artifact_digests(cls, values: list[str]) -> list[str]:
        for value in values:
            if (
                not value.startswith("sha256:")
                or len(value) != 71
                or any(
                    character not in "0123456789abcdef"
                    for character in value[7:]
                )
            ):
                raise ValueError(
                    "artifact digests must be lowercase sha256 values"
                )
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def performance_evidence_names_comparison(self) -> "EvidenceRecord":
        if self.completion_layer is CompletionLayer.OPERATOR_PERF and (
            not self.named_baseline
            or not self.reference
            or not self.tolerance
        ):
            raise ValueError(
                "performance evidence requires a named baseline, "
                "reference, and tolerance"
            )
        return self

    @computed_field
    @property
    def content_hash(self) -> str:
        """Stable hash excluding the derived content_hash field itself."""

        payload = self.model_dump(
            mode="json",
            exclude={"content_hash"},
        )
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


class CompletionEntry(StrictModel):
    """Status and evidence for one layer."""

    layer: CompletionLayer
    status: CompletionStatus = CompletionStatus.PENDING
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str | None = None
    code_diff_sha: str | None = None
    goal_revision: int | None = Field(default=None, ge=0)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def satisfied_entries_require_evidence(self) -> "CompletionEntry":
        if self.status is CompletionStatus.SATISFIED:
            if not self.evidence_ids:
                raise ValueError("satisfied completion entries require evidence")
            if self.code_diff_sha is None or self.goal_revision is None:
                raise ValueError(
                    "satisfied completion entries require code and goal provenance"
                )
        if self.status in {
            CompletionStatus.BLOCKED,
            CompletionStatus.FAILED,
            CompletionStatus.NOT_APPLICABLE,
        } and not self.rationale:
            raise ValueError(f"{self.status} completion entries require a rationale")
        return self


class GoalPathCompletion(StrictModel):
    """Completion projection for one locked goal path and repository."""

    path_id: str
    repository: str
    required_layers: list[CompletionLayer] = Field(min_length=1)
    entries: dict[CompletionLayer, CompletionEntry] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_entries(self) -> "GoalPathCompletion":
        if len(self.required_layers) != len(set(self.required_layers)):
            raise ValueError("required completion layers must be unique")
        for layer, entry in self.entries.items():
            if layer is not entry.layer:
                raise ValueError(
                    "completion entry keys must match entry layers"
                )
        return self

    def can_close(self, *, code_diff_sha: str, goal_revision: int) -> bool:
        for layer in self.required_layers:
            entry = self.entries.get(layer)
            if entry is None or entry.status is not CompletionStatus.SATISFIED:
                return False
            if (
                entry.code_diff_sha != code_diff_sha
                or entry.goal_revision != goal_revision
            ):
                return False
        return True


class CompletionMatrix(StrictModel):
    """Per-goal, per-repository nine-layer completion projection."""

    schema_version: Literal["completion-matrix.v3"] = "completion-matrix.v3"
    task_id: str
    required_layers: list[CompletionLayer] = Field(
        default_factory=lambda: list(CompletionLayer)
    )
    entries: dict[CompletionLayer, CompletionEntry] = Field(default_factory=dict)
    goal_paths: dict[str, GoalPathCompletion] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_entries(self) -> "CompletionMatrix":
        if len(self.required_layers) != len(set(self.required_layers)):
            raise ValueError("required completion layers must be unique")
        if self.goal_paths and (self.required_layers or self.entries):
            raise ValueError(
                "per-goal matrices cannot mix aggregate layers or entries"
            )
        for layer, entry in self.entries.items():
            if layer is not entry.layer:
                raise ValueError("completion entry keys must match entry layers")
        for path_id, completion in self.goal_paths.items():
            if path_id != completion.path_id:
                raise ValueError(
                    "goal path keys must match completion path identifiers"
                )
        return self

    def update(
        self,
        entry: CompletionEntry,
        *,
        path_id: str | None = None,
    ) -> None:
        """Replace one completion entry."""

        if self.goal_paths:
            if path_id is None:
                raise ValueError(
                    "path_id is required for a per-goal completion matrix"
                )
            try:
                path = self.goal_paths[path_id]
            except KeyError as exc:
                raise KeyError(f"unknown goal path: {path_id}") from exc
            if entry.layer not in path.required_layers:
                raise ValueError(
                    "completion layer is not required by this goal path"
                )
            path.entries[entry.layer] = entry
        else:
            if path_id is not None:
                raise ValueError(
                    "aggregate completion matrices do not accept path_id"
                )
            if entry.layer not in self.required_layers:
                raise ValueError(
                    "completion layer is not required by this matrix"
                )
            self.entries[entry.layer] = entry
        self.updated_at = utc_now()

    def invalidate_for_diff(
        self,
        code_diff_sha: str,
        *,
        layers: set[CompletionLayer] | None = None,
    ) -> list[CompletionLayer]:
        """Invalidate stale satisfied evidence after code changes."""

        invalidated: list[CompletionLayer] = []
        targets: list[dict[CompletionLayer, CompletionEntry]] = [
            self.entries
        ]
        targets.extend(
            completion.entries for completion in self.goal_paths.values()
        )
        for entries in targets:
            for layer, entry in list(entries.items()):
                if layers is not None and layer not in layers:
                    continue
                if (
                    entry.status is CompletionStatus.SATISFIED
                    and entry.code_diff_sha != code_diff_sha
                ):
                    entries[layer] = entry.model_copy(
                        update={
                            "status": CompletionStatus.INVALIDATED,
                            "rationale": (
                                "Code diff changed after evidence was recorded."
                            ),
                            "updated_at": utc_now(),
                        }
                    )
                    invalidated.append(layer)
        if invalidated:
            self.updated_at = utc_now()
        return invalidated

    def can_close(self, *, code_diff_sha: str, goal_revision: int) -> bool:
        """Return whether every required layer is fresh and satisfied."""

        if self.goal_paths:
            return all(
                completion.can_close(
                    code_diff_sha=code_diff_sha,
                    goal_revision=goal_revision,
                )
                for completion in self.goal_paths.values()
            )
        for layer in self.required_layers:
            entry = self.entries.get(layer)
            if entry is None or entry.status is not CompletionStatus.SATISFIED:
                return False
            if (
                entry.code_diff_sha != code_diff_sha
                or entry.goal_revision != goal_revision
            ):
                return False
        return True

    def evidence_bindings(
        self,
    ) -> list[tuple[str | None, str | None, CompletionEntry]]:
        """Return entries with their locked path and repository identity."""

        if self.goal_paths:
            return [
                (path.path_id, path.repository, entry)
                for path in self.goal_paths.values()
                for entry in path.entries.values()
            ]
        return [
            (None, None, entry) for entry in self.entries.values()
        ]

    def fingerprint(self) -> str:
        """Return a stable digest for frozen reviewer input."""

        encoded = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


class ReviewFinding(StrictModel):
    """Actionable independent-review finding."""

    finding_id: str
    severity: FindingSeverity
    title: str
    description: str
    path: str | None = None
    line: int | None = Field(default=None, ge=1)
    resolved: bool = False
    resolution_evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def resolved_findings_require_evidence(self) -> "ReviewFinding":
        if self.resolved and not self.resolution_evidence_ids:
            raise ValueError("resolved findings require resolution evidence")
        if not self.resolved and self.resolution_evidence_ids:
            raise ValueError("unresolved findings cannot claim resolution evidence")
        return self


class RepositoryReviewInput(StrictModel):
    """Frozen diff identity and location for one candidate repository."""

    schema_version: Literal["repository-review-input.v1"] = (
        "repository-review-input.v1"
    )
    repository: str
    absolute_repository_path: str
    base_sha: str
    branch: str
    code_diff_sha: str
    complete_diff_ref: str
    includes_untracked: bool

    @field_validator("base_sha", "code_diff_sha")
    @classmethod
    def validate_git_sha(cls, value: str) -> str:
        if len(value) < 7 or any(
            character not in "0123456789abcdef"
            for character in value.lower()
        ):
            raise ValueError("review commit identifiers must be hexadecimal")
        return value.lower()

    @model_validator(mode="after")
    def input_is_complete_and_absolute(self) -> "RepositoryReviewInput":
        from pathlib import Path

        if not Path(self.absolute_repository_path).is_absolute():
            raise ValueError("review repository path must be absolute")
        if not self.includes_untracked:
            raise ValueError(
                "review input must account for untracked candidate files"
            )
        return self


class ReviewPacket(StrictModel):
    """Frozen read-only input shared by mandatory reviewer roles."""

    schema_version: Literal["review-packet.v1", "review-packet.v2"] = (
        "review-packet.v2"
    )
    packet_id: str
    task_id: str
    repository: str
    absolute_repository_path: str
    base_sha: str
    branch: str
    code_diff_sha: str
    goal_revision: int = Field(ge=0)
    complete_diff_ref: str
    includes_untracked: bool
    repository_inputs: dict[str, RepositoryReviewInput] = Field(
        default_factory=dict
    )
    must_preserve: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    completion_matrix_digest: str
    target_hardware: dict[str, Any]
    evidence_ids: list[str] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("base_sha", "code_diff_sha")
    @classmethod
    def validate_git_sha(cls, value: str) -> str:
        if len(value) < 7 or any(
            character not in "0123456789abcdef"
            for character in value.lower()
        ):
            raise ValueError("review commit identifiers must be hexadecimal")
        return value.lower()

    @field_validator("completion_matrix_digest")
    @classmethod
    def validate_completion_digest(cls, value: str) -> str:
        if (
            not value.startswith("sha256:")
            or len(value) != 71
            or any(
                character not in "0123456789abcdef"
                for character in value[7:]
            )
        ):
            raise ValueError(
                "completion matrix digest must be a lowercase sha256 value"
            )
        return value

    @model_validator(mode="after")
    def packet_is_complete_and_absolute(self) -> "ReviewPacket":
        from pathlib import Path

        if not Path(self.absolute_repository_path).is_absolute():
            raise ValueError("review repository path must be absolute")
        if not self.includes_untracked:
            raise ValueError(
                "review packet must account for untracked candidate files"
            )
        primary = RepositoryReviewInput(
            repository=self.repository,
            absolute_repository_path=self.absolute_repository_path,
            base_sha=self.base_sha,
            branch=self.branch,
            code_diff_sha=self.code_diff_sha,
            complete_diff_ref=self.complete_diff_ref,
            includes_untracked=self.includes_untracked,
        )
        if not self.repository_inputs:
            object.__setattr__(
                self,
                "repository_inputs",
                {self.repository: primary},
            )
        for repository, review_input in self.repository_inputs.items():
            if repository != review_input.repository:
                raise ValueError(
                    "review input keys must match repository identities"
                )
        primary_input = self.repository_inputs.get(self.repository)
        if primary_input is None or any(
            getattr(primary_input, field) != getattr(primary, field)
            for field in (
                "repository",
                "absolute_repository_path",
                "base_sha",
                "branch",
                "complete_diff_ref",
                "includes_untracked",
            )
        ):
            raise ValueError(
                "primary review input must match the packet repository fields"
            )
        return self

    @computed_field
    @property
    def content_hash(self) -> str:
        excluded = {"content_hash"}
        if self.schema_version == "review-packet.v1":
            excluded.add("repository_inputs")
        payload = self.model_dump(
            mode="json",
            exclude=excluded,
        )
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


class ReviewRecord(StrictModel):
    """One reviewer verdict tied to an exact candidate diff."""

    schema_version: Literal["review-record.v3", "review-record.v4"] = (
        "review-record.v4"
    )
    review_id: str
    task_id: str
    role: ReviewRole
    review_cycle: int | None = Field(default=None, ge=1)
    reviewer_session_id: str
    packet_id: str
    packet_digest: str
    code_diff_sha: str
    goal_revision: int = Field(ge=0)
    verdict: ReviewVerdict
    findings: list[ReviewFinding] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    summary: str
    progress: ReviewProgress | None = None
    progress_explanation: str | None = Field(default=None, min_length=1)
    assessed_goal_path_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("packet_digest")
    @classmethod
    def validate_packet_digest(cls, value: str) -> str:
        if (
            not value.startswith("sha256:")
            or len(value) != 71
            or any(
                character not in "0123456789abcdef"
                for character in value[7:]
            )
        ):
            raise ValueError(
                "review packet digest must be a lowercase sha256 value"
            )
        return value

    @model_validator(mode="after")
    def approval_is_fail_closed(self) -> "ReviewRecord":
        blocking = {
            FindingSeverity.MEDIUM,
            FindingSeverity.HIGH,
            FindingSeverity.CRITICAL,
        }
        if self.verdict is ReviewVerdict.APPROVE and any(
            finding.severity in blocking
            for finding in self.findings
        ):
            raise ValueError(
                "approved reviews cannot contain blocking findings"
            )
        if self.verdict is ReviewVerdict.APPROVE and not self.evidence_ids:
            raise ValueError("approved reviews require independent evidence")
        if self.schema_version == "review-record.v4":
            if self.review_cycle is None:
                raise ValueError("v4 reviews require a review_cycle")
            if self.role is ReviewRole.COMPLETION_AUDITOR:
                if (
                    self.progress is None
                    or not self.progress_explanation
                    or not self.progress_explanation.strip()
                    or not self.assessed_goal_path_ids
                ):
                    raise ValueError(
                        "completion-auditor reviews require progress, an "
                        "explanation, and assessed goal paths"
                    )
                if len(self.assessed_goal_path_ids) != len(
                    set(self.assessed_goal_path_ids)
                ):
                    raise ValueError(
                        "assessed goal path ids must be unique"
                    )
                if (
                    self.verdict is ReviewVerdict.APPROVE
                    and self.progress is not ReviewProgress.IMPROVED
                ):
                    raise ValueError(
                        "approved completion reviews must report improvement"
                    )
            elif (
                self.progress is not None
                or self.progress_explanation is not None
                or self.assessed_goal_path_ids
            ):
                raise ValueError(
                    "only the completion auditor may assess issue progress"
                )
        return self


class ReviewEscalationState(StrictModel):
    """Durable one-time escalation decision for one frozen goal revision."""

    schema_version: Literal["review-escalation-state.v1"] = (
        "review-escalation-state.v1"
    )
    task_id: str
    goal_revision: int = Field(ge=0)
    status: ReviewEscalationStatus = ReviewEscalationStatus.NONE
    trigger_review_cycle: int | None = Field(default=None, ge=1)
    trigger_review_ids: list[str] = Field(default_factory=list)
    reviewer_explanation: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def active_state_has_trigger(self) -> "ReviewEscalationState":
        if self.status is not ReviewEscalationStatus.NONE and (
            self.trigger_review_cycle is None
            or not self.trigger_review_ids
            or not self.reviewer_explanation
            or not self.reviewer_explanation.strip()
        ):
            raise ValueError(
                "requested or blocked escalation requires review provenance"
            )
        return self


class ReviewGate(StrictModel):
    """Mandatory dual-review gate."""

    schema_version: Literal["review-gate.v3"] = "review-gate.v3"
    task_id: str
    code_diff_sha: str
    goal_revision: int = Field(ge=0)
    reviews: list[ReviewRecord] = Field(default_factory=list)

    def evaluate(self) -> tuple[bool, list[str]]:
        """Return a fail-closed gate decision and concrete reasons."""

        reasons: list[str] = []
        by_role: dict[ReviewRole, list[ReviewRecord]] = {
            role: [] for role in ReviewRole
        }
        for review in self.reviews:
            if review.task_id != self.task_id:
                reasons.append(f"review {review.review_id} belongs to another task")
                continue
            if (
                review.code_diff_sha != self.code_diff_sha
                or review.goal_revision != self.goal_revision
            ):
                reasons.append(f"review {review.review_id} is stale")
                continue
            by_role[review.role].append(review)

        packet_digests = {
            review.packet_digest
            for records in by_role.values()
            for review in records
        }
        if len(packet_digests) > 1:
            reasons.append(
                "mandatory reviewers did not receive one frozen packet"
            )
        for role in ReviewRole:
            records = by_role[role]
            if len(records) != 1:
                reasons.append(
                    f"exactly one fresh {role.value} review is required"
                )
                continue
            if records[0].verdict is not ReviewVerdict.APPROVE:
                reasons.append(
                    f"{role.value} returned {records[0].verdict.value}"
                )
        return not reasons, reasons


class KnowledgeCandidate(StrictModel):
    """Extracted knowledge awaiting independent validation."""

    schema_version: Literal["knowledge-candidate.v1"] = "knowledge-candidate.v1"
    candidate_id: str
    task_id: str
    outcome: KnowledgeOutcome
    title: str
    body: str
    source_evidence_ids: list[str] = Field(min_length=1)
    code_diff_sha: str
    created_at: datetime = Field(default_factory=utc_now)


class KnowledgeCommit(StrictModel):
    """Committed knowledge artifact that can be probed independently."""

    schema_version: Literal["knowledge-commit.v1"] = "knowledge-commit.v1"
    commit_id: str
    task_id: str
    outcome: KnowledgeOutcome
    content_digest: str
    storage_ref: str
    validated_by: str
    committed_at: datetime = Field(default_factory=utc_now)


class CleanupTombstone(StrictModel):
    """Minimal audit row left after task-private state is deleted."""

    schema_version: Literal[
        "cleanup-tombstone.v1",
        "cleanup-tombstone.v2",
    ] = "cleanup-tombstone.v2"
    task_id: str
    terminal_outcome: str
    knowledge_commit_id: str
    status: CleanupStatus = CleanupStatus.COMPLETED
    planned_paths: list[str] = Field(default_factory=list)
    deleted_paths: list[str]
    retained_refs: list[str] = Field(default_factory=list)
    prepared_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def cleanup_state_is_consistent(self) -> "CleanupTombstone":
        if self.status is CleanupStatus.PREPARED:
            if not self.planned_paths:
                raise ValueError("prepared cleanup requires planned paths")
            if self.deleted_paths or self.completed_at is not None:
                raise ValueError(
                    "prepared cleanup cannot claim completed deletion"
                )
        elif self.completed_at is None:
            raise ValueError("completed cleanup requires completed_at")
        return self
