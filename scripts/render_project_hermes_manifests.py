#!/usr/bin/env python3
"""Render immutable ProjectHermes Kubernetes manifests from a release bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_ROOT = REPO_ROOT / "deploy" / "kubernetes"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
IMAGE_DIGEST_RE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
RELEASE_TOKEN = "@PROJECT_HERMES_RELEASE_DIGEST@"
IMAGE_TOKEN = "@PROJECT_HERMES_RUNTIME_IMAGE@"
ROLLBACK_TOKEN = "@PROJECT_HERMES_ROLLBACK_DIGEST@"


class RenderError(RuntimeError):
    """A manifest rendering input or template error."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_release(bundle: Path) -> tuple[str, str]:
    digest_path = bundle / "RELEASE_DIGEST"
    manifest_path = bundle / "release-manifest.json"
    digest = digest_path.read_text(encoding="ascii").strip()
    if not SHA256_RE.fullmatch(digest):
        raise RenderError("release bundle has an invalid RELEASE_DIGEST")
    if _sha256(manifest_path) != digest:
        raise RenderError("release manifest does not match RELEASE_DIGEST")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    image = str(manifest.get("runtime_image", ""))
    if not IMAGE_DIGEST_RE.fullmatch(image):
        raise RenderError("release manifest runtime_image is not digest-pinned")
    return digest, image


def _render(
    source: Path,
    destination: Path,
    *,
    replacements: dict[str, str],
) -> None:
    text = source.read_text(encoding="utf-8")
    for token, value in replacements.items():
        count = text.count(token)
        if count == 0:
            raise RenderError(f"{source.name} does not contain required token {token}")
        text = text.replace(token, value)
    unresolved = sorted(set(re.findall(r"@[A-Z0-9_]+@", text)))
    if unresolved:
        raise RenderError(
            f"{source.name} contains unresolved tokens: {', '.join(unresolved)}"
        )
    destination.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT.parent / "project-hermes-rendered",
    )
    parser.add_argument(
        "--rollback-digest",
        help="also render rollback-job.yaml for this retained release",
    )
    args = parser.parse_args()
    try:
        bundle = args.bundle.resolve()
        release_digest, runtime_image = _load_release(bundle)
        if args.rollback_digest and not SHA256_RE.fullmatch(args.rollback_digest):
            raise RenderError("--rollback-digest must be a lowercase SHA-256 digest")
        if args.rollback_digest and args.rollback_digest != release_digest:
            raise RenderError(
                "--rollback-digest must match the supplied previous release bundle"
            )
        output_root = args.output_dir.resolve()
        if output_root == REPO_ROOT or output_root.is_relative_to(REPO_ROOT):
            raise RenderError("render output must be outside Github_Hermes")
        output_root.mkdir(parents=True, exist_ok=True)
        destination = output_root / f"project-hermes-{release_digest}"
        if destination.exists():
            raise RenderError(f"rendered directory already exists: {destination}")
        with tempfile.TemporaryDirectory(
            prefix=".project-hermes-render-",
            dir=output_root,
        ) as temporary:
            stage = Path(temporary)
            replacements = {
                RELEASE_TOKEN: release_digest,
                IMAGE_TOKEN: runtime_image,
            }
            for name in (
                "install-job.yaml",
                "github-agent-pod.yaml",
                "github-agent-job-runner.yaml",
            ):
                _render(
                    TEMPLATE_ROOT / name,
                    stage / name,
                    replacements=replacements,
                )
            for name in ("project-hermes.cluster.yaml",):
                shutil.copyfile(TEMPLATE_ROOT / name, stage / name)
            if args.rollback_digest:
                _render(
                    TEMPLATE_ROOT / "rollback-job.yaml",
                    stage / "rollback-job.yaml",
                    replacements={
                        IMAGE_TOKEN: runtime_image,
                        ROLLBACK_TOKEN: args.rollback_digest,
                    },
                )
            shutil.move(str(stage), destination)
        print(f"Rendered release manifests: {destination}")
    except (OSError, RenderError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
