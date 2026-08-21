"""English-only policy checks for ProjectHermes-owned sources."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field

from project_hermes.models import StrictModel

# Project prose may use ordinary Unicode punctuation and mathematical symbols.
# These ranges target scripts that indicate untranslated prose.
_NON_ENGLISH_SCRIPT = re.compile(
    "["
    "\u0400-\u052f"  # Cyrillic
    "\u0600-\u06ff"  # Arabic
    "\u0750-\u077f"
    "\u08a0-\u08ff"
    "\u3040-\u30ff"  # Japanese
    "\u3400-\u4dbf"  # CJK extension A
    "\u4e00-\u9fff"  # CJK unified
    "\uac00-\ud7af"  # Hangul
    "]"
)

_TEXT_SUFFIXES = {
    ".cfg",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


class EnglishViolation(StrictModel):
    """One line containing a prohibited script."""

    path: str
    line: int = Field(ge=1)
    excerpt: str


def scan_english_only(paths: list[Path]) -> list[EnglishViolation]:
    """Scan ProjectHermes-owned text paths for untranslated prose."""

    violations: list[EnglishViolation] = []
    for file_path in _iter_text_files(paths):
        try:
            text = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = _NON_ENGLISH_SCRIPT.search(line)
            if match is None:
                continue
            start = max(0, match.start() - 40)
            end = min(len(line), match.end() + 40)
            violations.append(
                EnglishViolation(
                    path=str(file_path),
                    line=line_number,
                    excerpt=line[start:end],
                )
            )
    return violations


def _iter_text_files(paths: list[Path]):
    for path in paths:
        if path.is_file():
            if path.suffix.lower() in _TEXT_SUFFIXES:
                yield path
            continue
        if not path.is_dir():
            continue
        for candidate in sorted(path.rglob("*")):
            if any(
                part in {".git", "__pycache__", "node_modules"}
                for part in candidate.parts
            ):
                continue
            if (
                candidate.is_file()
                and candidate.suffix.lower() in _TEXT_SUFFIXES
            ):
                yield candidate
