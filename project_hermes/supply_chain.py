"""Offline image and model supply contracts."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tempfile
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from project_hermes.models import StrictModel, utc_now
from project_hermes.redaction import redact_text
from project_hermes.resources import ArtifactKind, ArtifactManifest, ArtifactRequest

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_ARCHIVE_IDENTIFIER_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
)
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GIT_OBJECT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_CANDIDATE_RE = re.compile(r"^[0-9a-f]{64}$")
_CORE_WORKER_ARTIFACTS = frozenset(
    {"stdout.log", "stderr.log", "result.json", "usage.json"}
)


class SupplyStatus(StrEnum):
    """Offline artifact supply lifecycle."""

    MISSING = "MISSING"
    FETCH_QUEUED = "FETCH_QUEUED"
    FETCHING = "FETCHING"
    VERIFYING = "VERIFYING"
    IMPORTING = "IMPORTING"
    READY = "READY"
    FAILED = "FAILED"


class SupplyFailureKind(StrEnum):
    """Actionable supply failure classes."""

    NETWORK = "network"
    AUTHENTICATION = "authentication"
    SOURCE_MISSING = "source_missing"
    CHECKSUM = "checksum"
    IMPORT = "import"
    POLICY = "policy"
    QUOTA = "quota"
    UNKNOWN = "unknown"


class ModelOriginKind(StrEnum):
    """Explicit supported external model catalogs."""

    HUGGING_FACE = "hugging_face"
    MODELSCOPE = "modelscope"


class SshRelayConfig(StrictModel):
    """Administrator-owned SSH relay reference without key material."""

    schema_version: Literal["ssh-relay.v1"] = "ssh-relay.v1"
    relay_id: str
    host: str
    user: str
    port: int = Field(default=22, ge=1, le=65_535)
    known_hosts_file: Path
    credential_secret_ref: str
    staging_root: PurePosixPath
    allowed_source_hosts: list[str] = Field(min_length=1)
    allowed_operations: list[
        Literal[
            "fetch_image",
            "export_oci",
            "fetch_model",
            "verify",
            "transfer",
            "import_image",
            "import_model",
        ]
    ] = Field(min_length=1)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not _HOST_RE.fullmatch(value):
            raise ValueError("relay host is invalid")
        return value

    @field_validator("allowed_source_hosts")
    @classmethod
    def validate_source_hosts(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.lower() for value in values))
        if any(not _HOST_RE.fullmatch(value) for value in normalized):
            raise ValueError("allowed source host is invalid")
        return normalized

    @field_validator("allowed_operations")
    @classmethod
    def unique_operations(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def staging_root_is_absolute(self) -> "SshRelayConfig":
        if not self.staging_root.is_absolute():
            raise ValueError("relay staging_root must be absolute")
        return self


class ImageSourceMap(StrictModel):
    """One logical image mapped to upstream and internal identities."""

    schema_version: Literal["image-source-map.v1"] = "image-source-map.v1"
    logical_name: str
    source_ref: str
    platform: str
    expected_manifest_digest: str
    internal_ref: str
    relay_id: str
    require_sbom: bool = True
    require_vulnerability_scan: bool = True

    @field_validator("expected_manifest_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST_RE.fullmatch(value):
            raise ValueError(
                "expected_manifest_digest must be a lowercase sha256 digest"
            )
        return value

    @model_validator(mode="after")
    def internal_reference_is_digest_pinned(self) -> "ImageSourceMap":
        if "@sha256:" not in self.internal_ref:
            raise ValueError("internal image reference must be digest-pinned")
        return self


class ModelOrigin(StrictModel):
    """One exact external source for a logical model."""

    kind: ModelOriginKind
    model_id: str
    revision: str
    expected_files: dict[str, str] = Field(min_length=1)
    source_host: str

    @field_validator("expected_files")
    @classmethod
    def validate_files(cls, values: dict[str, str]) -> dict[str, str]:
        for relative, digest in values.items():
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("model file paths must stay relative")
            if not _DIGEST_RE.fullmatch(digest):
                raise ValueError("model files require lowercase sha256 digests")
        return values


class ModelSourceMap(StrictModel):
    """Explicit, checksum-safe model catalog fallback."""

    schema_version: Literal["model-source-map.v1"] = "model-source-map.v1"
    logical_name: str
    internal_cache_ref: str
    license: str
    origins: list[ModelOrigin] = Field(min_length=1)
    fallback_order: list[ModelOriginKind] = Field(min_length=1)
    relay_id: str

    @model_validator(mode="after")
    def fallback_names_only_configured_origins(self) -> "ModelSourceMap":
        configured = {origin.kind for origin in self.origins}
        if len(self.fallback_order) != len(set(self.fallback_order)):
            raise ValueError("model fallback order must be unique")
        if set(self.fallback_order) != configured:
            raise ValueError(
                "model fallback order must name every configured origin"
            )
        identities = {
            (origin.kind, origin.model_id, origin.revision)
            for origin in self.origins
        }
        if len(identities) != len(self.origins):
            raise ValueError("model origins must be unique")
        return self


class SupplyRecord(StrictModel):
    """Durable projection of an offline artifact transfer."""

    schema_version: Literal["artifact-supply-record.v1"] = (
        "artifact-supply-record.v1"
    )
    supply_id: str
    request: ArtifactRequest
    status: SupplyStatus = SupplyStatus.MISSING
    source_identity: dict[str, Any] = Field(default_factory=dict)
    resolved_digest: str | None = None
    internal_ref: str | None = None
    manifest: ArtifactManifest | None = None
    attempt: int = Field(default=0, ge=0)
    transferred_bytes: int = Field(default=0, ge=0)
    part_digests: list[str] = Field(default_factory=list)
    failure_kind: SupplyFailureKind | None = None
    failure_detail: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def status_has_required_provenance(self) -> "SupplyRecord":
        if self.status is SupplyStatus.READY and self.manifest is None:
            raise ValueError("ready supply records require a manifest")
        if self.status is SupplyStatus.FAILED:
            if self.failure_kind is None or not self.failure_detail:
                raise ValueError("failed supply records require failure detail")
        elif self.failure_kind is not None or self.failure_detail is not None:
            raise ValueError("non-failed supply records cannot claim failure")
        return self


class TaskSourceBundleMetadata(StrictModel):
    """Identity embedded in one task-scoped source archive."""

    schema_version: Literal["task-source-bundle.v1"] = (
        "task-source-bundle.v1"
    )
    task_id: str
    repository: str
    workspace_lease_id: str
    base_sha: str
    candidate_digest: str

    @field_validator("task_id", "workspace_lease_id")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        if not _ARCHIVE_IDENTIFIER_RE.fullmatch(value):
            raise ValueError("source bundle identifier is invalid")
        return value

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if not _REPOSITORY_RE.fullmatch(value):
            raise ValueError("source bundle repository must use owner/name")
        return value

    @field_validator("base_sha")
    @classmethod
    def validate_base_sha(cls, value: str) -> str:
        if not _GIT_OBJECT_RE.fullmatch(value):
            raise ValueError("source bundle base_sha must be a full Git id")
        return value

    @field_validator("candidate_digest")
    @classmethod
    def validate_candidate_digest(cls, value: str) -> str:
        if not _CANDIDATE_RE.fullmatch(value):
            raise ValueError(
                "source bundle candidate_digest must be a SHA-256 hex digest"
            )
        return value


class WorkerArtifactEntry(StrictModel):
    """One content-addressed worker output."""

    name: Literal["stdout.log", "stderr.log", "result.json", "usage.json"]
    digest: str
    byte_size: int = Field(ge=0)
    object_path: str

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST_RE.fullmatch(value):
            raise ValueError("worker artifact digest must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def object_path_matches_digest(self) -> "WorkerArtifactEntry":
        expected = f"sha256/{self.digest.removeprefix('sha256:')}"
        if self.object_path != expected:
            raise ValueError(
                "worker artifact object_path must be content-addressed"
            )
        return self


class WorkerArtifactArchive(StrictModel):
    """Verified core outputs retained independently of a worker Job."""

    schema_version: Literal["worker-artifact-archive.v1"] = (
        "worker-artifact-archive.v1"
    )
    execution_id: str
    task_id: str
    release_digest: str
    image_digest: str
    source_bundle_digest: str
    outcome: Literal["succeeded", "failed"]
    exit_code: int
    entries: list[WorkerArtifactEntry] = Field(min_length=4, max_length=4)

    @field_validator("execution_id", "task_id")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        if not _ARCHIVE_IDENTIFIER_RE.fullmatch(value):
            raise ValueError("worker archive identifier is invalid")
        return value

    @field_validator("release_digest")
    @classmethod
    def validate_release_digest(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("release_digest must be lowercase SHA-256 hex")
        return value

    @field_validator("image_digest", "source_bundle_digest")
    @classmethod
    def validate_prefixed_digest(cls, value: str) -> str:
        if not _DIGEST_RE.fullmatch(value):
            raise ValueError("worker archive identity requires SHA-256")
        return value

    @model_validator(mode="after")
    def core_artifacts_are_complete(self) -> "WorkerArtifactArchive":
        names = [entry.name for entry in self.entries]
        if len(names) != len(set(names)) or set(names) != _CORE_WORKER_ARTIFACTS:
            raise ValueError("worker archive must contain each core artifact")
        expected_outcome = "succeeded" if self.exit_code == 0 else "failed"
        if self.outcome != expected_outcome:
            raise ValueError("worker archive outcome contradicts exit_code")
        return self


class ArtifactSupplyProvider(Protocol):
    """Controller-selected provider with explicit supply phases."""

    name: str

    def resolve(self, request: ArtifactRequest) -> dict[str, Any]:
        """Resolve an immutable upstream identity."""

    def probe(self, request: ArtifactRequest) -> ArtifactManifest | None:
        """Return an already imported verified artifact when present."""

    def fetch(
        self,
        request: ArtifactRequest,
        *,
        resume_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Fetch or resume content on an approved relay."""

    def export(self, request: ArtifactRequest) -> dict[str, Any]:
        """Export content into a transferable OCI or file layout."""

    def transfer(
        self,
        request: ArtifactRequest,
        *,
        part_digests: tuple[str, ...],
    ) -> dict[str, Any]:
        """Transfer verified parts to the target environment."""

    def import_artifact(
        self,
        request: ArtifactRequest,
    ) -> dict[str, Any]:
        """Import into the internal registry or model cache."""

    def verify(self, request: ArtifactRequest) -> ArtifactManifest:
        """Verify final internal bytes and return an immutable manifest."""


