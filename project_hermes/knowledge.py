"""Two-phase knowledge commit and task-private cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterator, Literal, Protocol
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from project_hermes.assurance import (
    CleanupStatus,
    CleanupTombstone,
    KnowledgeCandidate,
    KnowledgeCommit,
    KnowledgeOutcome,
)
from project_hermes.config import assert_private_directory
from project_hermes.models import StrictModel, utc_now
from project_hermes.redaction import redact_data


class ComparisonFindingCategory(StrEnum):
    """Reusable, code-free comparison dimensions."""

    DEFECT_CLASS = "defect_class"
    MISSED_BOUNDARY = "missed_boundary"
    VALIDATION_STRATEGY = "validation_strategy"
    MINIMAL_DIFF = "minimal_diff"


def _validate_reusable_text(value: str) -> str:
    if (
        "\n" in value
        or "\r" in value
        or "```" in value
        or any(character in value for character in "`{}[]=\\")
    ):
        raise ValueError("reusable findings must be concise prose, not code")
    normalized = value.casefold()
    prohibited = (
        "chain of thought",
        "private reasoning",
        "target patch",
        "target code",
        "code snippet",
    )
    if any(marker in normalized for marker in prohibited):
        raise ValueError(
            "reusable findings cannot contain target code or private reasoning"
        )
    return value


class ComparisonFinding(StrictModel):
    """One sanitized structural lesson from post-lock comparison."""

    category: ComparisonFindingCategory
    summary: str = Field(min_length=1, max_length=500)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _validate_reusable_text(value)


class ComparisonRecord(StrictModel):
    """Persistable comparison projection with no target implementation."""

    schema_version: Literal["comparison-record.v1"] = "comparison-record.v1"
    comparison_id: str
    task_id: str
    repository: str
    lock_id: str
    candidate_digest: str
    target_reference: str
    target_content_digest: str
    candidate_file_count: int = Field(ge=0)
    target_file_count: int = Field(ge=0)
    path_overlap_count: int = Field(ge=0)
    candidate_has_local_test_overlay: bool
    target_has_tests: bool
    findings: list[ComparisonFinding] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)

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

    @field_validator("target_content_digest")
    @classmethod
    def validate_target_digest(cls, value: str) -> str:
        if (
            not value.startswith("sha256:")
            or len(value) != 71
            or any(
                character not in "0123456789abcdef"
                for character in value[7:]
            )
        ):
            raise ValueError(
                "target_content_digest must be a lowercase sha256 value"
            )
        return value


class ReusableKnowledge(StrictModel):
    """Sanitized knowledge extracted without target PR implementation data."""

    schema_version: Literal["reusable-knowledge.v1"] = "reusable-knowledge.v1"
    knowledge_id: str
    task_id: str
    repository: str
    source_comparison_id: str
    defect_classes: list[str] = Field(default_factory=list)
    missed_boundaries: list[str] = Field(default_factory=list)
    validation_strategies: list[str] = Field(default_factory=list)
    minimal_diff_lessons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator(
        "defect_classes",
        "missed_boundaries",
        "validation_strategies",
        "minimal_diff_lessons",
    )
    @classmethod
    def validate_lessons(cls, values: list[str]) -> list[str]:
        return list(
            dict.fromkeys(_validate_reusable_text(value) for value in values)
        )

    @model_validator(mode="after")
    def contains_a_lesson(self) -> "ReusableKnowledge":
        if not any(
            (
                self.defect_classes,
                self.missed_boundaries,
                self.validation_strategies,
                self.minimal_diff_lessons,
            )
        ):
            raise ValueError("reusable knowledge requires at least one lesson")
        return self


class LearningStore(Protocol):
    """Durable comparison and reusable-knowledge boundary."""

    def put_comparison(self, record: ComparisonRecord) -> ComparisonRecord:
        """Persist an immutable sanitized comparison."""

    def put_knowledge(self, knowledge: ReusableKnowledge) -> ReusableKnowledge:
        """Persist an immutable reusable lesson set."""


_LEARNING_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS project_hermes_comparisons (
    comparison_id          TEXT PRIMARY KEY,
    task_id                TEXT NOT NULL,
    repository             TEXT NOT NULL,
    lock_id                TEXT NOT NULL UNIQUE,
    candidate_digest       TEXT NOT NULL,
    comparison_json        TEXT NOT NULL,
    created_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_hermes_reusable_knowledge (
    knowledge_id           TEXT PRIMARY KEY,
    task_id                TEXT NOT NULL,
    repository             TEXT NOT NULL,
    source_comparison_id   TEXT NOT NULL UNIQUE,
    knowledge_json         TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    FOREIGN KEY(source_comparison_id)
        REFERENCES project_hermes_comparisons(comparison_id)
);

CREATE INDEX IF NOT EXISTS idx_project_hermes_knowledge_repository
ON project_hermes_reusable_knowledge(repository, created_at);
"""


