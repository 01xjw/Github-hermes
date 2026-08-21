from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from project_hermes.config import PollingConfig, ProjectHermesConfig
from project_hermes.polling import IssuePollingService
from project_hermes.polling_store import (
    OPERATOR_SELECTION_HOLD_REASON,
    PollingRunStatus,
    RepositoryScanStatus,
    SqlitePollingStore,
    WorkPlan,
    WorkResourceRequirements,
    WorkStatus,
)
from project_hermes.web_integration import mount_project_hermes


NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)


class _GitHubClient:
    def __init__(self) -> None:
        self.truncated = False
        self.scanned: list[tuple[str, datetime, int | None]] = []
        self.repository_ids = {
            "acme/alpha": 101,
            "acme/beta": 102,
        }

    def get(
        self,
        path_or_url: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        assert params is None
        repository = path_or_url.removeprefix("/repos/")
        repository_id = self.repository_ids[repository.casefold()]
        canonical = next(
            name
            for name in self.repository_ids
            if name.casefold() == repository.casefold()
        )
        return (
            {
                "id": repository_id,
                "full_name": canonical,
                "html_url": f"https://github.com/{canonical}",
                "default_branch": "main",
            },
            {},
        )

    def recent_open_issues(
        self,
        repository: str,
        cutoff: datetime,
        *,
        max_pages: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        self.truncated = False
        self.scanned.append((repository, cutoff, max_pages))
        if repository.casefold() == "acme/beta":
            return
        yield _issue(1, labels=["bug"])
        yield _issue(2, labels=["question"])


def _issue(number: int, *, labels: list[str]) -> dict[str, Any]:
    return {
        "number": number,
        "state": "open",
        "html_url": f"https://github.com/acme/alpha/issues/{number}",
        "title": f"Issue {number}",
        "body": "A reproducible failure with concrete expected behavior.",
        "labels": [{"name": label} for label in labels],
        "user": {"login": "reporter"},
        "comments": 2,
        "locked": False,
        "created_at": "2026-08-14T03:00:00Z",
        "updated_at": "2026-08-14T04:00:00Z",
    }


def _polling_config(path: Path) -> PollingConfig:
    return PollingConfig(
        sqlite_path=path,
        rolling_window_days=1,
        repositories_per_run=1,
        repositories=(
            {"repository": "acme/alpha"},
            {"repository": "acme/beta"},
        ),
        minimum_evidence_score=0,
        max_issue_pages_per_repository=1,
        max_candidates_per_repository=30,
        max_candidates_per_run=30,
    )


def _service(
    path: Path,
) -> tuple[IssuePollingService, SqlitePollingStore, _GitHubClient]:
    config = _polling_config(path)
    store = SqlitePollingStore(path)
    store.configure_task(enabled=True, interval_seconds=config.interval_seconds, now=NOW)
    client = _GitHubClient()
    return IssuePollingService(client, store, config), store, client


def test_polling_scans_one_repository_one_day_and_rotates_durably(
    tmp_path: Path,
) -> None:
    service, store, client = _service(tmp_path / "polling.db")

    first = service.run_now(now=NOW)
    second = service.run_now(now=NOW + timedelta(minutes=15))

    assert first.repositories_requested == 1
    assert first.repositories_scanned == 1
    assert first.issues_seen == 2
    assert first.candidates_matched == 1
    assert first.work_items_queued == 1
    assert second.repositories_requested == 1
    assert [item[0] for item in client.scanned] == ["acme/alpha", "acme/beta"]
    assert client.scanned[0][1] == NOW - timedelta(days=1)
    assert client.scanned[0][2] == 1
    assert store.count_candidates() == 2
    assert store.count_candidates(eligible=True) == 1
    assert store.work_counts()["queued"] == 1
    assert store.repository_last_attempts()["acme/alpha"] < (
        store.repository_last_attempts()["acme/beta"]
    )


def test_due_polling_continues_immediately_then_waits_per_repository(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr("project_hermes.polling.utc_now", lambda: NOW)
    service, store, client = _service(tmp_path / "polling.db")

    first = service.run_if_due(now=NOW)
    second = service.run_if_due(now=NOW)

    assert first is not None
    assert second is not None
    assert [item[0] for item in client.scanned] == ["acme/alpha", "acme/beta"]
    assert store.get_task().next_run_at == NOW + timedelta(minutes=15)
    assert service.run_if_due(now=NOW + timedelta(minutes=14)) is None


def test_guided_mode_scans_every_repository_without_auto_enqueuing(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr("project_hermes.polling.utc_now", lambda: NOW)
    config = _polling_config(tmp_path / "polling.db").model_copy(
        update={
            "require_operator_selection": True,
            "max_pending_work_items": 1,
        }
    )
    store = SqlitePollingStore(config.sqlite_path)
    store.configure_task(
        enabled=True,
        interval_seconds=config.interval_seconds,
        now=NOW,
    )
    client = _GitHubClient()
    service = IssuePollingService(client, store, config)

    assert service.run_if_due(now=NOW) is not None
    assert service.run_if_due(now=NOW) is not None

    assert [item[0] for item in client.scanned] == ["acme/alpha", "acme/beta"]
    assert store.count_candidates(eligible=True) == 1
    assert store.count_work_items() == 0
    assert service.run_if_due(now=NOW + timedelta(minutes=14)) is None


def test_guided_mode_holds_legacy_work_and_operator_selection_resumes_it(
    tmp_path: Path,
) -> None:
    config = _polling_config(tmp_path / "polling.db")
    store = SqlitePollingStore(config.sqlite_path)
    store.configure_task(
        enabled=True,
        interval_seconds=config.interval_seconds,
        now=NOW,
    )
    client = _GitHubClient()
    IssuePollingService(client, store, config).run_now(now=NOW)
    candidate = store.list_candidates(eligible=True)[0]
    legacy = store.get_work_item_for_candidate(candidate.candidate_id)
    assert legacy is not None
    assert legacy.status is WorkStatus.QUEUED

    guided = config.model_copy(update={"require_operator_selection": True})
    service = IssuePollingService(client, store, guided)
    held = store.get_work_item_for_candidate(candidate.candidate_id)
    assert held is not None
    assert held.status is WorkStatus.BLOCKED
    assert held.blocked_reason == OPERATOR_SELECTION_HOLD_REASON

    selected = service.select_candidate(
        candidate.candidate_id,
        selected_by="dashboard-operator",
        now=NOW + timedelta(minutes=1),
    )

    assert selected.status is WorkStatus.QUEUED
    assert selected.blocked_reason is None
    assert store.operator_selection_state(candidate.candidate_id) == {
        "selected": True,
        "selectable": False,
    }
    assert "work.operator_selected" in {
        event.event_type
        for event in store.list_work_events(
            work_item_id_value=selected.work_item_id,
            limit=100,
        )
    }
    repeated = service.select_candidate(
        candidate.candidate_id,
        selected_by="dashboard-operator",
        now=NOW + timedelta(minutes=2),
    )
    assert repeated == selected
    assert [
        event.event_type
        for event in store.list_work_events(
            work_item_id_value=selected.work_item_id,
            limit=100,
        )
    ].count("work.operator_selected") == 1
    IssuePollingService(client, store, guided)
    assert store.get_work_item(selected.work_item_id).status is WorkStatus.QUEUED


def test_bounded_page_limit_is_partial_and_rotates_to_next_repository(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr("project_hermes.polling.utc_now", lambda: NOW)

    class _BoundedClient(_GitHubClient):
        def recent_open_issues(
            self,
            repository: str,
            cutoff: datetime,
            *,
            max_pages: int | None = None,
        ) -> Iterator[dict[str, Any]]:
            self.truncated = False
            self.scanned.append((repository, cutoff, max_pages))
            if repository.casefold() == "acme/beta":
                return
            yield _issue(1, labels=["bug"])
            self.truncated = True

    config = _polling_config(tmp_path / "polling.db")
    store = SqlitePollingStore(config.sqlite_path)
    store.configure_task(
        enabled=True,
        interval_seconds=config.interval_seconds,
        now=NOW,
    )
    client = _BoundedClient()
    service = IssuePollingService(client, store, config)

    bounded = service.run_if_due(now=NOW)
    following = service.run_if_due(now=NOW)

    assert bounded is not None
    assert bounded.status is PollingRunStatus.PARTIAL
    assert bounded.repositories_scanned == 1
    assert bounded.repositories_failed == 0
    assert "continuing repository rotation" in (bounded.error or "")
    alpha = store.list_repositories(repository_id=101)[0]
    assert alpha.last_status is RepositoryScanStatus.PARTIAL
    assert following is not None
    assert following.status is PollingRunStatus.COMPLETED
    assert [item[0] for item in client.scanned] == [
        "acme/alpha",
        "acme/beta",
    ]


def test_due_polling_pauses_at_pending_capacity_and_resumes(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr("project_hermes.polling.utc_now", lambda: NOW)
    config = _polling_config(tmp_path / "polling.db").model_copy(
        update={"max_pending_work_items": 1}
    )
    store = SqlitePollingStore(config.sqlite_path)
    store.configure_task(
        enabled=True,
        interval_seconds=config.interval_seconds,
        now=NOW,
    )
    client = _GitHubClient()
    service = IssuePollingService(client, store, config)

    assert service.run_if_due(now=NOW) is not None
    assert store.pending_work_count() == 1
    assert service.run_if_due(now=NOW) is None
    assert [item[0] for item in client.scanned] == ["acme/alpha"]

    work = store.list_work_items()[0]
    store.transition_work_item(
        work.work_item_id,
        WorkStatus.BLOCKED,
        current_step="Not suitable for this cluster.",
        reason="Backpressure test completed.",
        now=NOW,
    )

    assert service.run_if_due(now=NOW) is not None
    assert [item[0] for item in client.scanned] == ["acme/alpha", "acme/beta"]


def test_overview_filters_by_numeric_repository_and_counts_processed_work(
    tmp_path: Path,
) -> None:
    service, store, _client = _service(tmp_path / "polling.db")
    service.run_now(now=NOW)
    work = store.list_work_items()[0]
    store.transition_work_item(
        work.work_item_id,
        WorkStatus.BLOCKED,
        current_step="Not actionable after project-manager review.",
        reason="The reproducer does not cover the supported platform.",
        now=NOW + timedelta(hours=1),
    )

    overview = store.overview(repository_id=101, now=NOW + timedelta(hours=2))

    assert overview["issues_seen"] == 2
    assert overview["candidate_counts"] == {
        "total": 2,
        "matched": 1,
        "filtered": 1,
    }
    assert overview["processed"] == {"today": 1, "this_week": 1}
    assert overview["work_counts"]["blocked"] == 1
    assert overview["repository_stats"][0]["repository_id"] == 101


def test_issue_api_pairs_work_and_reveals_resources_only_after_verification(
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
            **_polling_config(tmp_path / "polling.db").model_dump(),
            "repositories": [{"repository": "acme/alpha"}],
        },
        codex={"enabled": False, "runtime_root": tmp_path / "runtime"},
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
    polling = IssuePollingService(
        _GitHubClient(),
        services.polling,
        services.config.polling,
    )
    polling.run_now(now=NOW)
    client = TestClient(app)

    listed = client.get(
        "/api/v2/project-hermes/issues",
        params={"repository_id": 101, "eligible": True},
    )
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] == 1
    assert payload["issues"][0]["work_item"]["resource_requirements"] is None

    work_item_id = payload["issues"][0]["work_item"]["work_item_id"]
    services.polling.verify_work_environment(
        work_item_id,
        named_baseline="main@" + "a" * 40,
        resources=WorkResourceRequirements(
            cpu_request="2",
            cpu_limit="4",
            memory_request="4Gi",
            memory_limit="8Gi",
            gpu_count=1,
            gpu_architecture="gfx942",
            worker_model="worker-model",
        ),
        now=NOW + timedelta(minutes=1),
    )
    detail = client.get(
        f"/api/v2/project-hermes/work-items/{work_item_id}"
    )

    assert detail.status_code == 200
    assert detail.json()["work_item"]["environment_status"] == "verified"
    assert (
        detail.json()["work_item"]["resource_requirements"]["worker_model"]
        == "worker-model"
    )


def test_guided_issue_api_requires_operator_selection_before_work(
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
            **_polling_config(tmp_path / "polling.db").model_dump(),
            "repositories": [{"repository": "acme/alpha"}],
            "require_operator_selection": True,
        },
        codex={"enabled": False, "runtime_root": tmp_path / "runtime"},
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
    polling = IssuePollingService(
        _GitHubClient(),
        services.polling,
        services.config.polling,
    )
    polling.run_now(now=NOW)
    candidate = services.polling.list_candidates(eligible=True)[0]
    client = TestClient(app)

    overview = client.get("/api/v2/project-hermes/polling")
    before = client.get(
        f"/api/v2/project-hermes/issues/{candidate.candidate_id}"
    )
    selected = client.post(
        f"/api/v2/project-hermes/issues/{candidate.candidate_id}/select"
    )

    assert overview.status_code == 200
    assert overview.json()["operator_selection_required"] is True
    assert before.status_code == 200
    assert before.json()["work_item"] is None
    assert before.json()["operator_selectable"] is True
    assert before.json()["operator_selected"] is False
    assert selected.status_code == 201
    assert selected.json()["work_item"]["status"] == "queued"
    assert selected.json()["operator_selected"] is True
    assert selected.json()["operator_selectable"] is False


def test_operator_retry_api_reopens_verified_policy_false_positive(
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
            **_polling_config(tmp_path / "polling.db").model_dump(),
            "repositories": [{"repository": "acme/alpha"}],
            "require_operator_selection": True,
        },
        codex={"enabled": False, "runtime_root": tmp_path / "runtime"},
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
    polling = IssuePollingService(
        _GitHubClient(),
        services.polling,
        services.config.polling,
    )
    polling.run_now(now=NOW)
    candidate = services.polling.list_candidates(eligible=True)[0]
    client = TestClient(app)
    selected = client.post(
        f"/api/v2/project-hermes/issues/{candidate.candidate_id}/select"
    )
    work_item_id = selected.json()["work_item"]["work_item_id"]
    services.polling.verify_work_environment(
        work_item_id,
        named_baseline="main@" + "a" * 40,
        resources=WorkResourceRequirements(
            cpu_request="1",
            cpu_limit="4",
            memory_request="4Gi",
            memory_limit="32Gi",
            gpu_count=1,
            gpu_architecture="gfx1100",
            worker_model="deepseek-v4-pro",
        ),
        now=NOW + timedelta(seconds=1),
    )
    services.polling.plan_work_item(
        work_item_id,
        WorkPlan(
            summary="Apply the local correction.",
            steps=["Do not push or comment on the GitHub issue."],
            acceptance_criteria=["The focused local test passes."],
        ),
        now=NOW + timedelta(seconds=2),
    )
    blocked_reason = (
        "Committed plan requires an external GitHub action, but workers may "
        "only produce and verify local repository changes."
    )
    services.polling.transition_work_item(
        work_item_id,
        WorkStatus.BLOCKED,
        current_step="Main Hermes blocked this Work item.",
        reason=blocked_reason,
        now=NOW + timedelta(seconds=3),
    )

    retried = client.post(
        f"/api/v2/project-hermes/work-items/{work_item_id}/retry",
        json={
            "schema_version": "retry-work-item-request.v1",
            "reason": "The frozen plan prohibits rather than requests GitHub action.",
        },
    )

    assert retried.status_code == 200
    assert retried.json()["work_item"]["status"] == "planning"
    detail = client.get(
        f"/api/v2/project-hermes/work-items/{work_item_id}"
    ).json()
    event = next(
        item
        for item in detail["events"]
        if item["event_type"] == "work.policy_block_retry_requested"
    )
    assert event["payload"]["requested_by"] == "dashboard-operator"


def test_operator_retry_api_reopens_controller_failed_execution(
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
            **_polling_config(tmp_path / "polling.db").model_dump(),
            "repositories": [{"repository": "acme/alpha"}],
            "require_operator_selection": True,
        },
        codex={"enabled": False, "runtime_root": tmp_path / "runtime"},
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
    polling = IssuePollingService(
        _GitHubClient(),
        services.polling,
        services.config.polling,
    )
    polling.run_now(now=NOW)
    candidate = services.polling.list_candidates(eligible=True)[0]
    client = TestClient(app)
    selected = client.post(
        f"/api/v2/project-hermes/issues/{candidate.candidate_id}/select"
    )
    work_item_id = selected.json()["work_item"]["work_item_id"]
    services.polling.verify_work_environment(
        work_item_id,
        named_baseline="main@" + "a" * 40,
        resources=WorkResourceRequirements(
            cpu_request="1",
            cpu_limit="4",
            memory_request="4Gi",
            memory_limit="32Gi",
            gpu_count=1,
            gpu_architecture="gfx1100",
            worker_model="deepseek-v4-pro",
        ),
        now=NOW + timedelta(seconds=1),
    )
    services.polling.plan_work_item(
        work_item_id,
        WorkPlan(
            summary="Apply the local correction.",
            steps=["Implement and verify the focused fix."],
            acceptance_criteria=["The focused local test passes."],
        ),
        now=NOW + timedelta(seconds=2),
    )
    services.polling.start_work_item(
        work_item_id,
        task_id="task-retry-1",
        run_id="run-retry-1",
        execution_id="execution-retry-1",
        now=NOW + timedelta(seconds=3),
    )
    services.polling.transition_work_item(
        work_item_id,
        WorkStatus.FAILED,
        current_step="Controller run ended FAILED.",
        error="Controller run ended FAILED.",
        now=NOW + timedelta(seconds=4),
    )

    retried = client.post(
        f"/api/v2/project-hermes/work-items/{work_item_id}/retry",
        json={
            "schema_version": "retry-work-item-request.v1",
            "reason": "Verified the worker was evicted by its temporary-volume limit.",
        },
    )

    assert retried.status_code == 200
    assert retried.json()["work_item"]["status"] == "planning"
    assert retried.json()["work_item"]["execution_attempt"] == 1
    detail = client.get(
        f"/api/v2/project-hermes/work-items/{work_item_id}"
    ).json()
    event = next(
        item
        for item in detail["events"]
        if item["event_type"] == "work.failed_execution_retry_requested"
    )
    assert event["payload"]["execution_id"] == "execution-retry-1"
    assert event["payload"]["requested_by"] == "dashboard-operator"

    services.polling.start_work_item(
        work_item_id,
        task_id="task-retry-2",
        run_id="run-retry-2",
        execution_id="execution-retry-2",
        now=NOW + timedelta(seconds=5),
    )
    services.polling.transition_work_item(
        work_item_id,
        WorkStatus.REVIEW,
        current_step="Internal candidate is awaiting review.",
        internal_candidate_id="internal-review-after-provider-failure",
        now=NOW + timedelta(seconds=6),
    )
    services.polling.transition_work_item(
        work_item_id,
        WorkStatus.BLOCKED,
        current_step="Independent review revision budget was exhausted.",
        reason=(
            "Legacy total-execution accounting included a provider failure."
        ),
        now=NOW + timedelta(seconds=7),
    )

    review_retried = client.post(
        f"/api/v2/project-hermes/work-items/{work_item_id}/retry",
        json={
            "schema_version": "retry-work-item-request.v1",
            "reason": (
                "Exclude the provider failure and carry the first candidate's "
                "review feedback into a fresh Worker execution."
            ),
        },
    )

    assert review_retried.status_code == 200
    assert review_retried.json()["work_item"]["status"] == "planning"
    assert review_retried.json()["work_item"]["execution_attempt"] == 2
    detail = client.get(
        f"/api/v2/project-hermes/work-items/{work_item_id}"
    ).json()
    event = next(
        item
        for item in detail["events"]
        if item["event_type"] == "work.review_budget_retry_requested"
    )
    assert event["payload"]["reviewed_candidate_attempts"] == 1
    assert event["payload"]["total_execution_attempts"] == 2
    assert event["payload"]["max_review_attempts"] == 3
    assert event["payload"]["requested_by"] == "dashboard-operator"
