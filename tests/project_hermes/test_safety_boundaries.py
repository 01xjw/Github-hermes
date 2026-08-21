from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from project_hermes.assurance import KnowledgeCandidate, KnowledgeOutcome
from project_hermes.knowledge import (
    CleanupPlan,
    JsonKnowledgeRepository,
    KnowledgeLifecycle,
)
from project_hermes.models import IssueTask, ProjectRole
from project_hermes.policy import PolicyContext, PolicyEngine
from project_hermes.resource_managers import ArtifactCoordinator, GpuPool
from project_hermes.resources import ArtifactKind, ArtifactRequest, GpuRequest
from project_hermes.runtime.codex_daemon import OpenAICodexDriver
from project_hermes.runtime.codex_protocol import CodexTaskSpec
from project_hermes.work_graph import ActionKind, ActionRequest
from project_hermes.workspaces import GitWorkspaceManager


def test_policy_confines_paths_and_exact_repository_expansion(
    tmp_path: Path,
    issue_task: IssueTask,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    context = PolicyContext(
        task=issue_task,
        worktree_roots={"acme/kernel": worktree},
        approved_goal_revision_ids={"revision-1"},
        approved_repository_expansions={
            "revision-1": {"acme/documentation"}
        },
    )
    policy = PolicyEngine()
    escaped_write = policy.authorize(
        ActionRequest(
            request_id="escaped-write",
            task_id="task-1",
            requested_by=ProjectRole.CODEX,
            action=ActionKind.WRITE_WORKTREE,
            repository="acme/kernel",
            path=str(tmp_path / "outside.py"),
        ),
        context,
    )
    unrelated_expansion = policy.authorize(
        ActionRequest(
            request_id="unrelated-expansion",
            task_id="task-1",
            requested_by=ProjectRole.CODEX,
            action=ActionKind.READ_REPOSITORY,
            repository="acme/secrets",
            goal_revision_id="revision-1",
        ),
        context,
    )

    assert not escaped_write.allowed
    assert not unrelated_expansion.allowed


def test_controller_owned_resources_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", str(source)],
        check=True,
    )
    (source / "kernel.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "kernel.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=ProjectHermes Test",
            "-c",
            "user.email=project-hermes@example.test",
            "commit",
            "--quiet",
            "-m",
            "Initial fixture",
        ],
        check=True,
    )
    workspaces = GitWorkspaceManager(tmp_path / "workspaces")
    lease = workspaces.acquire(
        task_id="task-1",
        repository="acme/kernel",
        source=source,
        base_ref="main",
        owner_session_id="codex-session",
    )
    Path(lease.worktree_path, "kernel.py").write_text(
        "VALUE = 2\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="contains changes"):
        workspaces.release(lease, owner_session_id="codex-session")

    pool = GpuPool(["gpu-0"])
    gpu = pool.allocate(
        GpuRequest(request_id="gpu-1", task_id="task-1", count=1),
        execution_id="job-1",
    )
    assert gpu is not None
    with pytest.raises(PermissionError, match="confirmed"):
        pool.release(gpu.lease_id, execution_terminated=False)

    expected = b"expected artifact"
    artifact = ArtifactRequest(
        request_id="artifact-1",
        task_id="task-1",
        kind=ArtifactKind.MODEL,
        source_uri="controller://model",
        expected_digest="sha256:" + hashlib.sha256(expected).hexdigest(),
        destination="models/example",
    )
    coordinator = ArtifactCoordinator(tmp_path / "artifacts")
    coordinator.request(artifact)

    class TamperedProvider:
        def materialize(
            self,
            request: ArtifactRequest,
            staging_path: Path,
        ) -> Path:
            del request
            staging_path.write_bytes(b"tampered")
            return staging_path

    with pytest.raises(ValueError, match="digest mismatch"):
        coordinator.supply(artifact, TamperedProvider())


def test_codex_credential_channel_rejects_process_controls(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text(
        "environment:\n  PATH: /untrusted/bin\n",
        encoding="utf-8",
    )
    os.chmod(credentials, 0o600)
    spec = CodexTaskSpec(
        task_id="task-1",
        session_id="codex-session",
        worktree_path=worktree,
        runtime_root=tmp_path / "runtime",
        credentials_file=credentials,
    )
    driver = object.__new__(OpenAICodexDriver)
    driver._spec = spec

    with pytest.raises(ValueError, match="non-credential"):
        driver._load_credential_environment()


def test_knowledge_is_durable_before_private_state_is_deleted(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "runtime"
    task_state = private_root / "task-1"
    task_state.mkdir(parents=True)
    (task_state / "state.json").write_text("private", encoding="utf-8")
    tombstone_path = private_root / "tombstones" / "task-1.json"
    lifecycle = KnowledgeLifecycle(
        JsonKnowledgeRepository(tmp_path / "knowledge")
    )
    tombstone = lifecycle.finalize(
        [
            KnowledgeCandidate(
                candidate_id="knowledge-1",
                task_id="task-1",
                outcome=KnowledgeOutcome.POSITIVE,
                title="Bound the tile size",
                body="The bound avoids the observed resource overflow.",
                source_evidence_ids=["evidence-final"],
                code_diff_sha="abcdef1234",
            )
        ],
        validate=lambda candidate: bool(candidate.source_evidence_ids),
        validated_by="curator-session",
        cleanup=CleanupPlan(
            task_id="task-1",
            terminal_outcome="merged",
            allowed_root=private_root,
            delete_paths=[task_state],
            tombstone_path=tombstone_path,
        ),
    )

    assert not task_state.exists()
    persisted = json.loads(tombstone_path.read_text(encoding="utf-8"))
    assert persisted["knowledge_commit_id"] == tombstone.knowledge_commit_id
    assert list((tmp_path / "knowledge").rglob("*.json"))
