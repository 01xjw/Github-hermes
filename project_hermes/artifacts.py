"""Durable task-scoped source bundle registration."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Iterator

from project_hermes.config import assert_private_directory
from pydantic import model_validator

from project_hermes.models import StrictModel, utc_now
from project_hermes.resources import ArtifactKind, ArtifactManifest
from project_hermes.supply_chain import (
    TaskSourceBundleMetadata,
    create_task_source_bundle,
    verify_task_source_bundle,
)

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS task_source_bundles (
    request_id          TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL,
    resolved_digest     TEXT NOT NULL,
    manifest_json       TEXT NOT NULL,
    metadata_json       TEXT NOT NULL,
    registered_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_source_bundles_task
ON task_source_bundles(task_id, registered_at, request_id);
"""


class SourceBundleRecord(StrictModel):
    """One verified task source bundle and its embedded controller identity."""

    manifest: ArtifactManifest
    metadata: TaskSourceBundleMetadata
    registered_at: datetime

    @model_validator(mode="after")
    def identities_match(self) -> "SourceBundleRecord":
        if self.manifest.kind is not ArtifactKind.ARCHIVE:
            raise ValueError("source bundle manifest must be an archive")
        if self.manifest.task_id != self.metadata.task_id:
            raise ValueError("source bundle manifest belongs to another task")
        if self.registered_at.tzinfo is None:
            raise ValueError("registered_at must be timezone-aware")
        return self


class SqliteArtifactRegistry:
    """Persist verified source-bundle manifests for execution artifact lookup."""

    def __init__(
        self,
        path: str | Path,
        *,
        source_bundle_root: str | Path,
    ) -> None:
        raw_path = Path(path)
        if raw_path.is_symlink():
            raise ValueError("artifact registry database cannot be a symlink")
        self.path = raw_path.resolve()
        raw_source_root = Path(source_bundle_root)
        if raw_source_root.is_symlink():
            raise ValueError("source bundle root cannot be a symbolic link")
        self.source_bundle_root = raw_source_root.resolve()
        assert_private_directory(self.path.parent)
        if self.path.exists() and not self.path.is_file():
            raise ValueError("artifact registry database must be a regular file")
        self.source_bundle_root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
        os.chmod(self.path, 0o600)

    def create_source_bundle(
        self,
        worktree: str | Path,
        *,
        request_id: str,
        metadata: TaskSourceBundleMetadata,
    ) -> SourceBundleRecord:
        """Create deterministic bytes, verify them, and register atomically."""

        manifest = create_task_source_bundle(
            Path(worktree),
            self.source_bundle_root,
            request_id=request_id,
            metadata=metadata,
        )
        return self.register_source_bundle(manifest, metadata=metadata)

    def register_source_bundle(
        self,
        manifest: ArtifactManifest,
        *,
        metadata: TaskSourceBundleMetadata,
        now: datetime | None = None,
    ) -> SourceBundleRecord:
        """Register one verified archive with exact-payload idempotency."""

        if manifest.kind is not ArtifactKind.ARCHIVE:
            raise ValueError("task source bundle must be an archive artifact")
        verify_task_source_bundle(
            self.source_bundle_root,
            manifest,
            expected=metadata,
        )
        registered_at = now or utc_now()
        if registered_at.tzinfo is None:
            raise ValueError("registered_at must be timezone-aware")
        record = SourceBundleRecord(
            manifest=manifest,
            metadata=metadata,
            registered_at=registered_at,
        )
        manifest_json = _json(manifest.model_dump(mode="json"))
        metadata_json = _json(metadata.model_dump(mode="json"))
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM task_source_bundles WHERE request_id = ?
                """,
                (manifest.request_id,),
            ).fetchone()
            if existing is not None:
                existing_record = self._record_from_row(existing)
                if (
                    _manifest_identity(existing_record.manifest)
                    != _manifest_identity(manifest)
                    or existing_record.metadata != metadata
                ):
                    raise ValueError(
                        "artifact request_id is already registered differently"
                    )
                return existing_record
            connection.execute(
                """
                INSERT INTO task_source_bundles (
                    request_id, task_id, resolved_digest, manifest_json,
                    metadata_json, registered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.request_id,
                    manifest.task_id,
                    manifest.resolved_digest,
                    manifest_json,
                    metadata_json,
                    registered_at.isoformat(),
                ),
            )
        return record

    def get(self, request_id: str) -> ArtifactManifest | None:
        """Return a persisted and byte-verified execution artifact."""

        record = self._load(request_id)
        if record is None:
            return None
        verify_task_source_bundle(
            self.source_bundle_root,
            record.manifest,
            expected=record.metadata,
        )
        return record.manifest

    def require_source_bundle(self, request_id: str) -> SourceBundleRecord:
        """Re-verify registered bytes and return their task metadata."""

        record = self._load(request_id)
        if record is None:
            raise KeyError(f"unknown task source bundle: {request_id}")
        verify_task_source_bundle(
            self.source_bundle_root,
            record.manifest,
            expected=record.metadata,
        )
        return record

    def _load(self, request_id: str) -> SourceBundleRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM task_source_bundles WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        return None if row is None else self._record_from_row(row)

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> SourceBundleRecord:
        manifest = ArtifactManifest.model_validate_json(row["manifest_json"])
        metadata = TaskSourceBundleMetadata.model_validate_json(
            row["metadata_json"]
        )
        if (
            row["task_id"] != manifest.task_id
            or row["resolved_digest"] != manifest.resolved_digest
            or metadata.task_id != manifest.task_id
        ):
            raise ValueError("stored source bundle identity is inconsistent")
        return SourceBundleRecord(
            manifest=manifest,
            metadata=metadata,
            registered_at=row["registered_at"],
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


def _json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _manifest_identity(manifest: ArtifactManifest) -> tuple[object, ...]:
    return (
        manifest.schema_version,
        manifest.request_id,
        manifest.task_id,
        manifest.kind,
        manifest.source_uri,
        manifest.resolved_digest,
        manifest.local_path,
        manifest.byte_size,
    )


__all__ = [
    "SourceBundleRecord",
    "SqliteArtifactRegistry",
]
