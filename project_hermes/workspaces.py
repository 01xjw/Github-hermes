"""Controller-owned Git mirror and exclusive worktree management."""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from threading import RLock
from typing import Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from project_hermes.config import assert_private_directory
from project_hermes.models import utc_now
from project_hermes.resources import WorkspaceLease

_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FULL_GIT_ID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class GitWorkspaceManager:
    """Create one exclusive Git worktree per task and repository."""

    def __init__(
        self,
        root: str | Path,
        *,
        command_timeout_seconds: int = 900,
        fetch_attempts: int = 3,
        fetch_retry_backoff_seconds: int = 5,
    ) -> None:
        if command_timeout_seconds < 1:
            raise ValueError("command_timeout_seconds must be positive")
        if fetch_attempts < 1:
            raise ValueError("fetch_attempts must be positive")
        if fetch_retry_backoff_seconds < 0:
            raise ValueError("fetch_retry_backoff_seconds cannot be negative")
        self.root = Path(root).resolve()
        self.mirror_root = self.root / "mirrors"
        self.worktree_root = self.root / "worktrees"
        self.lease_root = self.root / "leases"
        self.command_timeout_seconds = command_timeout_seconds
        self.fetch_attempts = fetch_attempts
        self.fetch_retry_backoff_seconds = fetch_retry_backoff_seconds
        assert_private_directory(self.root)
        assert_private_directory(self.mirror_root)
        assert_private_directory(self.worktree_root)
        assert_private_directory(self.lease_root)
        self._leases = self._load_leases()
        self._lock = RLock()

    def acquire(
        self,
        *,
        task_id: str,
        repository: str,
        source: str | Path,
        base_ref: str,
        owner_session_id: str,
        allowed_existing_sources: Sequence[str | Path] = (),
    ) -> WorkspaceLease:
        """Update a controller mirror and create an isolated worktree."""

        if not _REPOSITORY_RE.fullmatch(repository):
            raise ValueError("repository must use owner/name form")
        if not task_id or not owner_session_id or not base_ref:
            raise ValueError(
                "task_id, owner_session_id, and base_ref are required"
            )
        parsed_source = urlsplit(str(source))
        if (
            parsed_source.username is not None
            or parsed_source.password is not None
        ):
            raise ValueError(
                "Git source cannot contain embedded credentials"
            )
        with self._lock:
            active = [
                lease
                for lease in self._leases.values()
                if lease.task_id == task_id
                and lease.repository == repository
                and lease.released_at is None
            ]
            if active:
                raise ValueError(
                    "an active worktree lease already exists for this task "
                    "and repository"
                )

            mirror = self._mirror_path(repository)
            base_sha = self._ensure_mirror(
                mirror,
                source,
                base_ref,
                allowed_existing_sources=allowed_existing_sources,
            )

            worktree = self._worktree_path(task_id, repository)
            if worktree.exists():
                raise FileExistsError(
                    f"task worktree path already exists: {worktree}"
                )
            assert_private_directory(worktree.parent)
            branch = (
                f"project-hermes/{_slug(task_id)}/"
                f"{_slug(repository.replace('/', '-'))}-{uuid4().hex[:10]}"
            )
            self._git(
                "--git-dir",
                str(mirror),
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree),
                base_sha,
            )
            os.chmod(worktree, 0o700)
            lease = WorkspaceLease(
                lease_id=f"workspace-{uuid4().hex}",
                task_id=task_id,
                repository=repository,
                mirror_path=str(mirror),
                worktree_path=str(worktree),
                branch_name=branch,
                base_sha=base_sha,
                owner_session_id=owner_session_id,
            )
            self._leases[lease.lease_id] = lease
            self._write_lease(lease)
            return lease

    def candidate_digest(self, lease: WorkspaceLease) -> str:
        """Hash tracked and untracked candidate changes without staging them."""

        self._require_active(lease)
        worktree = Path(lease.worktree_path)
        hasher = hashlib.sha256()
        hasher.update(lease.base_sha.encode("ascii"))
        diff = self._git_bytes(
            "-C",
            str(worktree),
            "diff",
            "--binary",
            "--no-ext-diff",
            lease.base_sha,
            "--",
        )
        hasher.update(diff)
        untracked = self._git_bytes(
            "-C",
            str(worktree),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).split(b"\0")
        for encoded_path in sorted(path for path in untracked if path):
            relative = encoded_path.decode("utf-8", errors="surrogateescape")
            candidate = worktree / relative
            resolved = candidate.resolve(strict=False)
            if not resolved.is_relative_to(worktree.resolve()):
                raise ValueError("untracked path escapes the leased worktree")
            if candidate.is_symlink():
                hasher.update(b"symlink\0")
                hasher.update(encoded_path)
                hasher.update(b"\0")
                hasher.update(os.readlink(candidate).encode("utf-8"))
            elif candidate.is_file():
                hasher.update(b"file\0")
                hasher.update(encoded_path)
                hasher.update(b"\0")
                with candidate.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
        return hasher.hexdigest()

    def assert_clean(self, lease: WorkspaceLease) -> None:
        """Require an active worktree to remain at its immutable baseline."""

        current = self._require_active(lease)
        status = self._git(
            "-C",
            current.worktree_path,
            "status",
            "--porcelain",
            "--untracked-files=all",
        )
        if status.strip():
            raise ValueError(
                "controller source worktree differs from its locked baseline"
            )

    def release(
        self,
        lease: WorkspaceLease,
        *,
        owner_session_id: str,
        discard_changes: bool = False,
    ) -> WorkspaceLease:
        """Remove an owned worktree without silently discarding changes."""

        with self._lock:
            current = self._require_active(lease)
            if current.owner_session_id != owner_session_id:
                raise PermissionError(
                    "workspace lease is owned by another session"
                )
            worktree = Path(current.worktree_path)
            if worktree.exists() and not discard_changes:
                status = self._git(
                    "-C",
                    str(worktree),
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                )
                if status.strip():
                    raise RuntimeError(
                        "worktree contains changes; preserve or explicitly "
                        "discard them before release"
                    )
            command = [
                "--git-dir",
                current.mirror_path,
                "worktree",
                "remove",
            ]
            if discard_changes:
                command.append("--force")
            command.append(current.worktree_path)
            if worktree.exists():
                self._git(*command)
            updated = WorkspaceLease.model_validate(
                current.model_copy(
                    update={"released_at": utc_now()}
                ).model_dump()
            )
            self._leases[current.lease_id] = updated
            self._write_lease(updated)
            return updated

    def get(self, lease_id: str) -> WorkspaceLease:
        with self._lock:
            try:
                return self._leases[lease_id]
            except KeyError as exc:
                raise KeyError(
                    f"unknown workspace lease: {lease_id}"
                ) from exc

    def active_for_task(self, task_id: str) -> list[WorkspaceLease]:
        """Return active task leases in stable repository order."""

        with self._lock:
            return sorted(
                (
                    lease
                    for lease in self._leases.values()
                    if lease.task_id == task_id
                    and lease.released_at is None
                ),
                key=lambda lease: (lease.repository, lease.lease_id),
            )

    def _load_leases(self) -> dict[str, WorkspaceLease]:
        leases: dict[str, WorkspaceLease] = {}
        for path in sorted(self.lease_root.glob("workspace-*.json")):
            lease = WorkspaceLease.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if path.stem != lease.lease_id:
                raise ValueError(
                    f"workspace lease filename does not match id: {path}"
                )
            leases[lease.lease_id] = lease
        return leases

    def _write_lease(self, lease: WorkspaceLease) -> None:
        path = self.lease_root / f"{lease.lease_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            lease.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def _ensure_mirror(
        self,
        mirror: Path,
        source: str | Path,
        base_ref: str,
        *,
        allowed_existing_sources: Sequence[str | Path] = (),
    ) -> str:
        """Materialize only the locked baseline needed by one worker.

        Polling resolves immutable Git object ids before a launch. Fetching an
        entire remote mirror here is both unnecessary and unbounded for large
        repositories with years of history and hundreds of thousands of refs.
        Keep one bare object store, fetch the requested baseline at depth one,
        and retain it under a controller-owned ref. A cached immutable object
        is reused without touching the network.
        """

        immutable_id = _FULL_GIT_ID_RE.fullmatch(base_ref) is not None
        if mirror.exists() and immutable_id:
            try:
                return self._git(
                    "--git-dir",
                    str(mirror),
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    f"{base_ref}^{{commit}}",
                ).strip()
            except RuntimeError:
                pass

        if mirror.exists():
            try:
                origin = self._git(
                    "--git-dir",
                    str(mirror),
                    "remote",
                    "get-url",
                    "origin",
                ).strip()
            except RuntimeError:
                self._git(
                    "--git-dir",
                    str(mirror),
                    "remote",
                    "add",
                    "origin",
                    str(source),
                )
            else:
                if origin != str(source):
                    allowed = {str(value) for value in allowed_existing_sources}
                    if origin not in allowed:
                        raise ValueError(
                            "controller mirror origin differs from the "
                            "configured repository source"
                        )
                    self._git(
                        "--git-dir",
                        str(mirror),
                        "remote",
                        "set-url",
                        "origin",
                        str(source),
                    )
        else:
            assert_private_directory(mirror.parent)
            self._git(
                "init",
                "--bare",
                "--initial-branch=main",
                str(mirror),
            )
            self._git(
                "--git-dir",
                str(mirror),
                "remote",
                "add",
                "origin",
                str(source),
            )

        retained_ref = (
            "refs/project-hermes/baselines/"
            + hashlib.sha256(base_ref.encode("utf-8")).hexdigest()
        )
        self._fetch_locked_baseline(
            mirror=mirror,
            base_ref=base_ref,
            retained_ref=retained_ref,
        )
        base_sha = self._git(
            "--git-dir",
            str(mirror),
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{retained_ref}^{{commit}}",
        ).strip()
        if immutable_id and base_sha != base_ref:
            raise ValueError("fetched baseline differs from the locked Git id")
        return base_sha

    def _fetch_locked_baseline(
        self,
        *,
        mirror: Path,
        base_ref: str,
        retained_ref: str,
    ) -> None:
        """Fetch one baseline with bounded retries on unreliable links."""

        self._remove_stale_fetch_locks(mirror, retained_ref)
        for attempt in range(1, self.fetch_attempts + 1):
            try:
                self._git(
                    "-c",
                    "http.version=HTTP/1.1",
                    "-c",
                    "http.maxRequests=1",
                    "--git-dir",
                    str(mirror),
                    "fetch",
                    "--no-tags",
                    "--depth=1",
                    "origin",
                    f"+{base_ref}:{retained_ref}",
                )
                return
            except RuntimeError as exc:
                # A failed or timed-out shallow fetch can leave Git lock files
                # behind even after its process tree has exited.  This mirror
                # is controller-owned and ``acquire`` serializes access under
                # ``self._lock``, so these exact fetch locks cannot belong to
                # another live operation here.  Clear them before retrying so
                # later attempts exercise the transport instead of failing
                # immediately on stale local state.
                self._remove_stale_fetch_locks(mirror, retained_ref)
                if attempt >= self.fetch_attempts:
                    raise RuntimeError(
                        "git fetch failed after "
                        f"{self.fetch_attempts} bounded attempt(s): {exc}"
                    ) from exc
                delay = self.fetch_retry_backoff_seconds * attempt
                if delay:
                    time.sleep(delay)

    @staticmethod
    def _remove_stale_fetch_locks(mirror: Path, retained_ref: str) -> None:
        """Remove only controller-owned locks used by one baseline fetch."""

        ref_path = Path(*retained_ref.split("/"))
        if ref_path.is_absolute() or ".." in ref_path.parts:
            raise ValueError("retained Git ref is not a safe relative path")
        mirror_root = mirror.resolve()
        lock_paths = (
            mirror / "shallow.lock",
            mirror / "packed-refs.lock",
            mirror / "FETCH_HEAD.lock",
            mirror / ref_path.parent / f"{ref_path.name}.lock",
        )
        for lock_path in lock_paths:
            if not lock_path.parent.resolve().is_relative_to(mirror_root):
                raise ValueError("fetch lock path escapes the controller mirror")
            try:
                lock_path.unlink(missing_ok=True)
            except OSError as exc:
                raise RuntimeError(
                    f"could not clear stale Git fetch lock: {lock_path.name}"
                ) from exc

    def _mirror_path(self, repository: str) -> Path:
        owner, name = repository.split("/", 1)
        return self.mirror_root / _slug(owner) / f"{_slug(name)}.git"

    def _worktree_path(self, task_id: str, repository: str) -> Path:
        digest = hashlib.sha256(
            f"{task_id}\0{repository}".encode("utf-8")
        ).hexdigest()[:12]
        return (
            self.worktree_root
            / f"{_slug(task_id)[:32]}-{digest}"
            / _slug(repository.replace("/", "-"))
        )

    def _require_active(self, lease: WorkspaceLease) -> WorkspaceLease:
        current = self.get(lease.lease_id)
        if current != lease:
            raise ValueError("workspace lease does not match controller state")
        if current.released_at is not None:
            raise ValueError("workspace lease is already released")
        root = self.worktree_root.resolve()
        worktree = Path(current.worktree_path).resolve(strict=False)
        if not worktree.is_relative_to(root):
            raise ValueError("workspace lease path escapes controller root")
        return current

    def _git(self, *arguments: str) -> str:
        result = self._run_git(*arguments)
        return result.stdout.decode("utf-8", errors="replace")

    def _git_bytes(self, *arguments: str) -> bytes:
        return self._run_git(*arguments).stdout

    def _run_git(
        self,
        *arguments: str,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        command = ["git", *arguments]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(
                timeout=self.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self._terminate_process_group(process)
            raise RuntimeError(
                f"git command timed out after {self.command_timeout_seconds} seconds"
            ) from exc
        returncode = int(process.returncode or 0)
        if returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            if not message:
                message = f"exit status {returncode}"
            raise RuntimeError(f"git command failed: {message}")
        return subprocess.CompletedProcess(
            args=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        """Stop a timed-out Git process and every transport helper it owns."""

        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:  # pragma: no cover - Kubernetes production is POSIX.
            process.terminate()
        try:
            process.communicate(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:  # pragma: no cover - Kubernetes production is POSIX.
            process.kill()
        process.communicate()


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    if not slug:
        raise ValueError("identifier cannot produce an empty path component")
    return slug
