#!/usr/bin/env bash
set -euo pipefail

: "${PROJECT_HERMES_WORKER_RUNTIME:?set PROJECT_HERMES_WORKER_RUNTIME}"
if [ "$#" -eq 0 ]; then
  echo "worker command is required" >&2
  exit 64
fi

runtime="${PROJECT_HERMES_WORKER_RUNTIME}"
if [ ! -r "${runtime}/release.env" ] ||
   [ ! -x "${runtime}/venv/bin/python" ]; then
  echo "worker runtime was not prepared by prepare-runtime.sh" >&2
  exit 70
fi

set -a
# The file contains only installer-generated paths and a SHA-256 digest.
# shellcheck disable=SC1090
. "${runtime}/release.env"
set +a

export PATH="${runtime}/venv/bin:${PATH}"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_INDEX=1
export PYTHONDONTWRITEBYTECODE=1
exec "$@"
