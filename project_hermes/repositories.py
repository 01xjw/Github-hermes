"""Configured GitHub repository and immutable baseline resolution."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Mapping, Protocol, Sequence
from urllib.parse import quote, urlsplit

from pydantic import Field, field_validator, model_validator

from project_hermes.models import StrictModel, utc_now

_GIT_OBJECT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_UNSAFE_REF_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


class RepositoryMetadataClient(Protocol):
    """Read-only GitHub metadata surface used by baseline resolution."""

    def get(
        self,
        path_or_url: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        """Return decoded JSON and response headers."""


class RepositoryBaseline(StrictModel):
    """Validated clone identity and immutable baseline for one fixed repository."""

    schema_version: Literal["repository-baseline.v1"] = "repository-baseline.v1"
    repository: str
    github_repository: str | None = None
    clone_source: str
    default_branch: str
    baseline_sha: str
    resolved_at: datetime = Field(default_factory=utc_now)

    @field_validator("repository", "github_repository")
    @classmethod
    def validate_repository(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = value.split("/")
        if (
            len(parts) != 2
            or not all(parts)
            or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)
        ):
            raise ValueError("repository must use owner/name form")
        return value

    @field_validator("default_branch")
    @classmethod
    def validate_default_branch(cls, value: str) -> str:
        return _validate_branch(value)

    @field_validator("baseline_sha")
    @classmethod
    def validate_baseline_sha(cls, value: str) -> str:
        if not _GIT_OBJECT_RE.fullmatch(value):
            raise ValueError("baseline_sha must be a full lowercase Git object id")
        return value

    @model_validator(mode="after")
    def validate_clone_source(self) -> "RepositoryBaseline":
        parsed = urlsplit(self.clone_source)
        expected_path = f"/{self.github_repository or self.repository}.git"
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.casefold() != expected_path.casefold()
        ):
            raise ValueError(
                "clone_source must be the credential-free canonical GitHub URL"
            )
        if self.resolved_at.tzinfo is None:
            raise ValueError("resolved_at must be timezone-aware")
        return self

    @property
    def named_baseline(self) -> str:
        """Return the human-readable branch plus immutable object identity."""

        return f"{self.default_branch}@{self.baseline_sha}"


class GitHubRepositoryResolver:
    """Resolve immutable baselines only for configured polling repositories."""

    def __init__(
        self,
        client: RepositoryMetadataClient,
        *,
        repositories: Sequence[str],
        repository_aliases: Mapping[str, str] | None = None,
    ) -> None:
        canonical = tuple(dict.fromkeys(repository.strip() for repository in repositories))
        if not canonical:
            raise ValueError("repository resolver requires configured repositories")
        if len({repository.casefold() for repository in canonical}) != len(canonical):
            raise ValueError("repository resolver scope contains duplicates")
        self.client = client
        self.repositories = canonical
        self._repository_names = {
            repository.casefold(): repository for repository in canonical
        }
        aliases = {
            repository.strip().casefold(): github_repository.strip()
            for repository, github_repository in (repository_aliases or {}).items()
        }
        unknown_aliases = {
            repository
            for repository in aliases
            if repository not in self._repository_names
        }
        if unknown_aliases:
            raise ValueError(
                "repository aliases are outside the configured polling scope"
            )
        self._github_repositories = {
            key: aliases.get(key, repository)
            for key, repository in self._repository_names.items()
        }
        for repository in self._github_repositories.values():
            _validate_repository_name(repository)
        if len({
            repository.casefold()
            for repository in self._github_repositories.values()
        }) != len(self._github_repositories):
            raise ValueError("repository aliases contain duplicate GitHub targets")

    def resolve(self, repository: str) -> RepositoryBaseline:
        """Resolve canonical clone URL, default branch, and current commit SHA."""

        try:
            canonical = self._repository_names[repository.strip().casefold()]
        except KeyError as exc:
            raise ValueError(
                "repository is outside the configured polling scope"
            ) from exc
        github_repository = self._github_repositories[canonical.casefold()]
        raw_repository, _headers = self.client.get(f"/repos/{github_repository}")
        if not isinstance(raw_repository, dict):
            raise ValueError("GitHub repository metadata must be an object")
        if str(raw_repository.get("full_name") or "").casefold() != (
            github_repository.casefold()
        ):
            raise ValueError("GitHub repository identity differs from requested scope")
        clone_source = str(raw_repository.get("clone_url") or "")
        default_branch = str(raw_repository.get("default_branch") or "")
        _validate_clone_source(github_repository, clone_source)
        default_branch = _validate_branch(default_branch)

        raw_commit, _headers = self.client.get(
            f"/repos/{github_repository}/commits/"
            f"{quote(default_branch, safe='')}"
        )
        if not isinstance(raw_commit, dict):
            raise ValueError("GitHub commit metadata must be an object")
        baseline_sha = str(raw_commit.get("sha") or "")
        return RepositoryBaseline(
            repository=canonical,
            github_repository=(
                github_repository
                if github_repository.casefold() != canonical.casefold()
                else None
            ),
            clone_source=clone_source,
            default_branch=default_branch,
            baseline_sha=baseline_sha,
        )

    def resolve_all(self) -> tuple[RepositoryBaseline, ...]:
        """Resolve every configured repository in deterministic policy order."""

        return tuple(self.resolve(repository) for repository in self.repositories)


def _validate_clone_source(repository: str, value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.casefold() != f"/{repository}.git".casefold()
    ):
        raise ValueError(
            "GitHub clone source is not the canonical credential-free URL"
        )


def _validate_repository_name(value: str) -> None:
    parts = value.split("/")
    if (
        len(parts) != 2
        or not all(parts)
        or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)
    ):
        raise ValueError("repository aliases must use owner/name form")


def _validate_branch(value: str) -> str:
    if (
        len(value) > 255
        or not value
        or value.startswith(("-", ".", "/"))
        or value.endswith((".", "/"))
        or ".." in value
        or "@{" in value
        or _UNSAFE_REF_RE.search(value)
    ):
        raise ValueError("default_branch is not a safe Git branch name")
    return value


__all__ = [
    "GitHubRepositoryResolver",
    "RepositoryBaseline",
    "RepositoryMetadataClient",
]
