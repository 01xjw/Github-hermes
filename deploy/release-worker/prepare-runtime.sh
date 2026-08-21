#!/usr/bin/env bash
set -euo pipefail

: "${PROJECT_HERMES_RELEASE_ROOT:?set PROJECT_HERMES_RELEASE_ROOT}"
: "${PROJECT_HERMES_RELEASE_DIGEST:?set PROJECT_HERMES_RELEASE_DIGEST}"
: "${PROJECT_HERMES_WORKER_RUNTIME:?set PROJECT_HERMES_WORKER_RUNTIME}"

case "${PROJECT_HERMES_RELEASE_DIGEST}" in
  (*[!0-9a-f]*|"")
    echo "PROJECT_HERMES_RELEASE_DIGEST is not lowercase hexadecimal" >&2
    exit 64
    ;;
esac
if [ "${#PROJECT_HERMES_RELEASE_DIGEST}" -ne 64 ]; then
  echo "PROJECT_HERMES_RELEASE_DIGEST must contain 64 characters" >&2
  exit 64
fi

release_root="$(readlink -f "${PROJECT_HERMES_RELEASE_ROOT}")"
manifest="${release_root}/release-manifest.json"
wheelhouse="${release_root}/wheelhouse"
requirements="${wheelhouse}/requirements.lock"
actual_digest="$(sha256sum "${manifest}" | awk '{print $1}')"
if [ "${actual_digest}" != "${PROJECT_HERMES_RELEASE_DIGEST}" ]; then
  echo "mounted release does not match PROJECT_HERMES_RELEASE_DIGEST" >&2
  exit 65
fi

case "${PROJECT_HERMES_WORKER_RUNTIME}" in
  (/*) ;;
  (*)
    echo "PROJECT_HERMES_WORKER_RUNTIME must be absolute" >&2
    exit 64
    ;;
esac
if [ -e "${PROJECT_HERMES_WORKER_RUNTIME}" ] &&
   [ -n "$(ls -A "${PROJECT_HERMES_WORKER_RUNTIME}")" ]; then
  echo "worker runtime must be an empty mounted directory" >&2
  exit 73
fi

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_INDEX=1
export PYTHONDONTWRITEBYTECODE=1
/opt/venv/bin/python -m venv --copies "${PROJECT_HERMES_WORKER_RUNTIME}/venv"
"${PROJECT_HERMES_WORKER_RUNTIME}/venv/bin/python" -m pip install \
  --no-compile \
  --no-deps \
  --no-index \
  --only-binary=:all: \
  --require-hashes \
  --find-links "${wheelhouse}/wheels" \
  --requirement "${requirements}"

"${PROJECT_HERMES_WORKER_RUNTIME}/venv/bin/python" - <<'PY'
import os
from importlib.metadata import version
from pathlib import Path
import sys

from codex_cli_bin import bundled_codex_path

expected = "0.144.4"
for distribution in ("openai-codex", "openai-codex-cli-bin"):
    actual = version(distribution)
    if actual != expected:
        raise SystemExit(
            f"{distribution} version mismatch: expected {expected}, got {actual}"
        )

binary = bundled_codex_path().resolve(strict=True)
if not os.access(binary, os.X_OK):
    raise SystemExit(f"packaged Codex binary is not executable: {binary}")
launcher = Path(sys.executable).parent / "codex"
if launcher.exists() or launcher.is_symlink():
    raise SystemExit(f"unexpected existing Codex launcher: {launcher}")
launcher.symlink_to(binary)
PY

if [ "$("${PROJECT_HERMES_WORKER_RUNTIME}/venv/bin/codex" --version)" \
     != "codex-cli 0.144.4" ]; then
  echo "prepared Codex launcher failed its version check" >&2
  exit 65
fi

umask 077
tmp="${PROJECT_HERMES_WORKER_RUNTIME}/release.env.tmp"
{
  printf 'PROJECT_HERMES_RELEASE_DIGEST=%s\n' \
    "${PROJECT_HERMES_RELEASE_DIGEST}"
  printf 'PROJECT_HERMES_RELEASE_ROOT=%s\n' "${release_root}"
} > "${tmp}"
mv -f "${tmp}" "${PROJECT_HERMES_WORKER_RUNTIME}/release.env"
chmod -R a-w "${PROJECT_HERMES_WORKER_RUNTIME}/venv"
echo "Prepared immutable worker runtime ${PROJECT_HERMES_RELEASE_DIGEST}"
