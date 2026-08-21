from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from project_hermes.api import (
    ActionContext,
    ApiPrincipal,
    build_project_hermes_router,
)
from project_hermes.config import ProjectHermesConfig
from project_hermes.models import ProjectRole
from project_hermes.polling_store import (
    IssueScreening,
    MachineCompatibility,
    PollingCandidate,
    PollingRepository,
    PollingRun,
    PollingRunStatus,
    ScreeningDecision,
    ScreeningEnvironmentRequirements,
    ScreeningTaskKind,
    SqlitePollingStore,
    candidate_snapshot_digest,
)
from project_hermes.web_integration import mount_project_hermes


NOW = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)


def _candidate(store: SqlitePollingStore) -> PollingCandidate:
    store.configure_task(enabled=True, interval_seconds=900, now=NOW)
    run = PollingRun(
        run_id="poll-rescreen-api",
        status=PollingRunStatus.RUNNING,
        cutoff=NOW,
        repositories_requested=1,
        started_at=NOW,
    )
    store.begin_run(run)
    store.upsert_repository(
        PollingRepository(
            repository_id=101,
            repository="acme/alpha",
            default_branch="main",
            html_url="https://github.com/acme/alpha",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    candidate = PollingCandidate(
        candidate_id="poll-candidate-rescreen-api",
        repository_id=101,
        repository="acme/alpha",
        issue_number=17,
        issue_url="https://github.com/acme/alpha/issues/17",
        title="Compatibility namespace needs a verified re-screen",
        body="The CPU tensor reproducer can run through the installed runtime.",
        labels=["module: runtime"],
        author="reporter",
        comments=2,
        evidence_score=5,
        eligible=True,
        filter_reasons=[],
        created_at=NOW,
        updated_at=NOW,
        first_seen_at=NOW,
        last_seen_at=NOW,
        latest_run_id=run.run_id,
    )
    store.save_candidate(candidate, enqueue_eligible=False)
    return candidate


def _initial_screening(candidate: PollingCandidate) -> IssueScreening:
    return IssueScreening(
        candidate_id=candidate.candidate_id,
        candidate_snapshot_digest=candidate_snapshot_digest(candidate),
        decision=ScreeningDecision.DEFER,
        machine_compatibility=MachineCompatibility.NEEDS_PROBE,
        task_kind=ScreeningTaskKind.LOCAL_REPRODUCTION,
        reason="The runtime namespace needs one bounded compatibility probe.",
        required_environment=ScreeningEnvironmentRequirements(
            operating_systems=["linux"],
            cpu_architectures=["amd64"],
            minimum_cpu_cores=1,
            minimum_memory_gib=4,
            gpu_count=0,
            gpu_architectures=[],
            network_access_required=False,
            external_system_write_required=False,
            external_dependencies=["runtime compatibility evidence"],
        ),
        evidence=["The Issue contains a CPU tensor reproducer."],
        uncertainties=["Run the reproducer in the production worker image."],
        model_provider="deepseek",
        model="deepseek-chat",
        session_id="issue-screening-initial",
        profile_digest="a" * 64,
        soul_digest="b" * 64,
        agent_digest="c" * 64,
        screened_at=NOW,
    )


def test_rescreen_api_enforces_rbac_payload_and_returns_history(
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
            "repositories": [{"repository": "acme/alpha"}],
            "require_operator_selection": True,
            "issue_screening_enabled": True,
            "require_screening_select_for_operator": True,
        },
        codex={"enabled": False, "runtime_root": tmp_path / "runtime"},
    )
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    bootstrap = FastAPI()
    services = mount_project_hermes(
        bootstrap,
        require_dashboard_token=lambda _request: None,
        config_path=config_path,
        test_mode=True,
    )
    assert services is not None
    candidate = _candidate(services.polling)
    services.polling.save_screenings([_initial_screening(candidate)])
    calls: list[dict[str, Any]] = []

    def rescreen(
        candidate_id: str,
        requested_by: str,
        reason: str,
        probe_evidence: list[str],
    ) -> None:
        calls.append({
            "candidate_id": candidate_id,
            "requested_by": requested_by,
            "reason": reason,
            "probe_evidence": probe_evidence,
        })
        previous = services.polling.get_current_screening(candidate_id)
        assert previous is not None
        payload = previous.model_dump(mode="python")
        payload.update({
            "decision": ScreeningDecision.SELECT,
            "machine_compatibility": MachineCompatibility.COMPATIBLE,
            "reason": "The bounded production-worker probe proved compatibility.",
            "required_environment": previous.required_environment.model_copy(
                update={"external_dependencies": []}
            ),
            "uncertainties": [],
            "session_id": "issue-screening-rescreen-2",
            "revision": 2,
            "screening_trigger": "operator_rescreen",
            "requested_by": requested_by,
            "request_reason": reason,
            "probe_evidence": probe_evidence,
            "screened_at": NOW + timedelta(minutes=1),
        })
        services.polling.save_screening_revision(IssueScreening.model_validate(payload))

    def authenticate(request: Request) -> ApiPrincipal:
        return ApiPrincipal(
            subject=request.headers.get("X-Test-Subject", "dashboard-operator"),
            role=ProjectRole(
                request.headers.get(
                    "X-Project-Hermes-Role",
                    ProjectRole.OPERATOR.value,
                )
            ),
        )

    app = FastAPI()
    app.include_router(
        build_project_hermes_router(
            services.controller,
            services.runs,
            authenticate=authenticate,
            action_context=lambda _run_id: ActionContext(),
            polling=services.polling,
            issue_rescreener=rescreen,
            operator_selection_required=True,
            screening_selection_required=True,
        ),
        prefix="/api",
    )
    client = TestClient(app)
    route = f"/api/v2/project-hermes/issues/{candidate.candidate_id}/rescreen"

    forbidden = client.post(
        route,
        headers={"X-Project-Hermes-Role": ProjectRole.CODEX.value},
        json={
            "schema_version": "rescreen-issue-request.v1",
            "reason": "A worker probe completed.",
            "probe_evidence": ["The reproducer returned the expected value."],
        },
    )
    invalid = client.post(
        route,
        json={
            "schema_version": "rescreen-issue-request.v1",
            "reason": "A worker probe completed.",
            "probe_evidence": ["  "],
        },
    )
    accepted = client.post(
        route,
        headers={"X-Test-Subject": "operator-session-17"},
        json={
            "schema_version": "rescreen-issue-request.v1",
            "reason": "The production AMD worker resolved the API alias question.",
            "probe_evidence": [
                "The CPU tensor reproducer changed the flag from false to true."
            ],
        },
    )

    assert forbidden.status_code == 403
    assert invalid.status_code == 422
    assert len(calls) == 1
    assert calls[0]["requested_by"] == "operator-session-17"
    assert accepted.status_code == 201
    payload = accepted.json()
    assert payload["screening"]["revision"] == 2
    assert payload["screening"]["decision"] == "SELECT"
    assert payload["operator_selectable"] is True
    assert [item["revision"] for item in payload["screening_history"]] == [
        1,
        2,
    ]
    assert payload["screening_history"][0]["decision"] == "DEFER"
    assert payload["screening_history"][1]["requested_by"] == ("operator-session-17")
    services.close()
