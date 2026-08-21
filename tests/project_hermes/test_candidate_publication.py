from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from project_hermes import candidate_publication
from project_hermes.api import (
    ActionContext,
    ApiPrincipal,
    build_project_hermes_router,
)
from project_hermes.candidate_publication import publish_internal_pull_request
from project_hermes.models import ProjectRole
from project_hermes.publication import (
    InternalCandidateLockState,
    InternalPullRequestCandidate,
    InternalPullRequestCommit,
    InternalPullRequestFile,
)


NOW = datetime(2026, 8, 21, 3, 0, tzinfo=UTC)


def _git(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repositories(tmp_path: Path) -> tuple[Path, Path, str, str]:
    source = tmp_path / "source"
    mirror = tmp_path / "mirror.git"
    remote = tmp_path / "remote.git"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Test Author")
    _git(source, "config", "user.email", "author@example.com")
    production = source / "src"
    production.mkdir()
    (production / "kernel.py").write_text("LIMIT = 0\n", encoding="utf-8")
    _git(source, "add", "src/kernel.py")
    _git(source, "commit", "-m", "baseline")
    base_sha = _git(source, "rev-parse", "HEAD")
    (production / "kernel.py").write_text("LIMIT = 1\n", encoding="utf-8")
    diff = _git(source, "diff", "--", "src/kernel.py") + "\n"
    _git(tmp_path, "clone", "--bare", str(source), str(mirror))
    _git(tmp_path, "clone", "--bare", str(source), str(remote))
    return mirror, remote, base_sha, diff


def _candidate(base_sha: str, diff: str) -> InternalPullRequestCandidate:
    candidate = InternalPullRequestCandidate(
        candidate_id="candidate-17",
        task_id="task-17",
        title="Fix the kernel boundary",
        body="## Summary\n\nKeep the kernel launch within its valid range.",
        repository="acme/kernel",
        base_ref="main",
        base_sha=base_sha,
        head_ref="project-hermes/task-17",
        head_sha="b" * 40,
        commits=[
            InternalPullRequestCommit(
                sha="b" * 40,
                message="Fix the kernel boundary",
                author_name="ProjectHermes Worker",
                author_email="worker@project-hermes.invalid",
                authored_at=NOW,
            )
        ],
        files=[
            InternalPullRequestFile(
                path="src/kernel.py",
                status="M",
                added=1,
                removed=1,
                diff=diff,
                necessity="Correct the reported production boundary.",
            )
        ],
        lock_state=InternalCandidateLockState.APPROVAL_PENDING,
        created_at=NOW,
        updated_at=NOW,
    )
    return candidate.immutable_copy(approved_by="operator", approved_at=NOW)


def test_publishes_locked_diff_without_mutating_the_controller_mirror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror, remote, base_sha, diff = _repositories(tmp_path)
    candidate = _candidate(base_sha, diff)
    created = False
    create_calls = 0

    def runner(
        cwd: Path,
        command: list[str],
        input_text: str | None,
        environment: Mapping[str, str] | None,
    ) -> str:
        nonlocal created, create_calls
        if command[0] != "gh":
            return candidate_publication._run_command(
                cwd,
                command,
                input_text,
                environment,
            )
        if command[1:3] == ["auth", "status"]:
            return ""
        if command[1:3] == ["pr", "list"]:
            return json.dumps(
                [
                    {
                        "url": "https://github.com/acme/kernel/pull/17",
                        "state": "OPEN",
                        "number": 17,
                        "title": candidate.title,
                        "isDraft": True,
                        "headRefName": candidate.head_ref,
                        "baseRefName": candidate.base_ref,
                        "mergedAt": None,
                    }
                ]
                if created
                else []
            )
        if command[1:3] == ["pr", "create"]:
            created = True
            create_calls += 1
            return "https://github.com/acme/kernel/pull/17\n"
        raise AssertionError(command)

    monkeypatch.setattr(
        candidate_publication.shutil,
        "which",
        lambda command: f"/usr/bin/{command}",
    )

    first = publish_internal_pull_request(
        candidate,
        mirror_path=mirror,
        remote_url=str(remote),
        runner=runner,
    )
    second = publish_internal_pull_request(
        candidate,
        mirror_path=mirror,
        remote_url=str(remote),
        runner=runner,
    )

    assert first["state"] == "draft"
    assert second["url"] == first["url"]
    assert create_calls == 1
    published_sha = _git(
        tmp_path,
        "--git-dir",
        str(remote),
        "rev-parse",
        f"refs/heads/{candidate.head_ref}",
    )
    assert published_sha
    assert (
        _git(tmp_path, "--git-dir", str(mirror), "rev-parse", base_sha)
        == base_sha
    )
    assert candidate.head_ref not in _git(
        tmp_path,
        "--git-dir",
        str(mirror),
        "show-ref",
    )


def test_rejects_candidate_before_immutable_approval(tmp_path: Path) -> None:
    mirror, _remote, base_sha, diff = _repositories(tmp_path)
    candidate = _candidate(base_sha, diff).model_copy(
        update={
            "lock_state": InternalCandidateLockState.APPROVAL_PENDING,
            "lock_digest": None,
            "locked_by": None,
            "locked_at": None,
        }
    )

    with pytest.raises(PermissionError, match="approved and immutable"):
        publish_internal_pull_request(candidate, mirror_path=mirror)


def test_publish_api_passes_the_confirmed_immutable_candidate() -> None:
    diff = (
        "diff --git a/src/kernel.py b/src/kernel.py\n"
        "--- a/src/kernel.py\n"
        "+++ b/src/kernel.py\n"
        "@@ -1 +1 @@\n"
        "-LIMIT = 0\n"
        "+LIMIT = 1\n"
    )
    candidate = _candidate("a" * 40, diff)
    published: list[tuple[str, str]] = []

    class CandidateStore:
        def get(self, candidate_id: str) -> InternalPullRequestCandidate:
            assert candidate_id == candidate.candidate_id
            return candidate

    def publish(
        approved: InternalPullRequestCandidate,
        requested_by: str,
    ) -> dict[str, object]:
        published.append((approved.candidate_id, requested_by))
        return {
            "url": "https://github.com/acme/kernel/pull/17",
            "number": 17,
            "title": approved.title,
            "state": "draft",
            "draft": True,
            "head_ref": approved.head_ref,
            "base_ref": approved.base_ref,
        }

    app = FastAPI()
    app.include_router(
        build_project_hermes_router(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            authenticate=lambda _request: ApiPrincipal(
                subject="operator-session",
                role=ProjectRole.OPERATOR,
            ),
            action_context=lambda _run_id: ActionContext(),
            candidates=CandidateStore(),  # type: ignore[arg-type]
            candidate_publisher=publish,
        ),
        prefix="/api",
    )

    response = TestClient(app).post(
        "/api/v2/project-hermes/pull-request-candidates/candidate-17/publish",
        json={
            "schema_version": "publish-internal-pr-request.v1",
            "confirmed_lock_digest": candidate.lock_digest,
            "confirm": True,
        },
    )

    assert response.status_code == 201
    assert response.json()["url"] == "https://github.com/acme/kernel/pull/17"
    assert published == [(candidate.candidate_id, "operator-session")]