class SqliteLearningStore:
    """Focused SQLite persistence for post-lock comparison and learning."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        assert_private_directory(self.path.parent)
        self._lock = RLock()
        with self._connect() as connection:
            connection.executescript(_LEARNING_SCHEMA)

    def put_comparison(self, record: ComparisonRecord) -> ComparisonRecord:
        payload = record.model_dump(mode="json")
        self._require_sanitized(payload)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT comparison_json
                FROM project_hermes_comparisons
                WHERE comparison_id = ?
                """,
                (record.comparison_id,),
            ).fetchone()
            if existing is not None:
                stored = ComparisonRecord.model_validate_json(
                    existing["comparison_json"]
                )
                if stored != record:
                    raise ValueError(
                        "comparison ids are immutable and cannot be overwritten"
                    )
                return stored
            try:
                connection.execute(
                    """
                    INSERT INTO project_hermes_comparisons (
                        comparison_id, task_id, repository, lock_id,
                        candidate_digest, comparison_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.comparison_id,
                        record.task_id,
                        record.repository,
                        record.lock_id,
                        record.candidate_digest,
                        record.model_dump_json(),
                        record.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    "a comparison already exists for this candidate lock"
                ) from exc
        return record

    def get_comparison(self, comparison_id: str) -> ComparisonRecord:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT comparison_json
                FROM project_hermes_comparisons
                WHERE comparison_id = ?
                """,
                (comparison_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown comparison: {comparison_id}")
        return ComparisonRecord.model_validate_json(row["comparison_json"])

    def put_knowledge(self, knowledge: ReusableKnowledge) -> ReusableKnowledge:
        payload = knowledge.model_dump(mode="json")
        self._require_sanitized(payload)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT knowledge_json
                FROM project_hermes_reusable_knowledge
                WHERE knowledge_id = ?
                """,
                (knowledge.knowledge_id,),
            ).fetchone()
            if existing is not None:
                stored = ReusableKnowledge.model_validate_json(
                    existing["knowledge_json"]
                )
                if stored != knowledge:
                    raise ValueError(
                        "knowledge ids are immutable and cannot be overwritten"
                    )
                return stored
            try:
                connection.execute(
                    """
                    INSERT INTO project_hermes_reusable_knowledge (
                        knowledge_id, task_id, repository,
                        source_comparison_id, knowledge_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        knowledge.knowledge_id,
                        knowledge.task_id,
                        knowledge.repository,
                        knowledge.source_comparison_id,
                        knowledge.model_dump_json(),
                        knowledge.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    "knowledge requires one existing comparison source"
                ) from exc
        return knowledge

    def retrieve(
        self,
        repository: str,
        *,
        limit: int = 20,
    ) -> list[ReusableKnowledge]:
        if limit < 1:
            raise ValueError("knowledge retrieval limit must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT knowledge_json
                FROM project_hermes_reusable_knowledge
                WHERE repository = ?
                ORDER BY created_at DESC, knowledge_id ASC
                LIMIT ?
                """,
                (repository, limit),
            ).fetchall()
        return [
            ReusableKnowledge.model_validate_json(row["knowledge_json"])
            for row in rows
        ]

    @staticmethod
    def _require_sanitized(payload: dict[str, Any]) -> None:
        if redact_data(payload) != payload:
            raise ValueError("learning records contain credential-shaped data")
        serialized = json.dumps(payload, ensure_ascii=True).casefold()
        for marker in (
            '"patch"',
            '"body"',
            '"title"',
            "chain of thought",
            "private reasoning",
        ):
            if marker in serialized:
                raise ValueError(
                    "learning records cannot retain target code or private reasoning"
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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


def extract_reusable_knowledge(record: ComparisonRecord) -> ReusableKnowledge:
    """Project only sanitized findings into repository-level knowledge."""

    grouped: dict[ComparisonFindingCategory, list[str]] = {
        category: [] for category in ComparisonFindingCategory
    }
    for finding in record.findings:
        grouped[finding.category].append(finding.summary)
    return ReusableKnowledge(
        knowledge_id=f"knowledge-{record.comparison_id}",
        task_id=record.task_id,
        repository=record.repository,
        source_comparison_id=record.comparison_id,
        defect_classes=grouped[ComparisonFindingCategory.DEFECT_CLASS],
        missed_boundaries=grouped[ComparisonFindingCategory.MISSED_BOUNDARY],
        validation_strategies=grouped[
            ComparisonFindingCategory.VALIDATION_STRATEGY
        ],
        minimal_diff_lessons=grouped[
            ComparisonFindingCategory.MINIMAL_DIFF
        ],
    )


class CleanupPlan(StrictModel):
    """Validated deletion boundary after knowledge is durable."""

    task_id: str
    terminal_outcome: str
    allowed_root: Path
    delete_paths: list[Path] = Field(min_length=1)
    tombstone_path: Path
    retained_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_paths(self) -> "CleanupPlan":
        if not self.allowed_root.is_absolute():
            raise ValueError("allowed_root must be absolute")
        root = self.allowed_root.resolve()
        if not self.tombstone_path.is_absolute():
            raise ValueError("tombstone_path must be absolute")
        tombstone = Path(os.path.abspath(self.tombstone_path))
        if not tombstone.is_relative_to(root):
            raise ValueError("tombstone_path must be inside allowed_root")
        if not tombstone.parent.resolve().is_relative_to(root):
            raise ValueError("tombstone parent escapes allowed_root")
        for candidate in self.delete_paths:
            if not candidate.is_absolute():
                raise ValueError("cleanup paths must be absolute")
            path = Path(os.path.abspath(candidate))
            if path == root:
                raise ValueError("cleanup cannot delete allowed_root itself")
            if not path.is_relative_to(root):
                raise ValueError(
                    f"cleanup path escapes allowed_root: {candidate}"
                )
            if not path.parent.resolve().is_relative_to(root):
                raise ValueError(
                    f"cleanup parent escapes allowed_root: {candidate}"
                )
            if path.exists() and not path.is_symlink():
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    raise ValueError(
                        f"cleanup path escapes allowed_root: {candidate}"
                    )
            if tombstone.is_relative_to(path):
                raise ValueError(
                    "tombstone_path cannot be inside a path scheduled for deletion"
                )
        return self


class KnowledgeRepository(Protocol):
    """Durable knowledge storage boundary."""

    def commit(
        self,
        candidates: list[KnowledgeCandidate],
        *,
        validated_by: str,
    ) -> KnowledgeCommit:
        """Commit a validated batch."""

    def probe(self, commit: KnowledgeCommit) -> bool:
        """Independently read and verify a committed batch."""


class JsonKnowledgeRepository:
    """Content-addressed local repository for development and tests."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        assert_private_directory(self.root)

    def commit(
        self,
        candidates: list[KnowledgeCandidate],
        *,
        validated_by: str,
    ) -> KnowledgeCommit:
        if not candidates:
            raise ValueError("knowledge commit requires at least one candidate")
        task_ids = {candidate.task_id for candidate in candidates}
        outcomes = {candidate.outcome for candidate in candidates}
        if len(task_ids) != 1 or len(outcomes) != 1:
            raise ValueError(
                "one knowledge commit cannot mix tasks or outcomes"
            )

        payload = [
            candidate.model_dump(mode="json")
            for candidate in sorted(
                candidates,
                key=lambda item: item.candidate_id,
            )
        ]
        if redact_data(payload) != payload:
            raise ValueError(
                "knowledge candidates contain credential-shaped data"
            )
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        task_id = next(iter(task_ids))
        outcome = next(iter(outcomes))
        task_component = hashlib.sha256(
            task_id.encode("utf-8")
        ).hexdigest()[:32]
        directory = self.root / outcome.value / task_component
        assert_private_directory(directory)
        path = directory / f"{digest.split(':', 1)[1]}.json"
        _atomic_private_write(path, encoded + b"\n")
        return KnowledgeCommit(
            commit_id=f"knowledge-{uuid4().hex}",
            task_id=task_id,
            outcome=outcome,
            content_digest=digest,
            storage_ref=str(path),
            validated_by=validated_by,
        )

    def probe(self, commit: KnowledgeCommit) -> bool:
        path = Path(commit.storage_ref)
        if not path.is_file() or not path.resolve().is_relative_to(self.root):
            return False
        digest = "sha256:" + hashlib.sha256(path.read_bytes().strip()).hexdigest()
        return digest == commit.content_digest


class KnowledgeLifecycle:
    """Enforce extract -> validate -> commit -> probe -> delete."""

    _TERMINAL_OUTCOMES = {
        "merged": KnowledgeOutcome.POSITIVE,
        "closed": KnowledgeOutcome.NEGATIVE,
        "rejected": KnowledgeOutcome.NEGATIVE,
        "failed": KnowledgeOutcome.NEGATIVE,
    }

    def __init__(self, repository: KnowledgeRepository) -> None:
        self.repository = repository

    def finalize(
        self,
        candidates: list[KnowledgeCandidate],
        *,
        validate: Callable[[KnowledgeCandidate], bool],
        validated_by: str,
        cleanup: CleanupPlan,
    ) -> CleanupTombstone:
        """Commit validated knowledge before deleting any task-private state."""

        expected_outcome = self._TERMINAL_OUTCOMES.get(
            cleanup.terminal_outcome.lower()
        )
        if expected_outcome is None:
            raise ValueError(
                "cleanup requires a terminal PR outcome: "
                "merged, closed, rejected, or failed"
            )
        if not candidates:
            raise ValueError("cleanup requires extracted knowledge")
        if any(candidate.task_id != cleanup.task_id for candidate in candidates):
            raise ValueError("knowledge candidate belongs to a different task")
        if any(candidate.outcome is not expected_outcome for candidate in candidates):
            raise ValueError(
                "knowledge outcome does not match the terminal PR outcome"
            )
        rejected = [
            candidate.candidate_id
            for candidate in candidates
            if not validate(candidate)
        ]
        if rejected:
            raise PermissionError(
                "knowledge validation failed: " + ", ".join(rejected)
            )

        commit = self.repository.commit(
            candidates,
            validated_by=validated_by,
        )
        if not self.repository.probe(commit):
            raise RuntimeError(
                "knowledge commit could not be independently verified"
            )

        planned_paths = [
            str(path) for path in self._validated_cleanup_paths(cleanup)
        ]
        prepared = CleanupTombstone(
            task_id=cleanup.task_id,
            terminal_outcome=cleanup.terminal_outcome.lower(),
            knowledge_commit_id=commit.commit_id,
            status=CleanupStatus.PREPARED,
            planned_paths=planned_paths,
            deleted_paths=[],
            retained_refs=cleanup.retained_refs,
            completed_at=None,
        )
        self._write_tombstone(cleanup.tombstone_path, prepared)
        deleted = self._delete_private_state(cleanup)
        tombstone = CleanupTombstone.model_validate(
            prepared.model_copy(
                update={
                    "status": CleanupStatus.COMPLETED,
                    "deleted_paths": deleted,
                    "completed_at": utc_now(),
                }
            ).model_dump()
        )
        self._write_tombstone(cleanup.tombstone_path, tombstone)
        return tombstone

    @staticmethod
    def _validated_cleanup_paths(cleanup: CleanupPlan) -> list[Path]:
        root = cleanup.allowed_root.resolve()
        validated: list[Path] = []
        for candidate in cleanup.delete_paths:
            if not candidate.is_absolute():
                raise PermissionError(
                    f"cleanup path must be absolute: {candidate}"
                )
            path = Path(os.path.abspath(candidate))
            if path == root or not path.is_relative_to(root):
                raise PermissionError(f"refusing unsafe cleanup path: {path}")
            resolved_parent = path.parent.resolve()
            if not resolved_parent.is_relative_to(root):
                raise PermissionError(f"refusing unsafe cleanup path: {path}")
            if path.exists() and not path.is_symlink():
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    raise PermissionError(
                        f"refusing unsafe cleanup path: {path}"
                    )
            validated.append(path)
        return validated

    @classmethod
    def _delete_private_state(cls, cleanup: CleanupPlan) -> list[str]:
        validated = cls._validated_cleanup_paths(cleanup)

        deleted: list[str] = []
        for path in validated:
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)
            deleted.append(str(path))
        return deleted

    @staticmethod
    def _write_tombstone(
        path: Path,
        tombstone: CleanupTombstone,
    ) -> None:
        assert_private_directory(path.parent)
        payload = (
            json.dumps(
                tombstone.model_dump(mode="json"),
                sort_keys=True,
                indent=2,
                ensure_ascii=True,
            )
            + "\n"
        )
        _atomic_private_write(path, payload.encode("utf-8"))


def _atomic_private_write(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
