"""Versioned, repository-scoped Skill resolution for isolated workers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import field_validator

from project_hermes.config import PollingConfig, PollingRepositoryConfig
from project_hermes.models import StrictModel


_MAX_SKILL_BYTES = 1024 * 1024
_MAX_SKILL_FILE_BYTES = 256 * 1024
_SKILL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class RepositorySkillReference(StrictModel):
    """Immutable identity of the Skill supplied to one Issue worker."""

    schema_version: Literal["repository-skill.v1"] = "repository-skill.v1"
    repository: str
    name: str
    digest: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _SKILL_NAME_RE.fullmatch(value):
            raise ValueError(
                "repository Skill name must use lowercase skill-name form"
            )
        return value

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("repository Skill digest must be lowercase SHA-256")
        return value


class RepositorySkillRegistry:
    """Resolve only the configured Skill for a configured repository."""

    def __init__(self, config: PollingConfig) -> None:
        self.root = config.repository_skills_path.resolve()
        self.required = config.require_repository_skills
        self._repositories = {
            item.repository.casefold(): item
            for item in config.repositories
            if item.enabled
        }

    def validate_all(self) -> tuple[RepositorySkillReference, ...]:
        return tuple(
            skill
            for repository in self._repositories.values()
            if (skill := self.resolve(repository.repository)) is not None
        )

    def resolve(self, repository: str) -> RepositorySkillReference | None:
        try:
            configured = self._repositories[repository.casefold()]
        except KeyError as exc:
            raise ValueError(
                "repository is outside the enabled Skill registry"
            ) from exc
        path = self.root / configured.resolved_skill_name
        if not path.is_dir():
            if self.required:
                raise FileNotFoundError(
                    f"repository Skill does not exist: {path}"
                )
            return None
        return _load_skill(configured, path)


def _load_skill(
    configured: PollingRepositoryConfig,
    path: Path,
) -> RepositorySkillReference:
    root = path.resolve()
    if root != path or path.is_symlink():
        raise ValueError("repository Skill root cannot be a symbolic link")
    skill_path = root / "SKILL.md"
    if not skill_path.is_file() or skill_path.is_symlink():
        raise ValueError("repository Skill requires a regular SKILL.md")
    text = skill_path.read_text(encoding="utf-8")
    metadata = _frontmatter(text)
    expected_name = configured.resolved_skill_name
    if metadata.get("name") != expected_name:
        raise ValueError(
            f"repository Skill name must be {expected_name!r}"
        )
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("repository Skill requires a description")
    return RepositorySkillReference(
        repository=configured.repository,
        name=expected_name,
        digest=repository_skill_digest(root),
    )


def _frontmatter(text: str) -> dict[str, object]:
    if not text.startswith("---\n"):
        raise ValueError("repository SKILL.md requires YAML frontmatter")
    marker = text.find("\n---\n", 4)
    if marker < 0:
        raise ValueError("repository SKILL.md frontmatter is not closed")
    raw = yaml.safe_load(text[4:marker]) or {}
    if not isinstance(raw, dict) or set(raw) != {"name", "description"}:
        raise ValueError(
            "repository SKILL.md frontmatter permits only name and description"
        )
    return raw


def repository_skill_digest(root: Path) -> str:
    """Return the canonical digest for one symlink-free Skill directory."""

    resolved = root.resolve()
    if resolved != root or root.is_symlink() or not root.is_dir():
        raise ValueError("repository Skill root must be a regular directory")
    entries = list(root.rglob("*"))
    for path in entries:
        if path.is_symlink():
            raise ValueError("repository Skill cannot contain symbolic links")
        if not path.is_file() and not path.is_dir():
            raise ValueError("repository Skill contains a non-regular entry")
    files = sorted(
        (path for path in entries if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    if not files:
        raise ValueError("repository Skill is empty")
    total = 0
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root)
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("repository Skill contains an unsafe path")
        size = path.stat().st_size
        total += size
        if size > _MAX_SKILL_FILE_BYTES or total > _MAX_SKILL_BYTES:
            raise ValueError("repository Skill exceeds the size boundary")
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


__all__ = [
    "RepositorySkillReference",
    "RepositorySkillRegistry",
    "repository_skill_digest",
]
