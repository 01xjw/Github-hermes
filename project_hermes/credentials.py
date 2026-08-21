"""Secure loading for ProjectHermes credential environment files."""

from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Mapping

import yaml

from project_hermes.config import is_credential_environment_name

MAX_CREDENTIAL_FILE_BYTES = 65_536
CONTROLLER_CREDENTIAL_SECRET_FILES = (
    ("MODEL_ACCESS_KEY", "model-access-key"),
    ("MINIMAX_CN_API_KEY", "minimax-api-key"),
    ("DEEPSEEK_API_KEY", "deepseek-api-key"),
)


def write_credentials_environment(
    path: str | Path,
    environment: Mapping[str, str],
) -> None:
    """Atomically write a private controller credential environment file."""

    validated: dict[str, str] = {}
    for key, value in environment.items():
        if not isinstance(key, str) or not is_credential_environment_name(key):
            raise ValueError(
                "credentials_file contains a non-credential environment name"
            )
        if not isinstance(value, str) or not value:
            raise ValueError(
                "credential environment values must be non-empty strings"
            )
        validated[key] = value
    if not validated:
        raise ValueError("credential environment cannot be empty")

    destination = Path(path)
    if not destination.parent.is_dir():
        raise ValueError("credentials_file parent directory does not exist")
    payload = (
        json.dumps(
            {"environment": validated},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    if len(payload.encode("utf-8")) > MAX_CREDENTIAL_FILE_BYTES:
        raise ValueError("credentials_file exceeds 64 KiB")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def install_controller_credentials(
    secret_directory: str | Path,
    destination: str | Path,
) -> None:
    """Install every controller model credential from a Secret projection."""

    root = Path(secret_directory)
    environment: dict[str, str] = {}
    for environment_name, secret_file_name in CONTROLLER_CREDENTIAL_SECRET_FILES:
        secret_path = root / secret_file_name
        try:
            contents = secret_path.read_bytes()
        except FileNotFoundError as exc:
            raise ValueError(
                f"controller credential secret is missing: {environment_name}"
            ) from exc
        if len(contents) > MAX_CREDENTIAL_FILE_BYTES:
            raise ValueError(
                f"controller credential secret is too large: {environment_name}"
            )
        try:
            value = contents.decode("utf-8").replace("\r", "").replace("\n", "")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"controller credential secret is not UTF-8: {environment_name}"
            ) from exc
        if not value:
            raise ValueError(
                f"controller credential secret is empty: {environment_name}"
            )
        environment[environment_name] = value
    write_credentials_environment(destination, environment)


def read_credentials_environment(path: str | Path) -> dict[str, str]:
    """Read the sole credential environment mapping from private YAML."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(Path(path), flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(
                "credentials_file must be a regular, non-symlink file"
            ) from exc
        raise

    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError(
                "credentials_file must be a regular, non-symlink file"
            )
        if stat.S_IMODE(details.st_mode) & 0o077:
            raise ValueError(
                "credentials_file permissions must be 0600 or stricter"
            )
        if details.st_uid != os.geteuid():
            raise ValueError(
                "credentials_file must be owned by the current user"
            )
        if details.st_size > MAX_CREDENTIAL_FILE_BYTES:
            raise ValueError("credentials_file exceeds 64 KiB")

        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            contents = stream.read(MAX_CREDENTIAL_FILE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(contents.encode("utf-8")) > MAX_CREDENTIAL_FILE_BYTES:
        raise ValueError("credentials_file exceeds 64 KiB")
    raw = yaml.safe_load(contents)
    if not isinstance(raw, dict) or set(raw) != {"environment"}:
        raise ValueError(
            "credentials_file may contain only an 'environment' mapping"
        )
    environment = raw["environment"]
    if not isinstance(environment, dict):
        raise ValueError(
            "credentials_file must contain an 'environment' mapping"
        )

    result: dict[str, str] = {}
    for key, value in environment.items():
        if not isinstance(key, str) or not is_credential_environment_name(key):
            raise ValueError(
                "credentials_file contains a non-credential environment name"
            )
        if not isinstance(value, str) or not value:
            raise ValueError(
                "credential environment values must be non-empty strings"
            )
        result[key] = value
    return result
