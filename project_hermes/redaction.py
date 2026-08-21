"""Conservative redaction for controller-visible errors and metadata."""

from __future__ import annotations

import re
from typing import Any

_ASSIGNMENT = re.compile(
    r"(?i)(\\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"password|secret|credential)\\b\\s*[:=]\\s*)"
    r"([^\\s,;\"']+)"
)
_BEARER = re.compile(r"(?i)\\b(Bearer|Basic)\\s+[A-Za-z0-9._~+/=-]+")
_TOKEN = re.compile(
    r"\\b(?:sk|ghp|github_pat|hf|ms)-?[A-Za-z0-9_]{16,}\\b"
)
_URL_USERINFO = re.compile(
    r"(?P<scheme>https?://)[^/@\\s:]+:[^/@\\s]+@",
    re.IGNORECASE,
)


def redact_text(
    value: str,
    *,
    known_secrets: tuple[str, ...] = (),
) -> str:
    """Remove common credentials and explicitly known secret values."""

    redacted: str = value
    for secret in sorted(
        {secret for secret in known_secrets if secret},
        key=len,
        reverse=True,
    ):
        redacted = str(redacted).replace(str(secret), "<redacted>")
    redacted = _ASSIGNMENT.sub(r"\1<redacted>", redacted)
    redacted = _BEARER.sub(r"\1 <redacted>", redacted)
    redacted = _TOKEN.sub("<redacted>", redacted)
    return _URL_USERINFO.sub(r"\g<scheme><redacted>@", redacted)


def redact_data(
    value: Any,
    *,
    known_secrets: tuple[str, ...] = (),
) -> Any:
    """Recursively redact strings without changing the data shape."""

    if isinstance(value, str):
        return redact_text(value, known_secrets=known_secrets)
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if _looks_secret_name(str(key))
                else redact_data(item, known_secrets=known_secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            redact_data(item, known_secrets=known_secrets) for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            redact_data(item, known_secrets=known_secrets) for item in value
        )
    if isinstance(value, set):
        return {
            redact_data(item, known_secrets=known_secrets) for item in value
        }
    if isinstance(value, frozenset):
        return frozenset(
            redact_data(item, known_secrets=known_secrets) for item in value
        )
    return value


def _looks_secret_name(value: str) -> bool:
    normalized = value.casefold().replace("-", "_")
    return any(
        marker in normalized
        for marker in (
            "api_key",
            "access_token",
            "auth_token",
            "password",
            "secret",
            "credential",
        )
    )