_ALLOWED_SUPPLY_TRANSITIONS: dict[SupplyStatus, frozenset[SupplyStatus]] = {
    SupplyStatus.MISSING: frozenset(
        {SupplyStatus.FETCH_QUEUED, SupplyStatus.FAILED}
    ),
    SupplyStatus.FETCH_QUEUED: frozenset(
        {SupplyStatus.FETCHING, SupplyStatus.FAILED}
    ),
    SupplyStatus.FETCHING: frozenset(
        {SupplyStatus.VERIFYING, SupplyStatus.FAILED}
    ),
    SupplyStatus.VERIFYING: frozenset(
        {SupplyStatus.IMPORTING, SupplyStatus.FAILED}
    ),
    SupplyStatus.IMPORTING: frozenset(
        {SupplyStatus.READY, SupplyStatus.FAILED}
    ),
    SupplyStatus.READY: frozenset(),
    SupplyStatus.FAILED: frozenset({SupplyStatus.FETCH_QUEUED}),
}


def transition_supply(
    record: SupplyRecord,
    target: SupplyStatus,
    **updates: Any,
) -> SupplyRecord:
    """Apply one fail-closed artifact supply transition."""

    if target not in _ALLOWED_SUPPLY_TRANSITIONS[record.status]:
        raise ValueError(
            f"invalid artifact supply transition: {record.status} -> {target}"
        )
    payload = {
        **updates,
        "status": target,
        "updated_at": utc_now(),
    }
    if payload.get("failure_detail"):
        payload["failure_detail"] = redact_text(
            str(payload["failure_detail"])
        )
    if target is not SupplyStatus.FAILED:
        payload["failure_kind"] = None
        payload["failure_detail"] = None
    updated = record.model_copy(update=payload)
    return SupplyRecord.model_validate(updated.model_dump())


