"""Resource, workspace, and artifact supply contracts."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from project_hermes.models import StrictModel, utc_now

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ExecutionEnvironment(StrEnum):
    """Supported execution environments."""

    LOCAL = "local"
    KUBERNETES = "kubernetes"
    GPU_CLUSTER = "gpu_cluster"


class GpuAllocationStatus(StrEnum):
    """GPU allocation lifecycle."""

    ALLOCATED = "ALLOCATED"
    TERMINATING = "TERMINATING"
    RELEASED = "RELEASED"


class ArtifactKind(StrEnum):
    """Externally supplied artifact classes."""

    CONTAINER_IMAGE = "container_image"
    MODEL = "model"
    DATASET = "dataset"
    ARCHIVE = "archive"


class ArtifactStatus(StrEnum):
    """Content-addressed artifact supply lifecycle."""

    REQUESTED = "REQUESTED"
    DOWNLOADING = "DOWNLOADING"
    VERIFIED = "VERIFIED"
    READY = "READY"
    FAILED = "FAILED"


class GpuDevice(StrictModel):
    """Controller inventory for one physical GPU."""

    schema_version: Literal["gpu-device.v1"] = "gpu-device.v1"
    gpu_id: str
    architecture: str | None = None
    memory_mb: int | None = Field(default=None, ge=1)
    node: str | None = None
    topology_labels: dict[str, str] = Field(default_factory=dict)
    healthy: bool = True

    @field_validator("architecture")
    @classmethod
    def normalize_architecture(cls, value: str | None) -> str | None:
        return value.casefold() if value else None


class GpuRequest(StrictModel):
    """Request for GPUs.

    There is intentionally no duration field. A lease is released only after
    the associated execution pod or job is confirmed terminated.
    """

    schema_version: Literal["gpu-request.v1"] = "gpu-request.v1"
    request_id: str
    task_id: str
    count: int = Field(ge=1, le=8)
    architecture: str | None = None
    minimum_memory_mb: int | None = Field(default=None, ge=1)
    topology: str | None = None
    image_digest: str | None = None
    environment: ExecutionEnvironment = ExecutionEnvironment.GPU_CLUSTER
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("request_id", "task_id")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("GPU identifiers contain unsupported characters")
        return value

    @field_validator("image_digest")
    @classmethod
    def validate_image_digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST_RE.fullmatch(value):
            raise ValueError("image_digest must be a lowercase sha256 digest")
        return value


class GpuLease(StrictModel):
    """Atomic GPU allocation tied to one execution object."""

    schema_version: Literal["gpu-lease.v1"] = "gpu-lease.v1"
    lease_id: str
    request_id: str
    task_id: str
    gpu_ids: list[str] = Field(min_length=1, max_length=8)
    execution_id: str
    status: GpuAllocationStatus = GpuAllocationStatus.ALLOCATED
    allocated_at: datetime = Field(default_factory=utc_now)
    released_at: datetime | None = None

    @model_validator(mode="after")
    def validate_release_time(self) -> "GpuLease":
        if self.status is GpuAllocationStatus.RELEASED:
            if self.released_at is None:
                raise ValueError("released GPU leases require released_at")
        elif self.released_at is not None:
            raise ValueError("active GPU leases cannot contain released_at")
        if len(self.gpu_ids) != len(set(self.gpu_ids)):
            raise ValueError("gpu_ids must be unique")
        return self


class ArtifactRequest(StrictModel):
    """Controller-owned request for an external artifact."""

    schema_version: Literal["artifact-request.v1"] = "artifact-request.v1"
    request_id: str
    task_id: str
    kind: ArtifactKind
    source_uri: str
    expected_digest: str
    destination: str
    status: ArtifactStatus = ArtifactStatus.REQUESTED
    requested_at: datetime = Field(default_factory=utc_now)

    @field_validator("request_id", "task_id")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError(
                "artifact identifiers contain unsupported characters"
            )
        return value

    @field_validator("source_uri")
    @classmethod
    def validate_source_uri(cls, value: str) -> str:
        if any(ord(character) < 32 for character in value):
            raise ValueError("artifact source URI contains control characters")
        parsed = urlsplit(value)
        if not parsed.scheme:
            raise ValueError("artifact source URI requires an explicit scheme")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(
                "artifact source URI cannot contain embedded credentials"
            )
        return value

    @field_validator("destination")
    @classmethod
    def validate_destination(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(
                "artifact destination must be a confined relative path"
            )
        return value

    @field_validator("expected_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST_RE.fullmatch(value):
            raise ValueError("expected_digest must be a lowercase sha256 digest")
        return value


class ArtifactManifest(StrictModel):
    """Verified, immutable artifact identity."""

    schema_version: Literal["artifact-manifest.v1"] = "artifact-manifest.v1"
    request_id: str
    task_id: str
    kind: ArtifactKind
    source_uri: str
    resolved_digest: str
    local_path: str
    byte_size: int = Field(ge=0)
    verified_at: datetime = Field(default_factory=utc_now)

    @field_validator("resolved_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST_RE.fullmatch(value):
            raise ValueError("resolved_digest must be a lowercase sha256 digest")
        return value


class WorkspaceLease(StrictModel):
    """Exclusive ownership of a task worktree."""

    schema_version: Literal["workspace-lease.v1"] = "workspace-lease.v1"
    lease_id: str
    task_id: str
    repository: str
    mirror_path: str
    worktree_path: str
    branch_name: str
    base_sha: str
    owner_session_id: str
    acquired_at: datetime = Field(default_factory=utc_now)
    released_at: datetime | None = None

    @model_validator(mode="after")
    def worktree_must_not_equal_mirror(self) -> "WorkspaceLease":
        if self.worktree_path == self.mirror_path:
            raise ValueError("worktree path must be distinct from the mirror")
        return self


class ResourceBundle(StrictModel):
    """Resources required before an execution node can start."""

    schema_version: Literal["resource-bundle.v1"] = "resource-bundle.v1"
    workspace: WorkspaceLease
    gpu: GpuLease | None = None
    artifacts: list[ArtifactManifest] = Field(default_factory=list)

    @model_validator(mode="after")
    def resources_belong_to_one_task(self) -> "ResourceBundle":
        task_id = self.workspace.task_id
        if self.gpu is not None and self.gpu.task_id != task_id:
            raise ValueError("GPU lease belongs to a different task")
        if any(artifact.task_id != task_id for artifact in self.artifacts):
            raise ValueError("artifact manifest belongs to a different task")
        return self
