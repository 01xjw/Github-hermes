from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _packager() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts/package_project_hermes_release.py"
    )
    spec = importlib.util.spec_from_file_location(
        "project_hermes_release_packager_test",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_tree_excludes_python_runtime_caches(tmp_path: Path) -> None:
    (tmp_path / "run.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (tmp_path / "execute.py").write_text("VALUE = 1\n", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "execute.cpython-312.pyc").write_bytes(b"nondeterministic")
    pytest_cache = tmp_path / ".pytest_cache"
    pytest_cache.mkdir()
    (pytest_cache / "state").write_text("generated\n", encoding="utf-8")
    (tmp_path / "legacy.pyo").write_bytes(b"generated")

    files = _packager()._releasable_tree_files(tmp_path)

    assert [path.as_posix() for path in files] == ["execute.py", "run.sh"]


def test_manylinux_download_accepts_compatible_older_wheels() -> None:
    platforms = _packager()._compatible_pip_platforms(
        "manylinux_2_28_x86_64"
    )

    assert platforms[0] == "manylinux_2_28_x86_64"
    assert "manylinux_2_17_x86_64" in platforms
    assert "manylinux2014_x86_64" in platforms
    assert "manylinux_2_29_x86_64" not in platforms