def artifact_kind_for_source(
    source: ImageSourceMap | ModelSourceMap,
) -> ArtifactKind:
    """Return the artifact kind represented by one source map."""

    if isinstance(source, ImageSourceMap):
        return ArtifactKind.CONTAINER_IMAGE
    return ArtifactKind.MODEL


def create_task_source_bundle(
    worktree: Path,
    artifact_root: Path,
    *,
    request_id: str,
    metadata: TaskSourceBundleMetadata,
) -> ArtifactManifest:
    """Create a deterministic, content-addressed snapshot of one worktree."""

    source = worktree.resolve()
    if worktree.is_symlink() or not source.is_dir():
        raise ValueError("task source worktree must be a real directory")
    destination_root = artifact_root.resolve()
    if destination_root == source or destination_root.is_relative_to(source):
        raise ValueError("source bundle storage must be outside the worktree")
    destination_root.mkdir(parents=True, exist_ok=True)
    if artifact_root.is_symlink():
        raise ValueError("source bundle storage cannot be a symbolic link")
    staging_root = destination_root / ".staging"
    staging_root.mkdir(mode=0o700, exist_ok=True)

    entries: list[tuple[Path, Path]] = []
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if relative.parts[0] == ".git" or path.is_dir():
            continue
        if not path.is_file() and not path.is_symlink():
            raise ValueError(f"unsupported worktree entry: {relative}")
        if path.is_symlink():
            target = (path.parent / os.readlink(path)).resolve(strict=False)
            if not target.is_relative_to(source):
                raise ValueError(f"worktree symlink escapes source: {relative}")
        entries.append((relative, path))
    entries.sort(key=lambda item: item[0].as_posix())

    descriptor, temporary_name = tempfile.mkstemp(
        prefix="bundle-",
        suffix=".tar.gz",
        dir=staging_root,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw,
                compresslevel=9,
                mtime=0,
            ) as compressed:
                with tarfile.open(
                    mode="w",
                    fileobj=compressed,
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    encoded_metadata = (
                        json.dumps(
                            metadata.model_dump(mode="json"),
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("utf-8")
                    info = _normalized_tar_info(
                        "bundle.json",
                        mode=0o444,
                        size=len(encoded_metadata),
                    )
                    archive.addfile(info, fileobj=io.BytesIO(encoded_metadata))
                    for relative, path in entries:
                        archive_name = (
                            PurePosixPath("worktree") / relative.as_posix()
                        ).as_posix()
                        details = path.lstat()
                        if path.is_symlink():
                            info = _normalized_tar_info(
                                archive_name,
                                mode=0o777,
                                size=0,
                            )
                            info.type = tarfile.SYMTYPE
                            info.linkname = os.readlink(path)
                            archive.addfile(info)
                            continue
                        mode = (
                            0o755
                            if details.st_mode
                            & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                            else 0o644
                        )
                        info = _normalized_tar_info(
                            archive_name,
                            mode=mode,
                            size=details.st_size,
                        )
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
        digest = _sha256_file(temporary)
        relative_destination = Path("sha256") / f"{digest}.tar.gz"
        destination = destination_root / relative_destination
        destination.parent.mkdir(mode=0o700, exist_ok=True)
        if destination.exists():
            if (
                destination.is_symlink()
                or not destination.is_file()
                or _sha256_file(destination) != digest
            ):
                raise ValueError(
                    "existing content-addressed source bundle is invalid"
                )
            temporary.unlink()
        else:
            os.chmod(temporary, 0o444)
            os.replace(temporary, destination)
        return ArtifactManifest(
            request_id=request_id,
            task_id=metadata.task_id,
            kind=ArtifactKind.ARCHIVE,
            source_uri=(
                f"workspace-bundle://{metadata.workspace_lease_id}"
            ),
            resolved_digest=f"sha256:{digest}",
            local_path=relative_destination.as_posix(),
            byte_size=destination.stat().st_size,
        )
    finally:
        temporary.unlink(missing_ok=True)


def verify_task_source_bundle(
    artifact_root: Path,
    manifest: ArtifactManifest,
    *,
    expected: TaskSourceBundleMetadata,
) -> Path:
    """Verify bundle bytes and embedded task identity before Job creation."""

    if manifest.kind is not ArtifactKind.ARCHIVE:
        raise ValueError("Codex worker source must be an archive artifact")
    if manifest.task_id != expected.task_id:
        raise ValueError("source bundle belongs to another task")
    digest = manifest.resolved_digest.removeprefix("sha256:")
    expected_relative = PurePosixPath("sha256") / f"{digest}.tar.gz"
    if manifest.local_path != expected_relative.as_posix():
        raise ValueError("source bundle path is not content-addressed")
    path = _confined_regular_file(artifact_root, manifest.local_path)
    if path.stat().st_size != manifest.byte_size:
        raise ValueError("source bundle byte size does not match manifest")
    if _sha256_file(path) != digest:
        raise ValueError("source bundle digest does not match manifest")
    embedded = _read_task_source_metadata(path)
    if embedded != expected:
        raise ValueError("source bundle embedded identity does not match request")
    return path


def verify_worker_artifact_archive(
    archive_root: Path,
    *,
    execution_id: str,
    task_id: str,
    release_digest: str,
    image_digest: str,
    source_bundle_digest: str,
) -> WorkerArtifactArchive:
    """Verify a complete worker archive and every addressed object."""

    if not _ARCHIVE_IDENTIFIER_RE.fullmatch(execution_id):
        raise ValueError("execution_id is unsafe for archive lookup")
    execution_root = archive_root / "executions" / execution_id
    manifest_path = _confined_regular_file(
        archive_root,
        (PurePosixPath("executions") / execution_id / "manifest.json").as_posix(),
    )
    marker_path = _confined_regular_file(
        archive_root,
        (PurePosixPath("executions") / execution_id / "complete").as_posix(),
    )
    raw_manifest = manifest_path.read_bytes()
    manifest_digest = hashlib.sha256(raw_manifest).hexdigest()
    marker = marker_path.read_text(encoding="ascii").strip()
    if marker != manifest_digest:
        raise ValueError("worker archive completion digest does not match")
    archive = WorkerArtifactArchive.model_validate_json(raw_manifest)
    expected_identity = {
        "execution_id": execution_id,
        "task_id": task_id,
        "release_digest": release_digest,
        "image_digest": image_digest,
        "source_bundle_digest": source_bundle_digest,
    }
    actual_identity = {
        name: getattr(archive, name) for name in expected_identity
    }
    if actual_identity != expected_identity:
        raise ValueError("worker archive identity does not match execution")
    if execution_root.is_symlink():
        raise ValueError("worker execution archive cannot be a symlink")
    for entry in archive.entries:
        object_path = _confined_regular_file(
            archive_root,
            entry.object_path,
        )
        if object_path.stat().st_size != entry.byte_size:
            raise ValueError(f"worker artifact size mismatch: {entry.name}")
        actual_digest = f"sha256:{_sha256_file(object_path)}"
        if actual_digest != entry.digest:
            raise ValueError(f"worker artifact digest mismatch: {entry.name}")
    return archive


def _normalized_tar_info(name: str, *, mode: int, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = mode
    info.size = size
    info.mtime = 0
    info.pax_headers = {}
    return info


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _confined_regular_file(root: Path, relative: str) -> Path:
    relative_path = PurePosixPath(relative)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or ".." in relative_path.parts
    ):
        raise ValueError("artifact path is not confined")
    resolved_root = root.resolve()
    if root.is_symlink():
        raise ValueError("artifact root cannot be a symbolic link")
    candidate = resolved_root
    for part in relative_path.parts:
        candidate /= part
        if candidate.is_symlink():
            raise ValueError(
                f"artifact path contains a symbolic link: {relative}"
            )
    if (
        not candidate.is_file()
        or not candidate.resolve().is_relative_to(resolved_root)
    ):
        raise ValueError(f"artifact is not a confined regular file: {relative}")
    return candidate


def _read_task_source_metadata(path: Path) -> TaskSourceBundleMetadata:
    with tarfile.open(path, mode="r:gz") as archive:
        metadata_member: tarfile.TarInfo | None = None
        seen: set[str] = set()
        for member in archive:
            relative = PurePosixPath(member.name)
            if (
                relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
                or member.name in seen
            ):
                raise ValueError("source bundle contains an unsafe path")
            seen.add(member.name)
            if member.name == "bundle.json":
                if not member.isfile():
                    raise ValueError("source bundle metadata is not a file")
                metadata_member = member
                continue
            if relative.parts[0] != "worktree":
                raise ValueError("source bundle member is outside worktree")
            if not (member.isfile() or member.issym()):
                raise ValueError("source bundle contains unsupported entries")
            if member.issym():
                target = PurePosixPath(member.linkname)
                if target.is_absolute():
                    raise ValueError("source bundle has an absolute symlink")
                depth = len(relative.parent.parts)
                for part in target.parts:
                    if part in {"", "."}:
                        continue
                    depth = depth - 1 if part == ".." else depth + 1
                    if depth < 1:
                        raise ValueError("source bundle symlink escapes worktree")
        if metadata_member is None:
            raise ValueError("source bundle metadata is missing")
        stream = archive.extractfile(metadata_member)
        if stream is None:
            raise ValueError("source bundle metadata cannot be read")
        return TaskSourceBundleMetadata.model_validate_json(stream.read())
