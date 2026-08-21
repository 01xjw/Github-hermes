"""Cross-validated active ProjectHermes release identity."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import field_validator, model_validator

from project_hermes.models import StrictModel

_RELEASE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REGISTRY_RE = re.compile(
    r"^(?:localhost|[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)"
    r"(?::[1-9][0-9]{0,4})?$"
)
_IMAGE_COMPONENT_RE = re.compile(
    r"^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*$"
)
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_MAX_MANIFEST_BYTES = 1_048_576
_REQUIRED_WORKER_FILES = (
    "wheelhouse/requirements.lock",
    "worker/prepare-runtime.sh",
    "worker/prepare-source.py",
    "worker/run.sh",
    "worker/execute-task.py",
)


class ActiveReleaseRecord(StrictModel):
    """Controller state naming the active immutable release."""

    schema_version: Literal["project-hermes-active-release.v1"]
    active_release_digest: str
    previous_release_digest: str | None = None
    activated_at: datetime

    @field_validator("active_release_digest", "previous_release_digest")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is not None and not _RELEASE_DIGEST_RE.fullmatch(value):
            raise ValueError("release digest must be lowercase SHA-256 hex")
        return value

    @model_validator(mode="after")
    def validate_activation(self) -> "ActiveReleaseRecord":
        if self.activated_at.tzinfo is None:
            raise ValueError("activated_at must be timezone-aware")
        if self.previous_release_digest == self.active_release_digest:
            raise ValueError("previous release must differ from active release")
        return self


class VerifiedActiveRelease(StrictModel):
    """Active state joined to verified on-disk release bytes."""

    record: ActiveReleaseRecord
    release_root: Path
    runtime_image: str
    manifest: dict[str, Any]


def load_active_release(path: str | Path) -> ActiveReleaseRecord:
    """Load one bounded, regular active-release state file."""

    state_path = Path(path)
    if state_path.is_symlink() or not state_path.is_file():
        raise ValueError("active release state must be a regular file")
    if state_path.stat().st_size > 65_536:
        raise ValueError("active release state exceeds 64 KiB")
    return ActiveReleaseRecord.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )


def verify_active_release(
    release_root: str | Path,
    state_path: str | Path,
    *,
    expected_digest: str | None = None,
) -> VerifiedActiveRelease:
    """Cross-check active state, expected deployment, and mounted release."""

    record = load_active_release(state_path)
    if (
        expected_digest is not None
        and record.active_release_digest != expected_digest
    ):
        raise ValueError("active release state differs from expected deployment")
    manifest = verify_release_root(
        release_root,
        record.active_release_digest,
    )
    return VerifiedActiveRelease(
        record=record,
        release_root=Path(release_root).resolve(),
        runtime_image=str(manifest["runtime_image"]),
        manifest=manifest,
    )


def verify_release_root(
    release_root: str | Path,
    release_digest: str,
) -> dict[str, Any]:
    """Verify the mounted worker release against its immutable digest."""

    if not _RELEASE_DIGEST_RE.fullmatch(release_digest):
        raise ValueError("release_digest must be lowercase SHA-256 hex")
    root = Path(release_root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("worker release root must be a real directory")
    manifest_path = _regular_release_file(root, "release-manifest.json")
    if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("worker release manifest exceeds one MiB")
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != release_digest:
        raise ValueError("worker release manifest digest mismatch")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict):
        raise ValueError("worker release manifest must be an object")
    if manifest.get("schema_version") != "project-hermes-release.v1":
        raise ValueError("unsupported worker release manifest")
    if manifest.get("codex") != {
        "sdk_version": "0.144.4",
        "cli_version": "0.144.4",
    }:
        raise ValueError("worker release does not pin Codex 0.144.4")
    parse_runtime_image(str(manifest.get("runtime_image") or ""))

    digest_file = _regular_release_file(root, "RELEASE_DIGEST")
    if digest_file.read_text(encoding="ascii").strip() != release_digest:
        raise ValueError("worker RELEASE_DIGEST does not match the manifest")
    for relative in _REQUIRED_WORKER_FILES:
        _regular_release_file(root, relative)
    return manifest


def parse_runtime_image(value: str) -> tuple[str, str]:
    """Return tag-free repository and digest from an immutable OCI reference."""

    image_name, separator, digest = value.partition("@")
    last_slash = image_name.rfind("/")
    last_colon = image_name.rfind(":")
    tag: str | None = None
    if last_colon > last_slash:
        repository = image_name[:last_colon]
        tag = image_name[last_colon + 1 :]
    else:
        repository = image_name
    components = repository.split("/")
    if (
        not separator
        or "@" in digest
        or not _SHA256_RE.fullmatch(digest)
        or repository != repository.casefold()
        or (tag is not None and not _IMAGE_TAG_RE.fullmatch(tag))
        or len(components) < 2
        or not _REGISTRY_RE.fullmatch(components[0])
        or (
            components[0] != "localhost"
            and "." not in components[0]
            and ":" not in components[0]
        )
        or any(
            not _IMAGE_COMPONENT_RE.fullmatch(component)
            for component in components[1:]
        )
    ):
        raise ValueError("release runtime image is not an immutable OCI image")
    return repository, digest


def _regular_release_file(root: Path, relative: str) -> Path:
    resolved_root = root.resolve()
    candidate = root
    for part in Path(relative).parts:
        candidate /= part
        if candidate.is_symlink():
            raise ValueError(f"worker release path contains a symlink: {relative}")
    if (
        not candidate.is_file()
        or not candidate.resolve().is_relative_to(resolved_root)
    ):
        raise ValueError(f"worker release file is missing: {relative}")
    return candidate


__all__ = [
    "ActiveReleaseRecord",
    "VerifiedActiveRelease",
    "load_active_release",
    "parse_runtime_image",
    "verify_active_release",
    "verify_release_root",
]
