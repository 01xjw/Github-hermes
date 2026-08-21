#!/usr/bin/env python3
"""Verify and extract one task-scoped source bundle without tar traversal."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_symlink(member: tarfile.TarInfo, relative: PurePosixPath) -> None:
    target = PurePosixPath(member.linkname)
    if target.is_absolute():
        raise RuntimeError(f"absolute source symlink: {member.name}")
    depth = len(relative.parent.parts)
    for part in target.parts:
        if part in {"", "."}:
            continue
        depth = depth - 1 if part == ".." else depth + 1
        if depth < 1:
            raise RuntimeError(f"escaping source symlink: {member.name}")


def main() -> int:
    bundle = Path(_required("PROJECT_HERMES_SOURCE_BUNDLE"))
    expected_digest = _required("PROJECT_HERMES_SOURCE_BUNDLE_DIGEST")
    destination = Path(_required("PROJECT_HERMES_SOURCE_ROOT"))
    if not expected_digest.startswith("sha256:"):
        raise RuntimeError("source bundle digest must use sha256")
    if bundle.is_symlink() or not bundle.is_file():
        raise RuntimeError("source bundle is not a regular mounted file")
    if f"sha256:{_sha256(bundle)}" != expected_digest:
        raise RuntimeError("source bundle digest mismatch")
    if not destination.is_absolute():
        raise RuntimeError("source destination must be absolute")
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError("source destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    source_root = destination / "repo"
    source_root.mkdir(mode=0o700)

    expected_metadata = {
        "schema_version": "task-source-bundle.v1",
        "task_id": _required("PROJECT_HERMES_TASK_ID"),
        "repository": _required("PROJECT_HERMES_REPOSITORY"),
        "workspace_lease_id": _required("PROJECT_HERMES_WORKSPACE_LEASE_ID"),
        "base_sha": _required("PROJECT_HERMES_BASE_SHA"),
        "candidate_digest": _required("PROJECT_HERMES_CANDIDATE_DIGEST"),
    }
    metadata: dict[str, object] | None = None
    seen: set[str] = set()
    with tarfile.open(bundle, mode="r:gz") as archive:
        for member in archive:
            relative = PurePosixPath(member.name)
            if (
                relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
                or member.name in seen
            ):
                raise RuntimeError(f"unsafe source member: {member.name}")
            seen.add(member.name)
            if member.name == "bundle.json":
                if not member.isfile():
                    raise RuntimeError("source metadata is not a file")
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError("source metadata cannot be read")
                metadata = json.loads(stream.read())
                continue
            if relative.parts[0] != "worktree" or len(relative.parts) < 2:
                raise RuntimeError(
                    f"source member is outside worktree: {member.name}"
                )
            target = source_root.joinpath(*relative.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError(
                        f"source member cannot be read: {member.name}"
                    )
                with target.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(member.mode & 0o777)
            elif member.issym():
                _safe_symlink(member, relative)
                target.symlink_to(member.linkname)
            else:
                raise RuntimeError(
                    f"unsupported source member: {member.name}"
                )
    if metadata != expected_metadata:
        raise RuntimeError("source bundle task identity mismatch")
    git_environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": "/tmp",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_DATE": "1970-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "1970-01-01T00:00:00+00:00",
    }
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=project-hermes-base"],
        cwd=source_root,
        env=git_environment,
        check=True,
    )
    subprocess.run(
        ["git", "add", "--all"],
        cwd=source_root,
        env=git_environment,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=ProjectHermes Worker",
            "-c",
            "user.email=worker@project-hermes.invalid",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            f"Task source at {expected_metadata['base_sha']}",
        ],
        cwd=source_root,
        env=git_environment,
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
