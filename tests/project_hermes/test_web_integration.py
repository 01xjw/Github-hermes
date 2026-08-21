from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from project_hermes.config import ProjectHermesConfig
from project_hermes.models import IssueTask
from project_hermes.web_integration import mount_project_hermes


def test_mounts_authenticated_router_and_accounting_endpoint(
    tmp_path: Path,
    issue_task: IssueTask,
) -> None:
    config_path = tmp_path / "project-hermes.yaml"
    config = ProjectHermesConfig(
        project_root=tmp_path,
        control_plane={
            "mode": "sqlite",
            "sqlite_path": tmp_path / "control-plane.db",
        },
        codex={
            "enabled": False,
            "runtime_root": tmp_path / "runtime",
        },
    )
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    app = FastAPI()
    services = mount_project_hermes(
        app,
        require_dashboard_token=lambda _request: None,
        config_path=config_path,
        test_mode=True,
    )
    assert services is not None

    client = TestClient(app)
    created = client.post(
        "/api/v2/project-hermes/runs",
        json={"task": issue_task.model_dump(mode="json"), "run_id": "run-1"},
    )
    accounting = client.get(
        "/api/v2/project-hermes/tasks/task-1/accounting"
    )

    assert created.status_code == 201
    assert accounting.status_code == 200
    payload = accounting.json()
    assert payload["summary"]["run_id"] == "run-1"
    assert payload["summary"]["rounds"] == []
    assert payload["summary"]["estimated_llm_cost_usd"] == 0


def test_polling_routes_replace_removed_campaign_surface(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "project-hermes.yaml"
    config = ProjectHermesConfig(
        project_root=tmp_path,
        control_plane={
            "mode": "sqlite",
            "sqlite_path": tmp_path / "control-plane.db",
        },
        polling={
            "sqlite_path": tmp_path / "polling.db",
            "repositories": [{"repository": "ROCm/ROCm"}],
        },
        codex={
            "enabled": False,
            "runtime_root": tmp_path / "runtime",
        },
    )
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    app = FastAPI()
    services = mount_project_hermes(
        app,
        require_dashboard_token=lambda _request: None,
        config_path=config_path,
        test_mode=True,
    )
    assert services is not None
    client = TestClient(app)

    overview = client.get("/api/v2/project-hermes/polling")
    issues = client.get("/api/v2/project-hermes/issues")
    removed = client.get("/api/v2/project-hermes/campaigns")

    assert overview.status_code == 200
    assert overview.json()["task"]["task_id"] == "github-issue-polling"
    assert issues.status_code == 200
    assert issues.json()["issues"] == []
    assert removed.status_code == 404
