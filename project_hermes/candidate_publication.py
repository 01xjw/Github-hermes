"""Publish one immutable internal candidate as a GitHub Draft PR.

The worker intentionally has no GitHub credentials and its synthetic Git
history is not publishable.  This module reconstructs the reviewed production
diff on the controller's locked baseline inside a disposable clone, then
pushes only the controller-owned branch and asks ``gh`` to create a Draft PR.
The task workspace and Main Hermes execution state are never mutated.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from hermes_cli._subprocess_compat import noninteractive_git_env
from project_hermes.publication import (
    InternalCandidateLockState,
    InternalPullRequestCandidate,
    InternalPullRequestFile,
)

_COMMAND_TIMEOUT_SECONDS = 120
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PULL_REQUEST_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/([1-9][0-9]*)$"
)

CommandRunner = Callable[
    [Path, list[str], str | None, Mapping[str, str] | None],
    str,
]


def _run_command(
    cwd: Path,
    command: list[str],
    input_text: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    env = noninteractive_git_env()
    if environment:
        env.update(environment)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_COMMAND_TIMEOUT_SECONDS,
            stdin=None if input_text is not None else subprocess.DEVNULL,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"command could not be completed: {command[0]}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(detail or f"command failed: {command[0]}")
    return completed.stdout


def _git(
    runner: CommandRunner,
    cwd: Path,
    *arguments: str,
    input_text: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    return runner(cwd, ["git", *arguments], input_text, environment)


def _gh(
    runner: CommandRunner,
    cwd: Path,
    *arguments: str,
) -> str:
    return runner(
        cwd,
        ["gh", *arguments],
        None,
        {"GH_PROMPT_DISABLED": "1"},
    )


def _authenticated_git_arguments(*arguments: str) -> tuple[str, ...]:
    """Use gh as an ephemeral credential helper without changing Git config."""

    return (
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=!gh auth git-credential",
        *arguments,
    )


def _validate_candidate(candidate: InternalPullRequestCandidate) -> None:
    if candidate.lock_state is not InternalCandidateLockState.IMMUTABLE_APPROVED:
        raise PermissionError(
            "only an approved and immutable candidate can be published"
        )
    if candidate.lock_digest != candidate.solution_digest():
        raise PermissionError("candidate no longer matches its approval lock")
    if not _REPOSITORY_RE.fullmatch(candidate.repository):
        raise ValueError("candidate repository must use owner/name form")
    if not candidate.head_ref.startswith("project-hermes/"):
        raise ValueError("candidate branch is outside the ProjectHermes namespace")
    if any("\\" in item.path for item in candidate.files):
        raise ValueError("candidate file paths must use POSIX separators")
    unsupported = [
        item.path for item in candidate.files if item.status in {"C", "R", "U"}
    ]
    if unsupported:
        raise ValueError(
            "candidate contains unsupported copied, renamed, or conflicted files: "
            + ", ".join(unsupported)
        )


def _file_patch(file: InternalPullRequestFile) -> str:
    diff = file.diff.strip("\n")
    if not diff:
        raise ValueError(f"candidate file has no publishable diff: {file.path}")
    if diff.startswith("diff --git ") or diff.startswith("--- "):
        return diff + "\n"
    if not diff.startswith("@@ "):
        raise ValueError(f"candidate file has an unsupported diff: {file.path}")
    if any(character.isspace() for character in file.path):
        raise ValueError(
            f"headerless candidate diff cannot publish a whitespace path: {file.path}"
        )

    status = "A" if file.status == "?" else file.status
    before = "/dev/null" if status == "A" else f"a/{file.path}"
    after = "/dev/null" if status == "D" else f"b/{file.path}"
    mode = "new file mode 100644\n" if status == "A" else ""
    return (
        f"diff --git a/{file.path} b/{file.path}\n"
        f"{mode}--- {before}\n"
        f"+++ {after}\n"
        f"{diff}\n"
    )


def _candidate_patch(candidate: InternalPullRequestCandidate) -> str:
    return "".join(_file_patch(file) for file in candidate.files)


def _pull_request_payload(raw: dict[str, Any]) -> dict[str, Any]:
    state = (
        "merged"
        if raw.get("mergedAt")
        else "draft"
        if raw.get("isDraft")
        else str(raw.get("state") or "unknown").lower()
    )
    if state not in {"draft", "open", "merged", "closed"}:
        raise RuntimeError("gh returned an unknown pull request state")
    return {
        "url": str(raw.get("url") or ""),
        "number": int(raw.get("number") or 0),
        "title": str(raw.get("title") or ""),
        "state": state,
        "draft": bool(raw.get("isDraft")),
        "head_ref": str(raw.get("headRefName") or ""),
        "base_ref": str(raw.get("baseRefName") or ""),
    }


def _find_pull_request(
    runner: CommandRunner,
    cwd: Path,
    candidate: InternalPullRequestCandidate,
    github_repository: str,
) -> dict[str, Any] | None:
    output = _gh(
        runner,
        cwd,
        "pr",
        "list",
        "--repo",
        github_repository,
        "--head",
        candidate.head_ref,
        "--state",
        "all",
        "--limit",
        "10",
        "--json",
        "url,state,number,title,isDraft,headRefName,baseRefName,mergedAt",
    )
    try:
        values = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("gh returned an invalid pull request response") from exc
    if not isinstance(values, list):
        raise RuntimeError("gh returned an invalid pull request response")
    matching = [
        item
        for item in values
        if isinstance(item, dict)
        and item.get("headRefName") == candidate.head_ref
    ]
    return _pull_request_payload(matching[0]) if matching else None


def _remote_branch_sha(
    runner: CommandRunner,
    checkout: Path,
    head_ref: str,
) -> str | None:
    output = _git(
        runner,
        checkout,
        *_authenticated_git_arguments(
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{head_ref}",
        ),
    ).strip()
    if not output:
        return None
    sha, _, reference = output.partition("\t")
    if reference != f"refs/heads/{head_ref}" or not re.fullmatch(
        r"[0-9a-f]{40,64}", sha
    ):
        raise RuntimeError("remote returned an invalid candidate branch identity")
    return sha


def _assert_remote_tree_matches(
    runner: CommandRunner,
    checkout: Path,
    remote_sha: str,
    local_sha: str,
) -> None:
    _git(
        runner,
        checkout,
        *_authenticated_git_arguments("fetch", "--no-tags", "origin", remote_sha),
    )
    local_tree = _git(runner, checkout, "rev-parse", f"{local_sha}^{{tree}}").strip()
    remote_tree = _git(
        runner,
        checkout,
        "rev-parse",
        f"{remote_sha}^{{tree}}",
    ).strip()
    if local_tree != remote_tree:
        raise RuntimeError(
            "the remote ProjectHermes branch already exists with different content"
        )


def publish_internal_pull_request(
    candidate: InternalPullRequestCandidate,
    *,
    mirror_path: str | Path,
    remote_url: str | None = None,
    github_repository: str | None = None,
    runner: CommandRunner = _run_command,
) -> dict[str, Any]:
    """Push the frozen candidate diff and create (or return) its Draft PR."""

    _validate_candidate(candidate)
    mirror = Path(mirror_path).resolve()
    if not mirror.is_dir():
        raise ValueError("candidate repository mirror is unavailable")
    if shutil.which("git") is None or shutil.which("gh") is None:
        raise RuntimeError("publishing requires authenticated git and gh commands")

    resolved_github_repository = github_repository or candidate.repository
    if not _REPOSITORY_RE.fullmatch(resolved_github_repository):
        raise ValueError("GitHub repository must use owner/name form")
    canonical_remote = (
        remote_url
        or f"https://github.com/{resolved_github_repository}.git"
    )
    with tempfile.TemporaryDirectory(
        prefix=".project-hermes-publish-",
        dir=mirror.parent,
    ) as raw_temp:
        temporary = Path(raw_temp)
        checkout = temporary / "checkout"
        _gh(runner, temporary, "auth", "status", "--hostname", "github.com")
        _git(
            runner,
            temporary,
            "clone",
            "--no-checkout",
            "--local",
            str(mirror),
            str(checkout),
        )
        _git(runner, checkout, "remote", "set-url", "origin", canonical_remote)
        _git(runner, checkout, "check-ref-format", f"refs/heads/{candidate.head_ref}")
        _git(runner, checkout, "check-ref-format", f"refs/heads/{candidate.base_ref}")
        _git(runner, checkout, "checkout", "--detach", candidate.base_sha)

        patch = _candidate_patch(candidate)
        _git(
            runner,
            checkout,
            "apply",
            "--check",
            "--binary",
            "--whitespace=nowarn",
            "-",
            input_text=patch,
        )
        _git(
            runner,
            checkout,
            "apply",
            "--index",
            "--binary",
            "--whitespace=nowarn",
            "-",
            input_text=patch,
        )

        changed = {
            path
            for path in _git(
                runner,
                checkout,
                "diff",
                "--cached",
                "--name-only",
                "--no-renames",
                "-z",
            ).split("\0")
            if path
        }
        declared = {item.path for item in candidate.files}
        if changed != declared:
            raise RuntimeError(
                "applied candidate paths differ from the immutable review projection"
            )
        if _git(runner, checkout, "diff", "--name-only").strip():
            raise RuntimeError("candidate reconstruction left unstaged changes")
        if _git(
            runner,
            checkout,
            "ls-files",
            "--others",
            "--exclude-standard",
        ).strip():
            raise RuntimeError("candidate reconstruction left untracked files")

        author = candidate.commits[-1]
        author_name = author.author_name.strip() or "ProjectHermes Worker"
        author_email = (
            (author.author_email or "").strip()
            or "worker@project-hermes.invalid"
        )
        commit_environment = {
            "GIT_AUTHOR_DATE": author.authored_at.isoformat(),
            "GIT_COMMITTER_DATE": author.authored_at.isoformat(),
        }
        _git(
            runner,
            checkout,
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "-c",
            "commit.gpgSign=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "commit",
            "--no-verify",
            "-m",
            candidate.title,
            environment=commit_environment,
        )
        local_sha = _git(runner, checkout, "rev-parse", "HEAD").strip()

        existing = _find_pull_request(
            runner,
            checkout,
            candidate,
            resolved_github_repository,
        )
        remote_sha = _remote_branch_sha(runner, checkout, candidate.head_ref)
        if remote_sha:
            _assert_remote_tree_matches(runner, checkout, remote_sha, local_sha)
        elif existing is not None:
            # GitHub commonly deletes the source branch after a PR is closed or
            # merged.  Never recreate it merely because the operator revisited
            # an already-published result.
            return existing
        else:
            _git(
                runner,
                checkout,
                *_authenticated_git_arguments(
                    "push",
                    "origin",
                    f"HEAD:refs/heads/{candidate.head_ref}",
                ),
            )

        if existing is not None:
            return existing

        body_path = temporary / "pull-request-body.md"
        body_path.write_text(candidate.body + "\n", encoding="utf-8")
        try:
            output = _gh(
                runner,
                checkout,
                "pr",
                "create",
                "--repo",
                resolved_github_repository,
                "--draft",
                "--base",
                candidate.base_ref,
                "--head",
                candidate.head_ref,
                "--title",
                candidate.title,
                "--body-file",
                str(body_path),
            )
        except RuntimeError:
            raced = _find_pull_request(
                runner,
                checkout,
                candidate,
                resolved_github_repository,
            )
            if raced is not None:
                return raced
            raise
        created = _find_pull_request(
            runner,
            checkout,
            candidate,
            resolved_github_repository,
        )
        if created is not None:
            return created
        url = next(
            (
                line.strip()
                for line in reversed(output.splitlines())
                if _PULL_REQUEST_URL_RE.fullmatch(line.strip())
            ),
            "",
        )
        match = _PULL_REQUEST_URL_RE.fullmatch(url)
        if match is None:
            raise RuntimeError("GitHub created a Draft PR but returned no PR URL")
        return {
            "url": url,
            "number": int(match.group(1)),
            "title": candidate.title,
            "state": "draft",
            "draft": True,
            "head_ref": candidate.head_ref,
            "base_ref": candidate.base_ref,
        }


__all__ = ["publish_internal_pull_request"]
