from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import pytest

from project_hermes.workspaces import GitWorkspaceManager


def _commit(source: Path, value: int) -> str:
    (source / "kernel.py").write_text(
        f"VALUE = {value}\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(source), "add", "kernel.py"],
        check=True,
    )
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
            f"Fixture {value}",
        ],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _source(tmp_path: Path) -> tuple[Path, str, str]:
    source = tmp_path / "source"
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", str(source)],
        check=True,
    )
    first = _commit(source, 1)
    second = _commit(source, 2)
    return source, first, second


def test_workspace_fetches_only_the_locked_baseline(tmp_path: Path) -> None:
    source, first, second = _source(tmp_path)
    workspaces = GitWorkspaceManager(tmp_path / "workspaces")

    lease = workspaces.acquire(
        task_id="task-1",
        repository="acme/kernel",
        source=source,
        base_ref=second,
        owner_session_id="owner-1",
    )

    assert lease.base_sha == second
    assert Path(lease.worktree_path, "kernel.py").read_text() == "VALUE = 2\n"
    missing_parent = subprocess.run(
        [
            "git",
            "--git-dir",
            lease.mirror_path,
            "cat-file",
            "-e",
            f"{first}^{{commit}}",
        ],
        capture_output=True,
        check=False,
    )
    assert missing_parent.returncode != 0


def test_cached_locked_baseline_does_not_require_remote(tmp_path: Path) -> None:
    source, _first, second = _source(tmp_path)
    workspaces = GitWorkspaceManager(tmp_path / "workspaces")
    first_lease = workspaces.acquire(
        task_id="task-1",
        repository="acme/kernel",
        source=source,
        base_ref=second,
        owner_session_id="owner-1",
    )
    workspaces.release(first_lease, owner_session_id="owner-1")
    unavailable_source = tmp_path / "source-unavailable"
    source.rename(unavailable_source)

    second_lease = workspaces.acquire(
        task_id="task-2",
        repository="acme/kernel",
        source=source,
        base_ref=second,
        owner_session_id="owner-2",
    )

    assert second_lease.base_sha == second
    assert Path(second_lease.worktree_path, "kernel.py").read_text() == ("VALUE = 2\n")


class _RecordingFetchWorkspaceManager(GitWorkspaceManager):
    def __init__(self, root: Path, *, failures: int) -> None:
        super().__init__(
            root,
            fetch_attempts=3,
            fetch_retry_backoff_seconds=0,
        )
        self.failures = failures
        self.git_calls: list[tuple[str, ...]] = []

    def _git(self, *arguments: str) -> str:
        self.git_calls.append(arguments)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("simulated transport interruption")
        return ""


def test_locked_baseline_fetch_retries_with_stable_http_policy(
    tmp_path: Path,
) -> None:
    workspaces = _RecordingFetchWorkspaceManager(tmp_path, failures=2)

    workspaces._fetch_locked_baseline(
        mirror=tmp_path / "mirror.git",
        base_ref="a" * 40,
        retained_ref="refs/project-hermes/baselines/test",
    )

    assert len(workspaces.git_calls) == 3
    for call in workspaces.git_calls:
        assert call[:4] == (
            "-c",
            "http.version=HTTP/1.1",
            "-c",
            "http.maxRequests=1",
        )
        assert "--depth=1" in call


def test_locked_baseline_fetch_reports_exhausted_attempts(
    tmp_path: Path,
) -> None:
    workspaces = _RecordingFetchWorkspaceManager(tmp_path, failures=3)

    with pytest.raises(
        RuntimeError,
        match=r"git fetch failed after 3 bounded attempt\(s\)",
    ):
        workspaces._fetch_locked_baseline(
            mirror=tmp_path / "mirror.git",
            base_ref="b" * 40,
            retained_ref="refs/project-hermes/baselines/test",
        )


def test_locked_baseline_fetch_clears_only_stale_fetch_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces = GitWorkspaceManager(
        tmp_path / "workspaces",
        fetch_attempts=2,
        fetch_retry_backoff_seconds=0,
    )
    mirror = tmp_path / "mirror.git"
    retained_ref = "refs/project-hermes/baselines/test"
    ref_lock = mirror / "refs/project-hermes/baselines/test.lock"
    fetch_locks = (
        mirror / "shallow.lock",
        mirror / "packed-refs.lock",
        mirror / "FETCH_HEAD.lock",
        ref_lock,
    )
    for path in fetch_locks:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale", encoding="utf-8")
    unrelated_lock = mirror / "index.lock"
    unrelated_lock.write_text("preserve", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def _git(*arguments: str) -> str:
        calls.append(arguments)
        assert all(not path.exists() for path in fetch_locks)
        if len(calls) == 1:
            for path in fetch_locks:
                path.write_text("failed-attempt", encoding="utf-8")
            raise RuntimeError("simulated interrupted shallow fetch")
        return ""

    monkeypatch.setattr(workspaces, "_git", _git)

    workspaces._fetch_locked_baseline(
        mirror=mirror,
        base_ref="c" * 40,
        retained_ref=retained_ref,
    )

    assert len(calls) == 2
    assert all(not path.exists() for path in fetch_locks)
    assert unrelated_lock.read_text(encoding="utf-8") == "preserve"


def test_git_commands_receive_the_configured_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Process:
        pid = 123
        returncode = 0

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            captured["timeout"] = timeout
            return b"", b""

    def _popen(*args: object, **kwargs: object) -> _Process:
        captured["args"] = args
        captured.update(kwargs)
        return _Process()

    monkeypatch.setattr(subprocess, "Popen", _popen)
    workspaces = GitWorkspaceManager(
        tmp_path / "workspaces",
        command_timeout_seconds=37,
    )

    workspaces._git("status")

    assert captured["timeout"] == 37
    assert captured["start_new_session"] is True


def test_timed_out_git_command_terminates_its_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"communicate": 0}
    signals: list[tuple[int, signal.Signals]] = []

    class _Process:
        pid = 456
        returncode = None

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            calls["communicate"] += 1
            if calls["communicate"] == 1:
                raise subprocess.TimeoutExpired("git", timeout or 0)
            return b"", b""

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: _Process())
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pid, value: signals.append((pid, value)),
    )
    workspaces = GitWorkspaceManager(
        tmp_path / "workspaces",
        command_timeout_seconds=1,
    )

    with pytest.raises(RuntimeError, match="timed out after 1 seconds"):
        workspaces._git("status")

    assert signals == [(456, signal.SIGTERM)]
    assert calls["communicate"] == 2
