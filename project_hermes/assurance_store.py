"""Durable evidence, completion-matrix, and review projections."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Protocol

from project_hermes.assurance import (
    CompletionMatrix,
    EvidenceRecord,
    LocalTestOverlay,
    MinimalDiffAssessment,
    ReviewEscalationState,
    ReviewEscalationStatus,
    ReviewGate,
    ReviewPacket,
    ReviewRecord,
)
from project_hermes.config import (
    ControlPlaneConfig,
    ControlPlaneMode,
    assert_private_directory,
)
from project_hermes.models import utc_now
from project_hermes.publication import CandidateLock
from project_hermes.redaction import redact_data
from project_hermes.review_resolution import ReviewResolution

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS project_hermes_evidence (
    evidence_id      TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    completion_layer TEXT NOT NULL,
    code_diff_sha    TEXT NOT NULL,
    goal_revision    INTEGER NOT NULL,
    content_hash     TEXT NOT NULL,
    evidence_json    TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_hermes_evidence_candidate
ON project_hermes_evidence(task_id, code_diff_sha, goal_revision);

CREATE TABLE IF NOT EXISTS project_hermes_completion (
    task_id          TEXT PRIMARY KEY,
    matrix_json      TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_hermes_reviews (
    review_id        TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    review_role      TEXT NOT NULL,
    code_diff_sha    TEXT NOT NULL,
    goal_revision    INTEGER NOT NULL,
    review_cycle     INTEGER,
    progress_assessment TEXT,
    progress_explanation TEXT,
    review_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE(task_id, review_role, code_diff_sha, goal_revision)
);

CREATE TABLE IF NOT EXISTS project_hermes_review_packets (
    packet_id        TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    code_diff_sha    TEXT NOT NULL,
    goal_revision    INTEGER NOT NULL,
    content_hash     TEXT NOT NULL UNIQUE,
    packet_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_hermes_review_resolutions (
    resolution_id    TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    review_id        TEXT NOT NULL,
    finding_id       TEXT NOT NULL,
    resolution_json  TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE(review_id, finding_id)
);

CREATE INDEX IF NOT EXISTS idx_project_hermes_resolutions_task
ON project_hermes_review_resolutions(task_id, review_id);

CREATE TABLE IF NOT EXISTS project_hermes_review_escalations (
    task_id          TEXT NOT NULL,
    goal_revision    INTEGER NOT NULL,
    status           TEXT NOT NULL,
    state_json       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY(task_id, goal_revision)
);
"""


class AssuranceStore(Protocol):
    """Persistence port consumed by candidate assurance gates."""

    def put_evidence(self, evidence: EvidenceRecord) -> EvidenceRecord:
        """Persist immutable evidence."""

    def get_evidence(self, evidence_id: str) -> EvidenceRecord:
        """Read immutable evidence by identifier."""

    def put_completion_matrix(
        self,
        matrix: CompletionMatrix,
    ) -> CompletionMatrix:
        """Persist the current completion projection."""

    def get_completion_matrix(self, task_id: str) -> CompletionMatrix:
        """Read or initialize a completion projection."""

    def add_review(self, review: ReviewRecord) -> ReviewRecord:
        """Persist one immutable independent review."""

    def get_review(self, review_id: str) -> ReviewRecord:
        """Read one immutable review."""

    def review_history(
        self,
        task_id: str,
        *,
        goal_revision: int,
    ) -> list[ReviewRecord]:
        """Read ordered review records for one frozen goal revision."""

    def add_review_resolution(
        self,
        resolution: ReviewResolution,
    ) -> ReviewResolution:
        """Persist one immutable disposition of a reviewer finding."""

    def review_resolutions(
        self,
        task_id: str,
        *,
        review_id: str | None = None,
    ) -> list[ReviewResolution]:
        """Read finding resolutions for one task or review."""

    def get_review_escalation(
        self,
        task_id: str,
        *,
        goal_revision: int,
    ) -> ReviewEscalationState:
        """Read the one-time model escalation state."""

    def put_review_escalation(
        self,
        state: ReviewEscalationState,
    ) -> ReviewEscalationState:
        """Advance the one-time model escalation state."""

    def put_review_packet(self, packet: ReviewPacket) -> ReviewPacket:
        """Persist one immutable frozen reviewer input."""

    def get_review_packet(self, packet_id: str) -> ReviewPacket:
        """Read one frozen reviewer input."""

    def review_gate(
        self,
        task_id: str,
        *,
        code_diff_sha: str,
        goal_revision: int,
    ) -> ReviewGate:
        """Read fresh reviews for one candidate."""


