#!/usr/bin/env python3
"""Build and validate deterministic, local-only ProjectHermes releases."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PARITY_ROOT = REPO_ROOT.parent / "ProjectHermes"
PINNED_CODEX_VERSION = "0.144.4"
RELEASE_SCHEMA = "project-hermes-release.v1"
ARTIFACT_NAMES = (
    "source.tar.gz",
    "web.tar.gz",
    "wheelhouse.tar.gz",
    "worker.tar.gz",
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")
GIT_OBJECT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
IMAGE_DIGEST_RE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
GENERATED_PATH_PARTS = frozenset({"__pycache__", ".pytest_cache"})
GENERATED_SUFFIXES = frozenset({".pyc", ".pyo"})


class ReleaseError(RuntimeError):
    """A fail-closed release packaging or validation error."""


def _run(
    command: Sequence[str],
    *,
    cwd: Path = REPO_ROOT,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> str:
    print("+", shlex.join(command))
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=None,
    )
    return completed.stdout if capture else ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _canonical_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ReleaseError(f"unsafe archive path: {value!r}")
    return path


def _source_date_epoch(explicit: int | None) -> int:
    if explicit is not None:
        if explicit < 0:
            raise ReleaseError("SOURCE_DATE_EPOCH cannot be negative")
        return explicit
    configured = os.environ.get("SOURCE_DATE_EPOCH")
    if configured:
        try:
            value = int(configured)
        except ValueError as exc:
            raise ReleaseError("SOURCE_DATE_EPOCH must be an integer") from exc
        if value < 0:
            raise ReleaseError("SOURCE_DATE_EPOCH cannot be negative")
        return value
    output = _run(
        ["git", "log", "-1", "--format=%ct"],
        capture=True,
    ).strip()
    if not output.isdecimal():
        raise ReleaseError("cannot derive SOURCE_DATE_EPOCH from Git")
    return int(output)


def _git_source_files() -> list[Path]:
    output = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    paths: list[Path] = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        _safe_relative_path(relative.as_posix())
        if _is_generated_path(relative):
            continue
        absolute = REPO_ROOT / relative
        if not absolute.is_file() and not absolute.is_symlink():
            raise ReleaseError(f"source entry is not a file: {relative}")
        paths.append(relative)
    return sorted(set(paths), key=lambda path: path.as_posix())


def _tree_files(root: Path) -> list[Path]:
    return sorted(
        (
            path.relative_to(root)
            for path in root.rglob("*")
            if path.is_file() or path.is_symlink()
        ),
        key=lambda path: path.as_posix(),
    )


def _is_generated_path(relative: Path) -> bool:
    return (
        any(part in GENERATED_PATH_PARTS for part in relative.parts)
        or relative.suffix.casefold() in GENERATED_SUFFIXES
    )


def _releasable_tree_files(root: Path) -> list[Path]:
    return [
        relative
        for relative in _tree_files(root)
        if not _is_generated_path(relative)
    ]


def _shared_tree_files(root: Path) -> list[Path]:
    return _releasable_tree_files(root)


def _validate_symlink(root: Path, relative: Path) -> str:
    target = os.readlink(root / relative)
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        raise ReleaseError(f"absolute symlink is not releasable: {relative}")
    resolved = (root / relative.parent / target).resolve(strict=False)
    if not resolved.is_relative_to(root.resolve()):
        raise ReleaseError(f"escaping symlink is not releasable: {relative}")
    return target


def _create_archive(
    destination: Path,
    *,
    root: Path,
    files: Iterable[Path],
    prefix: str,
    epoch: int,
) -> None:
    prefix_path = _safe_relative_path(prefix)
    with destination.open("wb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw,
            compresslevel=9,
            mtime=epoch,
        ) as compressed:
            with tarfile.open(
                mode="w",
                fileobj=compressed,
                format=tarfile.PAX_FORMAT,
            ) as archive:
                for relative in files:
                    source = root / relative
                    archive_name = (prefix_path / relative.as_posix()).as_posix()
                    info = tarfile.TarInfo(archive_name)
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = epoch
                    info.pax_headers = {}
                    if source.is_symlink():
                        info.type = tarfile.SYMTYPE
                        info.mode = 0o777
                        info.linkname = _validate_symlink(root, relative)
                        archive.addfile(info)
                        continue
                    source_stat = source.stat()
                    info.type = tarfile.REGTYPE
                    info.mode = (
                        0o755
                        if (
                            source_stat.st_mode
                            & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                            or (
                                prefix_path.parts[0] == "worker"
                                and relative.suffix == ".sh"
                            )
                        )
                        else 0o644
                    )
                    info.size = source_stat.st_size
                    with source.open("rb") as stream:
                        archive.addfile(info, stream)


def _compare_shared_tree(reference_root: Path) -> None:
    if not reference_root.is_dir():
        raise ReleaseError(f"ProjectHermes parity root is missing: {reference_root}")
    shared_roots = (Path("project_hermes"), Path("tests/project_hermes"))
    differences: list[str] = []
    for shared_root in shared_roots:
        source_root = REPO_ROOT / shared_root
        reference = reference_root / shared_root
        source_files = set(_shared_tree_files(source_root))
        reference_files = set(_shared_tree_files(reference))
        for missing in sorted(source_files - reference_files):
            differences.append(f"missing from parity root: {shared_root / missing}")
        for extra in sorted(reference_files - source_files):
            differences.append(f"missing from Github_Hermes: {shared_root / extra}")
        for relative in sorted(source_files & reference_files):
            source = source_root / relative
            other = reference / relative
            if source.is_symlink() != other.is_symlink():
                differences.append(f"type mismatch: {shared_root / relative}")
            elif source.is_symlink():
                if os.readlink(source) != os.readlink(other):
                    differences.append(f"symlink mismatch: {shared_root / relative}")
            elif _sha256(source) != _sha256(other):
                differences.append(f"byte mismatch: {shared_root / relative}")
    if differences:
        sample = "\n".join(f"  - {item}" for item in differences[:25])
        suffix = (
            f"\n  - ... {len(differences) - 25} more"
            if len(differences) > 25
            else ""
        )
        raise ReleaseError(
            "shared ProjectHermes files differ; Github_Hermes is the release "
            f"source of truth:\n{sample}{suffix}"
        )


def _verify_codex_pins() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    for distribution in ("openai-codex", "openai-codex-cli-bin"):
        pin = f'"{distribution}=={PINNED_CODEX_VERSION}"'
        if pin not in pyproject:
            raise ReleaseError(f"missing exact pyproject pin: {pin}")
        lock_entry = (
            f'name = "{distribution}"\nversion = "{PINNED_CODEX_VERSION}"'
        )
        if lock_entry not in lock:
            raise ReleaseError(
                f"uv.lock does not contain {distribution} {PINNED_CODEX_VERSION}"
            )


def _wheel_identity(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as wheel:
        metadata_names = [
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ReleaseError(f"wheel has ambiguous METADATA: {path.name}")
        metadata = wheel.read(metadata_names[0]).decode("utf-8")
    name: str | None = None
    version: str | None = None
    for line in metadata.splitlines():
        if line.startswith("Name: "):
            name = line.removeprefix("Name: ").strip()
        elif line.startswith("Version: "):
            version = line.removeprefix("Version: ").strip()
        if name and version:
            break
    if not name or not version:
        raise ReleaseError(f"wheel metadata lacks name or version: {path.name}")
    return _canonical_distribution_name(name), version


def _write_wheel_requirements(wheels: Path) -> dict[str, str]:
    identities: dict[str, tuple[str, str]] = {}
    lines: list[str] = []
    for wheel in sorted(wheels.glob("*.whl"), key=lambda path: path.name):
        name, version = _wheel_identity(wheel)
        if name in identities:
            raise ReleaseError(
                f"wheelhouse contains duplicate distribution {name}: "
                f"{identities[name][0]} and {wheel.name}"
            )
        digest = _sha256(wheel)
        identities[name] = (wheel.name, version)
        lines.append(f"{name}=={version} --hash=sha256:{digest}")
    if not identities:
        raise ReleaseError("wheelhouse is empty")
    for codex_name in ("openai-codex", "openai-codex-cli-bin"):
        identity = identities.get(codex_name)
        if identity is None or identity[1] != PINNED_CODEX_VERSION:
            raise ReleaseError(
                f"wheelhouse must contain {codex_name} {PINNED_CODEX_VERSION}"
            )
    (wheels.parent / "requirements.lock").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    return {name: version for name, (_, version) in identities.items()}


def _compatible_pip_platforms(target: str) -> list[str]:
    match = re.fullmatch(r"manylinux_2_([0-9]+)_([A-Za-z0-9_]+)", target)
    if match is None:
        return [target]
    glibc_minor = int(match.group(1))
    architecture = match.group(2)
    platforms = [
        f"manylinux_2_{minor}_{architecture}"
        for minor in range(glibc_minor, 4, -1)
    ]
    legacy = {
        17: "manylinux2014",
        12: "manylinux2010",
        5: "manylinux1",
    }
    platforms.extend(
        f"{name}_{architecture}"
        for minimum, name in legacy.items()
        if glibc_minor >= minimum
    )
    return list(dict.fromkeys(platforms))


def _git_metadata() -> tuple[str, bool]:
    commit = _run(["git", "rev-parse", "HEAD"], capture=True).strip()
    if not GIT_OBJECT_RE.fullmatch(commit):
        raise ReleaseError("Git HEAD is not a full Git object identifier")
    status = _run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        capture=True,
    )
    return commit, bool(status)


def _build_release(args: argparse.Namespace) -> Path | None:
    _verify_codex_pins()
    _compare_shared_tree(args.parity_root.resolve())
    if not IMAGE_DIGEST_RE.fullmatch(args.runtime_image):
        raise ReleaseError(
            "--runtime-image must be an immutable OCI reference using @sha256"
        )
    epoch = _source_date_epoch(args.source_date_epoch)
    source_files = _git_source_files()
    print(f"ProjectHermes parity verified against {args.parity_root}")
    print(f"Source archive will contain {len(source_files)} files")
    if args.dry_run:
        print("+ npm ci --ignore-scripts")
        print("+ npm run build --workspace web")
        print(
            "+ uv export --quiet --frozen --no-dev --extra web --extra "
            "project-hermes --no-emit-project --format requirements-txt "
            "--python 3.12"
        )
        print("+ python -m pip download --require-hashes --only-binary=:all: ...")
        print("Dry run complete; no files or build outputs were created.")
        return None

    output_root = args.output_dir.resolve()
    if output_root == REPO_ROOT or output_root.is_relative_to(REPO_ROOT):
        raise ReleaseError("release output directory must be outside Github_Hermes")
    output_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = str(epoch)
    env["PYTHONHASHSEED"] = "0"
    env["TZ"] = "UTC"
    with tempfile.TemporaryDirectory(
        prefix=".project-hermes-release-",
        dir=output_root,
    ) as temporary:
        stage = Path(temporary)
        export_file = stage / "python-requirements.export.txt"
        wheelhouse = stage / "wheelhouse"
        wheels = wheelhouse / "wheels"
        wheels.mkdir(parents=True)

        _run(["npm", "ci", "--ignore-scripts"], env=env)
        _run(["npm", "run", "build", "--workspace", "web"], env=env)
        web_dist = REPO_ROOT / "hermes_cli" / "web_dist"
        if not (web_dist / "index.html").is_file():
            raise ReleaseError("web build did not create hermes_cli/web_dist")

        _run(
            [
                "uv",
                "export",
                "--quiet",
                "--frozen",
                "--no-dev",
                "--extra",
                "web",
                "--extra",
                "project-hermes",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--python",
                "3.12",
                "--output-file",
                str(export_file),
            ],
            env=env,
        )
        pip_download = [
            args.python,
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--require-hashes",
            "--only-binary=:all:",
            "--implementation",
            "cp",
            "--python-version",
            "3.12",
            "--abi",
            "cp312",
        ]
        for platform in _compatible_pip_platforms(args.target_platform):
            pip_download.extend(["--platform", platform])
        pip_download.extend(
            [
                "--dest",
                str(wheels),
                "--requirement",
                str(export_file),
            ]
        )
        _run(pip_download, env=env)
        distributions = _write_wheel_requirements(wheels)

        worker_root = REPO_ROOT / "deploy" / "release-worker"
        worker_files = _releasable_tree_files(worker_root)
        if not worker_files:
            raise ReleaseError("worker artifact source is empty")
        _create_archive(
            stage / "source.tar.gz",
            root=REPO_ROOT,
            files=source_files,
            prefix="source",
            epoch=epoch,
        )
        _create_archive(
            stage / "web.tar.gz",
            root=web_dist,
            files=_tree_files(web_dist),
            prefix="web",
            epoch=epoch,
        )
        _create_archive(
            stage / "wheelhouse.tar.gz",
            root=wheelhouse,
            files=_tree_files(wheelhouse),
            prefix="wheelhouse",
            epoch=epoch,
        )
        _create_archive(
            stage / "worker.tar.gz",
            root=worker_root,
            files=worker_files,
            prefix="worker",
            epoch=epoch,
        )

        commit, dirty = _git_metadata()
        artifact_records = [
            {
                "name": name,
                "sha256": _sha256(stage / name),
                "size": (stage / name).stat().st_size,
            }
            for name in ARTIFACT_NAMES
        ]
        manifest = {
            "schema_version": RELEASE_SCHEMA,
            "source_date_epoch": epoch,
            "source": {
                "repository": "Github_Hermes",
                "commit": commit,
                "dirty": dirty,
            },
            "runtime_image": args.runtime_image,
            "python_target": {
                "implementation": "cp",
                "version": "3.12",
                "abi": "cp312",
                "platform": args.target_platform,
            },
            "codex": {
                "sdk_version": PINNED_CODEX_VERSION,
                "cli_version": PINNED_CODEX_VERSION,
            },
            "distributions": distributions,
            "artifacts": artifact_records,
        }
        manifest_path = stage / "release-manifest.json"
        _write_json(manifest_path, manifest)
        release_digest = _sha256(manifest_path)
        (stage / "RELEASE_DIGEST").write_text(
            release_digest + "\n",
            encoding="ascii",
        )
        checksum_names = (*ARTIFACT_NAMES, "release-manifest.json")
        (stage / "SHA256SUMS").write_text(
            "".join(f"{_sha256(stage / name)}  {name}\n" for name in checksum_names),
            encoding="ascii",
        )
        export_file.unlink()
        destination = output_root / f"project-hermes-{release_digest}"
        if destination.exists():
            raise ReleaseError(f"release directory already exists: {destination}")
        shutil.move(str(stage), destination)
    _validate_bundle(destination)
    print(f"Release digest: {release_digest}")
    print(f"Release directory: {destination}")
    return destination


def _validate_archive(path: Path, expected_prefix: str) -> None:
    seen: set[str] = set()
    with tarfile.open(path, mode="r:gz") as archive:
        for member in archive:
            relative = _safe_relative_path(member.name)
            if relative.parts[0] != expected_prefix:
                raise ReleaseError(
                    f"{path.name} member is outside {expected_prefix}/: {member.name}"
                )
            if member.name in seen:
                raise ReleaseError(f"duplicate archive member: {member.name}")
            seen.add(member.name)
            if not (member.isfile() or member.isdir() or member.issym()):
                raise ReleaseError(
                    f"unsupported archive member type: {member.name}"
                )
            if member.issym():
                target = PurePosixPath(member.linkname)
                if target.is_absolute():
                    raise ReleaseError(f"absolute archive symlink: {member.name}")
                combined = relative.parent / target
                depth = 0
                for part in combined.parts:
                    depth = depth - 1 if part == ".." else depth + (part != ".")
                    if depth < 1:
                        raise ReleaseError(f"escaping archive symlink: {member.name}")
    if not seen:
        raise ReleaseError(f"archive is empty: {path.name}")


def _parse_checksums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if not match:
            raise ReleaseError(f"malformed checksum line: {line!r}")
        digest, name = match.groups()
        if name in checksums:
            raise ReleaseError(f"duplicate checksum entry: {name}")
        checksums[name] = digest
    return checksums


def _validate_bundle(bundle: Path) -> None:
    required = {
        *ARTIFACT_NAMES,
        "release-manifest.json",
        "RELEASE_DIGEST",
        "SHA256SUMS",
    }
    present = {path.name for path in bundle.iterdir() if path.is_file()}
    if present != required:
        raise ReleaseError(
            f"bundle file set differs: missing={sorted(required - present)}, "
            f"unexpected={sorted(present - required)}"
        )
    release_digest = (bundle / "RELEASE_DIGEST").read_text(
        encoding="ascii"
    ).strip()
    if not SHA256_RE.fullmatch(release_digest):
        raise ReleaseError("RELEASE_DIGEST is not a lowercase SHA-256 digest")
    if _sha256(bundle / "release-manifest.json") != release_digest:
        raise ReleaseError("release manifest digest does not match RELEASE_DIGEST")
    checksums = _parse_checksums(bundle / "SHA256SUMS")
    expected_checksum_names = {*ARTIFACT_NAMES, "release-manifest.json"}
    if set(checksums) != expected_checksum_names:
        raise ReleaseError("SHA256SUMS has an unexpected file set")
    for name, digest in checksums.items():
        if _sha256(bundle / name) != digest:
            raise ReleaseError(f"checksum mismatch: {name}")

    manifest = json.loads(
        (bundle / "release-manifest.json").read_text(encoding="utf-8")
    )
    if manifest.get("schema_version") != RELEASE_SCHEMA:
        raise ReleaseError("unsupported release manifest schema")
    if not IMAGE_DIGEST_RE.fullmatch(str(manifest.get("runtime_image", ""))):
        raise ReleaseError("release manifest runtime image is not immutable")
    codex = manifest.get("codex", {})
    if codex != {
        "sdk_version": PINNED_CODEX_VERSION,
        "cli_version": PINNED_CODEX_VERSION,
    }:
        raise ReleaseError("release manifest does not pin exact Codex versions")
    artifact_map = {
        record.get("name"): record for record in manifest.get("artifacts", [])
    }
    if set(artifact_map) != set(ARTIFACT_NAMES):
        raise ReleaseError("release manifest has an unexpected artifact set")
    for name in ARTIFACT_NAMES:
        record = artifact_map[name]
        if record.get("sha256") != checksums[name]:
            raise ReleaseError(f"manifest checksum mismatch: {name}")
        if record.get("size") != (bundle / name).stat().st_size:
            raise ReleaseError(f"manifest size mismatch: {name}")
    for name, prefix in (
        ("source.tar.gz", "source"),
        ("web.tar.gz", "web"),
        ("wheelhouse.tar.gz", "wheelhouse"),
        ("worker.tar.gz", "worker"),
    ):
        _validate_archive(bundle / name, prefix)
    print(f"Validated release bundle {bundle} ({release_digest})")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser(
        "build",
        help="build deterministic local artifacts",
    )
    build.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT.parent / "project-hermes-releases",
    )
    build.add_argument(
        "--parity-root",
        type=Path,
        default=DEFAULT_PARITY_ROOT,
    )
    build.add_argument("--source-date-epoch", type=int)
    build.add_argument("--python", default=sys.executable)
    build.add_argument(
        "--target-platform",
        default="manylinux_2_28_x86_64",
    )
    build.add_argument("--runtime-image", required=True)
    build.add_argument(
        "--dry-run",
        action="store_true",
        help="validate inputs and print commands without writing or building",
    )
    validate = subparsers.add_parser(
        "validate",
        help="validate an existing release bundle without network access",
    )
    validate.add_argument("bundle", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "build":
            _build_release(args)
        else:
            _validate_bundle(args.bundle.resolve())
    except (OSError, ReleaseError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
