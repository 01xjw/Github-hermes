from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from project_hermes.artifacts import SqliteArtifactRegistry
from project_hermes.release import load_active_release, verify_active_release
from project_hermes.repositories import GitHubRepositoryResolver
from project_hermes.supply_chain import TaskSourceBundleMetadata


class _FakeRepositoryClient:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def get(
        self,
        path_or_url: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        assert params is None
        self.paths.append(path_or_url)
        if path_or_url == "/repos/ROCm/ROCm":
            return (
                {
                    "full_name": "ROCm/ROCm",
                    "clone_url": "https://github.com/ROCm/ROCm.git",
                    "default_branch": "develop",
                },
                {},
            )
        if path_or_url == "/repos/ROCm/ROCm/commits/develop":
            return ({"sha": "a" * 40}, {})
        raise AssertionError(f"unexpected GitHub request: {path_or_url}")


def _release_fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    release_root = tmp_path / "release"
    for relative in (
        "wheelhouse/requirements.lock",
        "worker/prepare-runtime.sh",
        "worker/prepare-source.py",
        "worker/run.sh",
        "worker/execute-task.py",
    ):
        path = release_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    manifest = {
        "schema_version": "project-hermes-release.v1",
        "runtime_image": "docker.io/library/python@sha256:" + "a" * 64,
        "codex": {
            "sdk_version": "0.144.4",
            "cli_version": "0.144.4",
        },
    }
    encoded = (
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    (release_root / "release-manifest.json").write_bytes(encoded)
    (release_root / "RELEASE_DIGEST").write_text(
        digest + "\n",
        encoding="ascii",
    )
    state_path = tmp_path / "release.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "project-hermes-active-release.v1",
                "active_release_digest": digest,
                "previous_release_digest": None,
                "activated_at": "2026-08-13T08:00:00+00:00",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return release_root, state_path, digest


def test_configured_repository_resolver_returns_immutable_baseline() -> None:
    client = _FakeRepositoryClient()
    resolver = GitHubRepositoryResolver(client, repositories=("ROCm/ROCm",))

    baseline = resolver.resolve("rocm/rocm")

    assert baseline.repository == "ROCm/ROCm"
    assert baseline.clone_source == "https://github.com/ROCm/ROCm.git"
    assert baseline.named_baseline == f"develop@{'a' * 40}"
    assert client.paths == [
        "/repos/ROCm/ROCm",
        "/repos/ROCm/ROCm/commits/develop",
    ]
    with pytest.raises(ValueError, match="configured polling scope"):
        resolver.resolve("unapproved/repository")


def test_active_release_cross_checks_state_manifest_and_expected_digest(
    tmp_path: Path,
) -> None:
    release_root, state_path, digest = _release_fixture(tmp_path)

    verified = verify_active_release(
        release_root,
        state_path,
        expected_digest=digest,
    )

    assert verified.record.active_release_digest == digest
    assert verified.runtime_image.endswith("@sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="expected deployment"):
        verify_active_release(
            release_root,
            state_path,
            expected_digest="b" * 64,
        )

    (release_root / "release-manifest.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="manifest digest mismatch"):
        verify_active_release(release_root, state_path)


@pytest.mark.linux_only
def test_rendered_rollback_writes_schema_compatible_active_release(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    bundle, _state_path, rollback_digest = _release_fixture(
        tmp_path / "bundle"
    )
    current_digest = (
        "b" * 64 if rollback_digest != "b" * 64 else "c" * 64
    )
    installation_root = tmp_path / "installation"
    release_store = installation_root / "releases"
    rollback_release = release_store / "releases" / rollback_digest
    current_release = release_store / "releases" / current_digest
    rollback_release.mkdir(parents=True)
    current_release.mkdir(parents=True)
    (rollback_release / "release-manifest.json").write_bytes(
        (bundle / "release-manifest.json").read_bytes()
    )
    (release_store / "current").symlink_to(
        f"releases/{current_digest}",
        target_is_directory=True,
    )
    (release_store / "previous").symlink_to(
        f"releases/{rollback_digest}",
        target_is_directory=True,
    )
    state_root = installation_root / "state" / "hermes"
    state_root.mkdir(parents=True)
    rendered_root = tmp_path / "rendered"
    subprocess.run(
        [
            sys.executable,
            str(repository_root / "scripts/render_project_hermes_manifests.py"),
            str(bundle),
            "--output-dir",
            str(rendered_root),
            "--rollback-digest",
            rollback_digest,
        ],
        cwd=repository_root,
        check=True,
    )
    rendered_manifest = (
        rendered_root
        / f"project-hermes-{rollback_digest}"
        / "rollback-job.yaml"
    )
    document = yaml.safe_load(rendered_manifest.read_text(encoding="utf-8"))
    rollback_command = document["spec"]["template"]["spec"]["containers"][
        0
    ]["args"][0]
    environment = {
        **os.environ,
        "PROJECT_HERMES_ROLLBACK_ROOT": str(installation_root),
        "PROJECT_HERMES_ROLLBACK_PYTHON": sys.executable,
    }

    completed = subprocess.run(
        ["bash", "-lc", rollback_command],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    release_state = state_root / "release.json"
    record = load_active_release(release_state)
    assert record.active_release_digest == rollback_digest
    assert record.previous_release_digest == current_digest
    assert (release_store / "current").readlink() == Path(
        f"releases/{rollback_digest}"
    )
    assert (release_store / "previous").readlink() == Path(
        f"releases/{current_digest}"
    )
    resumed = subprocess.run(
        ["bash", "-lc", rollback_command],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert resumed.returncode == 0, resumed.stderr
    resumed_record = load_active_release(release_state)
    assert resumed_record.active_release_digest == rollback_digest
    assert resumed_record.previous_release_digest == current_digest
    assert set(json.loads(release_state.read_text(encoding="utf-8"))) == {
        "schema_version",
        "active_release_digest",
        "previous_release_digest",
        "activated_at",
    }


def test_source_bundle_registry_persists_and_rechecks_digest(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "kernel.py").write_text("VALUE = 1\n", encoding="utf-8")
    source_root = tmp_path / "source-bundles"
    database = tmp_path / "state" / "artifacts.db"
    metadata = TaskSourceBundleMetadata(
        task_id="task-1",
        repository="ROCm/ROCm",
        workspace_lease_id="workspace-1",
        base_sha="a" * 40,
        candidate_digest="b" * 64,
    )
    registry = SqliteArtifactRegistry(
        database,
        source_bundle_root=source_root,
    )

    record = registry.create_source_bundle(
        worktree,
        request_id="source-task-1",
        metadata=metadata,
    )
    reopened = SqliteArtifactRegistry(
        database,
        source_bundle_root=source_root,
    )

    assert reopened.get("source-task-1") == record.manifest
    assert reopened.require_source_bundle("source-task-1").metadata == metadata

    bundle = source_root / record.manifest.local_path
    os.chmod(bundle, 0o600)
    bundle.write_bytes(bundle.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="byte size|digest"):
        reopened.require_source_bundle("source-task-1")