class CandidateAssuranceStore(Protocol):
    """Focused persistence for overlays, minimality, and immutable locks."""

    def put_test_overlay(self, overlay: LocalTestOverlay) -> LocalTestOverlay:
        """Persist local tests separately by their content digest."""

    def get_test_overlay(self, overlay_id: str) -> LocalTestOverlay:
        """Read one local validation overlay and result."""

    def put_minimal_diff_assessment(
        self,
        assessment: MinimalDiffAssessment,
    ) -> MinimalDiffAssessment:
        """Persist one immutable independent minimal-diff assessment."""

    def get_minimal_diff_assessment(
        self,
        content_hash: str,
    ) -> MinimalDiffAssessment:
        """Read one assessment by its content digest."""

    def put_candidate_lock(self, lock: CandidateLock) -> CandidateLock:
        """Persist one immutable reveal gate."""

    def get_candidate_lock(self, lock_id: str) -> CandidateLock:
        """Read one immutable reveal gate."""


_CANDIDATE_ASSURANCE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS project_hermes_test_overlay_content (
    content_digest  TEXT PRIMARY KEY,
    files_json      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_hermes_test_overlay_runs (
    overlay_id      TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    code_diff_sha   TEXT NOT NULL,
    content_digest  TEXT NOT NULL,
    overlay_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY(content_digest)
        REFERENCES project_hermes_test_overlay_content(content_digest)
);

CREATE TABLE IF NOT EXISTS project_hermes_minimal_diff_assessments (
    assessment_id   TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    code_diff_sha   TEXT NOT NULL,
    goal_revision   INTEGER NOT NULL,
    content_hash    TEXT NOT NULL UNIQUE,
    assessment_json TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(task_id, code_diff_sha, goal_revision)
);

CREATE TABLE IF NOT EXISTS project_hermes_candidate_locks (
    lock_id          TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL UNIQUE,
    candidate_id     TEXT NOT NULL UNIQUE,
    candidate_digest TEXT NOT NULL UNIQUE,
    content_hash     TEXT NOT NULL UNIQUE,
    lock_json        TEXT NOT NULL,
    locked_at        TEXT NOT NULL
);
"""


class SqliteCandidateAssuranceStore:
    """SQLite store for candidate-only assurance milestones."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        assert_private_directory(self.path.parent)
        self._lock = RLock()
        with self._connect() as connection:
            connection.executescript(_CANDIDATE_ASSURANCE_SCHEMA)

    def put_test_overlay(self, overlay: LocalTestOverlay) -> LocalTestOverlay:
        payload = overlay.model_dump(
            mode="json",
            exclude={"content_digest"},
        )
        if redact_data(payload) != payload:
            raise ValueError("local test overlay contains credential-shaped data")
        files_json = json.dumps(
            [
                item.model_dump(
                    mode="json",
                    exclude_computed_fields=True,
                )
                for item in overlay.files
            ],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        metadata_json = overlay.model_dump_json(
            exclude={"files", "content_digest"}
        )
        with self._transaction() as connection:
            content = connection.execute(
                """
                SELECT files_json
                FROM project_hermes_test_overlay_content
                WHERE content_digest = ?
                """,
                (overlay.content_digest,),
            ).fetchone()
            if content is not None and content["files_json"] != files_json:
                raise ValueError("overlay content digest collision")
            if content is None:
                connection.execute(
                    """
                    INSERT INTO project_hermes_test_overlay_content (
                        content_digest, files_json
                    ) VALUES (?, ?)
                    """,
                    (overlay.content_digest, files_json),
                )
            existing = connection.execute(
                """
                SELECT overlay_json
                FROM project_hermes_test_overlay_runs
                WHERE overlay_id = ?
                """,
                (overlay.overlay_id,),
            ).fetchone()
            if existing is not None:
                stored = self._load_overlay_tx(connection, overlay.overlay_id)
                if stored != overlay:
                    raise ValueError(
                        "overlay ids are immutable and cannot be overwritten"
                    )
                return stored
            connection.execute(
                """
                INSERT INTO project_hermes_test_overlay_runs (
                    overlay_id, task_id, code_diff_sha, content_digest,
                    overlay_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    overlay.overlay_id,
                    overlay.task_id,
                    overlay.code_diff_sha,
                    overlay.content_digest,
                    metadata_json,
                    overlay.created_at.isoformat(),
                ),
            )
        return overlay

    def get_test_overlay(self, overlay_id: str) -> LocalTestOverlay:
        with self._connect() as connection:
            return self._load_overlay_tx(connection, overlay_id)

    @staticmethod
    def _load_overlay_tx(
        connection: sqlite3.Connection,
        overlay_id: str,
    ) -> LocalTestOverlay:
        row = connection.execute(
            """
            SELECT runs.overlay_json, content.files_json
            FROM project_hermes_test_overlay_runs AS runs
            JOIN project_hermes_test_overlay_content AS content
              ON content.content_digest = runs.content_digest
            WHERE runs.overlay_id = ?
            """,
            (overlay_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown local test overlay: {overlay_id}")
        payload = json.loads(row["overlay_json"])
        payload["files"] = json.loads(row["files_json"])
        return LocalTestOverlay.model_validate(payload)

    def put_minimal_diff_assessment(
        self,
        assessment: MinimalDiffAssessment,
    ) -> MinimalDiffAssessment:
        if assessment.local_test_overlay is not None:
            overlay = self.get_test_overlay(
                assessment.local_test_overlay.overlay_id
            )
            if overlay.summary() != assessment.local_test_overlay:
                raise ValueError(
                    "minimal-diff assessment uses stale overlay evidence"
                )
        payload = assessment.model_dump(
            mode="json",
            exclude={"content_hash"},
        )
        if redact_data(payload) != payload:
            raise ValueError(
                "minimal-diff assessment contains credential-shaped data"
            )
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT assessment_json
                FROM project_hermes_minimal_diff_assessments
                WHERE assessment_id = ?
                """,
                (assessment.assessment_id,),
            ).fetchone()
            if existing is not None:
                stored = MinimalDiffAssessment.model_validate_json(
                    existing["assessment_json"]
                )
                if stored != assessment:
                    raise ValueError(
                        "assessment ids are immutable and cannot be overwritten"
                    )
                return stored
            try:
                connection.execute(
                    """
                    INSERT INTO project_hermes_minimal_diff_assessments (
                        assessment_id, task_id, code_diff_sha, goal_revision,
                        content_hash, assessment_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assessment.assessment_id,
                        assessment.task_id,
                        assessment.code_diff_sha,
                        assessment.goal_revision,
                        assessment.content_hash,
                        assessment.model_dump_json(
                            exclude_computed_fields=True
                        ),
                        assessment.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    "an assessment already exists for this exact candidate"
                ) from exc
        return assessment

    def get_minimal_diff_assessment(
        self,
        content_hash: str,
    ) -> MinimalDiffAssessment:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT assessment_json
                FROM project_hermes_minimal_diff_assessments
                WHERE content_hash = ?
                """,
                (content_hash,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown minimal-diff assessment: {content_hash}")
        return MinimalDiffAssessment.model_validate_json(
            row["assessment_json"]
        )

    def put_candidate_lock(self, lock: CandidateLock) -> CandidateLock:
        assessment = self.get_minimal_diff_assessment(
            lock.minimal_diff_assessment_digest
        )
        if (
            assessment.task_id != lock.task_id
            or assessment.code_diff_sha != lock.code_diff_sha
            or assessment.goal_revision != lock.goal_revision
        ):
            raise ValueError(
                "candidate lock does not match its minimal-diff assessment"
            )
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT lock_json
                FROM project_hermes_candidate_locks
                WHERE lock_id = ?
                """,
                (lock.lock_id,),
            ).fetchone()
            if existing is not None:
                stored = CandidateLock.model_validate_json(
                    existing["lock_json"]
                )
                if stored != lock:
                    raise ValueError(
                        "candidate locks are immutable and cannot be overwritten"
                    )
                return stored
            try:
                connection.execute(
                    """
                    INSERT INTO project_hermes_candidate_locks (
                        lock_id, task_id, candidate_id, candidate_digest,
                        content_hash, lock_json, locked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lock.lock_id,
                        lock.task_id,
                        lock.candidate_id,
                        lock.candidate_digest,
                        lock.content_hash,
                        lock.model_dump_json(exclude_computed_fields=True),
                        lock.locked_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    "task or candidate already has an immutable lock"
                ) from exc
        return lock

    def get_candidate_lock(self, lock_id: str) -> CandidateLock:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT lock_json
                FROM project_hermes_candidate_locks
                WHERE lock_id = ?
                """,
                (lock_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown candidate lock: {lock_id}")
        return CandidateLock.model_validate_json(row["lock_json"])

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


class SqliteAssuranceStore:
    """Immutable evidence and reviews with mutable completion projection."""

    _integrity_errors: tuple[type[BaseException], ...] = (
        sqlite3.IntegrityError,
    )

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        assert_private_directory(self.path.parent)
        self._lock = RLock()
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            self._migrate_review_columns(connection)

    def put_evidence(self, evidence: EvidenceRecord) -> EvidenceRecord:
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT content_hash FROM project_hermes_evidence
                WHERE evidence_id = ?
                """,
                (evidence.evidence_id,),
            ).fetchone()
            if existing is not None:
                if existing["content_hash"] != evidence.content_hash:
                    raise ValueError(
                        "evidence ids are immutable and cannot be overwritten"
                    )
                return evidence
            connection.execute(
                """
                INSERT INTO project_hermes_evidence (
                    evidence_id, task_id, completion_layer, code_diff_sha,
                    goal_revision, content_hash, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.task_id,
                    evidence.completion_layer.value,
                    evidence.code_diff_sha,
                    evidence.goal_revision,
                    evidence.content_hash,
                    evidence.model_dump_json(exclude_computed_fields=True),
                    evidence.created_at.isoformat(),
                ),
            )
        return evidence

    def get_evidence(self, evidence_id: str) -> EvidenceRecord:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT evidence_json FROM project_hermes_evidence
                WHERE evidence_id = ?
                """,
                (evidence_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown evidence: {evidence_id}")
        return EvidenceRecord.model_validate_json(row["evidence_json"])

    def put_completion_matrix(
        self,
        matrix: CompletionMatrix,
    ) -> CompletionMatrix:
        with self._transaction() as connection:
            self._lock_completion_tx(connection, matrix.task_id)
            existing_row = connection.execute(
                """
                SELECT matrix_json FROM project_hermes_completion
                WHERE task_id = ?
                """,
                (matrix.task_id,),
            ).fetchone()
            if existing_row is not None:
                existing = CompletionMatrix.model_validate_json(
                    existing_row["matrix_json"]
                )
                if _completion_contract(existing) != _completion_contract(
                    matrix
                ):
                    raise ValueError(
                        "completion matrix cannot change locked goal paths"
                    )
            connection.execute(
                """
                INSERT INTO project_hermes_completion (
                    task_id, matrix_json, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    matrix_json = excluded.matrix_json,
                    updated_at = excluded.updated_at
                """,
                (
                    matrix.task_id,
                    matrix.model_dump_json(),
                    matrix.updated_at.isoformat(),
                ),
            )
        return matrix

    def _lock_completion_tx(
        self,
        connection: Any,
        task_id: str,
    ) -> None:
        """Acquire any adapter-specific completion projection lock."""

        del connection, task_id

    def get_completion_matrix(self, task_id: str) -> CompletionMatrix:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT matrix_json FROM project_hermes_completion
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            matrix = CompletionMatrix(task_id=task_id)
            self.put_completion_matrix(matrix)
            return matrix
        return CompletionMatrix.model_validate_json(row["matrix_json"])

    def add_review(self, review: ReviewRecord) -> ReviewRecord:
        packet = self.get_review_packet(review.packet_id)
        if (
            packet.content_hash != review.packet_digest
            or packet.task_id != review.task_id
            or packet.code_diff_sha != review.code_diff_sha
            or packet.goal_revision != review.goal_revision
        ):
            raise ValueError(
                "review does not match its frozen review packet"
            )
        with self._transaction() as connection:
            if review.review_cycle is not None:
                rows = connection.execute(
                    """
                    SELECT review_json FROM project_hermes_reviews
                    WHERE task_id = ? AND goal_revision = ?
                    ORDER BY review_cycle ASC, created_at ASC
                    """,
                    (review.task_id, review.goal_revision),
                ).fetchall()
                history = [
                    ReviewRecord.model_validate_json(row["review_json"])
                    for row in rows
                ]
                same_candidate = [
                    record
                    for record in history
                    if record.code_diff_sha == review.code_diff_sha
                ]
                if same_candidate:
                    expected_cycle = same_candidate[0].review_cycle
                else:
                    expected_cycle = (
                        max(
                            (
                                record.review_cycle or 0
                                for record in history
                            ),
                            default=0,
                        )
                        + 1
                    )
                if review.review_cycle != expected_cycle:
                    raise ValueError(
                        f"review_cycle must be {expected_cycle} for this "
                        "candidate"
                    )
            try:
                connection.execute(
                    """
                    INSERT INTO project_hermes_reviews (
                        review_id, task_id, review_role, code_diff_sha,
                        goal_revision, review_cycle, progress_assessment,
                        progress_explanation, review_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review.review_id,
                        review.task_id,
                        review.role.value,
                        review.code_diff_sha,
                        review.goal_revision,
                        review.review_cycle,
                        review.progress.value if review.progress else None,
                        review.progress_explanation,
                        review.model_dump_json(),
                        review.created_at.isoformat(),
                    ),
                )
            except self._integrity_errors as exc:
                raise ValueError(
                    "a review role already submitted a verdict for this candidate"
                ) from exc
        return review

    def get_review(self, review_id: str) -> ReviewRecord:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT review_json FROM project_hermes_reviews
                WHERE review_id = ?
                """,
                (review_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown review: {review_id}")
        return ReviewRecord.model_validate_json(row["review_json"])

    def review_history(
        self,
        task_id: str,
        *,
        goal_revision: int,
    ) -> list[ReviewRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT review_json FROM project_hermes_reviews
                WHERE task_id = ? AND goal_revision = ?
                ORDER BY review_cycle ASC, created_at ASC, review_role ASC
                """,
                (task_id, goal_revision),
            ).fetchall()
        return [
            ReviewRecord.model_validate_json(row["review_json"])
            for row in rows
        ]

    def add_review_resolution(
        self,
        resolution: ReviewResolution,
    ) -> ReviewResolution:
        review = self.get_review(resolution.review_id)
        if (
            review.task_id != resolution.task_id
            or review.code_diff_sha != resolution.reviewed_diff_sha
            or review.goal_revision != resolution.goal_revision
        ):
            raise ValueError("resolution does not match its frozen review")
        if resolution.finding_id not in {
            finding.finding_id for finding in review.findings
        }:
            raise ValueError("resolution references an unknown finding")
        for evidence_id in resolution.evidence_ids:
            evidence = self.get_evidence(evidence_id)
            if evidence.task_id != resolution.task_id:
                raise ValueError(
                    "resolution evidence belongs to another task"
                )
        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO project_hermes_review_resolutions (
                        resolution_id, task_id, review_id, finding_id,
                        resolution_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolution.resolution_id,
                        resolution.task_id,
                        resolution.review_id,
                        resolution.finding_id,
                        resolution.model_dump_json(),
                        resolution.decided_at.isoformat(),
                    ),
                )
            except self._integrity_errors as exc:
                raise ValueError(
                    "a finding already has a review resolution"
                ) from exc
        return resolution

    def review_resolutions(
        self,
        task_id: str,
        *,
        review_id: str | None = None,
    ) -> list[ReviewResolution]:
        query = """
            SELECT resolution_json
            FROM project_hermes_review_resolutions
            WHERE task_id = ?
        """
        parameters: tuple[Any, ...] = (task_id,)
        if review_id is not None:
            query += " AND review_id = ?"
            parameters = (task_id, review_id)
        query += " ORDER BY created_at ASC, resolution_id ASC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            ReviewResolution.model_validate_json(row["resolution_json"])
            for row in rows
        ]

    def get_review_escalation(
        self,
        task_id: str,
        *,
        goal_revision: int,
    ) -> ReviewEscalationState:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT state_json
                FROM project_hermes_review_escalations
                WHERE task_id = ? AND goal_revision = ?
                """,
                (task_id, goal_revision),
            ).fetchone()
        if row is None:
            return ReviewEscalationState(
                task_id=task_id,
                goal_revision=goal_revision,
            )
        return ReviewEscalationState.model_validate_json(row["state_json"])

    def put_review_escalation(
        self,
        state: ReviewEscalationState,
    ) -> ReviewEscalationState:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT state_json
                FROM project_hermes_review_escalations
                WHERE task_id = ? AND goal_revision = ?
                """,
                (state.task_id, state.goal_revision),
            ).fetchone()
            if row is not None:
                current = ReviewEscalationState.model_validate_json(
                    row["state_json"]
                )
                allowed = {
                    ReviewEscalationStatus.NONE: {
                        ReviewEscalationStatus.NONE,
                        ReviewEscalationStatus.REQUESTED,
                    },
                    ReviewEscalationStatus.REQUESTED: {
                        ReviewEscalationStatus.REQUESTED,
                        ReviewEscalationStatus.BLOCKED,
                    },
                    ReviewEscalationStatus.BLOCKED: {
                        ReviewEscalationStatus.BLOCKED,
                    },
                }
                if state.status not in allowed[current.status]:
                    raise ValueError(
                        "review escalation state cannot move backwards"
                    )
            connection.execute(
                """
                INSERT INTO project_hermes_review_escalations (
                    task_id, goal_revision, status, state_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id, goal_revision) DO UPDATE SET
                    status = excluded.status,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (
                    state.task_id,
                    state.goal_revision,
                    state.status.value,
                    state.model_dump_json(),
                    state.updated_at.isoformat(),
                ),
            )
        return state

    def put_review_packet(self, packet: ReviewPacket) -> ReviewPacket:
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT packet_json FROM project_hermes_review_packets
                WHERE packet_id = ?
                """,
                (packet.packet_id,),
            ).fetchone()
            if existing is not None:
                stored = ReviewPacket.model_validate_json(
                    existing["packet_json"]
                )
                if stored != packet:
                    raise ValueError(
                        "review packet id already exists with different content"
                    )
                return stored
            connection.execute(
                """
                INSERT INTO project_hermes_review_packets (
                    packet_id, task_id, code_diff_sha, goal_revision,
                    content_hash, packet_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    packet.packet_id,
                    packet.task_id,
                    packet.code_diff_sha,
                    packet.goal_revision,
                    packet.content_hash,
                    packet.model_dump_json(exclude_computed_fields=True),
                    packet.created_at.isoformat(),
                ),
            )
        return packet

    def get_review_packet(self, packet_id: str) -> ReviewPacket:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT packet_json FROM project_hermes_review_packets
                WHERE packet_id = ?
                """,
                (packet_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown review packet: {packet_id}")
        return ReviewPacket.model_validate_json(row["packet_json"])

    def review_gate(
        self,
        task_id: str,
        *,
        code_diff_sha: str,
        goal_revision: int,
    ) -> ReviewGate:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT review_json FROM project_hermes_reviews
                WHERE task_id = ? AND code_diff_sha = ? AND goal_revision = ?
                ORDER BY review_role ASC
                """,
                (task_id, code_diff_sha, goal_revision),
            ).fetchall()
        return ReviewGate(
            task_id=task_id,
            code_diff_sha=code_diff_sha,
            goal_revision=goal_revision,
            reviews=[
                ReviewRecord.model_validate_json(row["review_json"])
                for row in rows
            ],
        )

    def invalidate_stale_completion(
        self,
        task_id: str,
        *,
        code_diff_sha: str,
    ) -> list[str]:
        matrix = self.get_completion_matrix(task_id)
        invalidated = matrix.invalidate_for_diff(code_diff_sha)
        self.put_completion_matrix(matrix)
        return [layer.value for layer in invalidated]

    @staticmethod
    def _migrate_review_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(project_hermes_reviews)"
            ).fetchall()
        }
        additions = {
            "review_cycle": "INTEGER",
            "progress_assessment": "TEXT",
            "progress_explanation": "TEXT",
        }
        for name, column_type in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE project_hermes_reviews "
                    f"ADD COLUMN {name} {column_type}"
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


def _completion_contract(matrix: CompletionMatrix) -> tuple:
    """Return the immutable task-owned portion of a completion matrix."""

    return (
        matrix.task_id,
        tuple(layer.value for layer in matrix.required_layers),
        tuple(
            (
                path_id,
                completion.path_id,
                completion.repository,
                tuple(
                    layer.value for layer in completion.required_layers
                ),
            )
            for path_id, completion in sorted(matrix.goal_paths.items())
        ),
    )


def open_assurance_store(config: ControlPlaneConfig) -> AssuranceStore:
    """Open the configured assurance adapter without silent fallback."""

    if config.mode is ControlPlaneMode.SQLITE:
        return SqliteAssuranceStore(config.sqlite_path)
    if config.mode is ControlPlaneMode.POSTGRES:
        from project_hermes.postgres_assurance_store import (
            PostgresAssuranceStore,
        )

        if not config.database_url:
            raise ValueError("postgres mode requires database_url")
        return PostgresAssuranceStore(config.database_url)
    raise NotImplementedError(
        f"{config.mode.value} control-plane mode requires a registered "
        "production AssuranceStore adapter"
    )
