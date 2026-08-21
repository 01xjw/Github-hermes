from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from project_hermes.api import (
    ActionContext,
    ApiPrincipal,
    build_project_hermes_router,
)
from project_hermes.discovery import GitHubError
from project_hermes.github_status import resolve_pull_request_statuses
from project_hermes.models import ProjectRole


class FakeGitHubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def get(
        self,
        path_or_url: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        self.calls.append((path_or_url, params))
        if path_or_url.endswith("/pulls/568"):
            return (
                {
                    "number": 568,
                    "html_url": "https://github.com/ROCm/spur/pull/568",
                    "state": "closed",
                    "draft": False,
                    "merged_at": "2026-08-20T03:00:00Z",
                },
                {},
            )
        if path_or_url.endswith("/pulls/999"):
            raise GitHubError("rate limited")
        if path_or_url.endswith("/pulls"):
            return (
                [
                    {
                        "number": 16,
                        "html_url": "https://github.com/acme/kernel/pull/16",
                        "state": "open",
                        "draft": False,
                        "merged_at": None,
                        "head": {"ref": "another-branch"},
                    },
                    {
                        "number": 17,
                        "html_url": "https://github.com/acme/kernel/pull/17",
                        "state": "open",
                        "draft": True,
                        "merged_at": None,
                        "head": {"ref": "project-hermes/issue-17"},
                    },
                ],
                {},
            )
        raise AssertionError(path_or_url)


def test_resolves_pull_requests_by_number_and_head_ref() -> None:
    client = FakeGitHubClient()

    statuses = resolve_pull_request_statuses(
        client,
        [
            {
                "key": "spur-568",
                "repository": "ROCm/spur",
                "number": 568,
                "head_ref": None,
            },
            {
                "key": "candidate-17",
                "repository": "acme/kernel",
                "number": None,
                "head_ref": "project-hermes/issue-17",
            },
        ],
    )

    assert statuses[0] == {
        "key": "spur-568",
        "state": "merged",
        "number": 568,
        "url": "https://github.com/ROCm/spur/pull/568",
        "checked_at": statuses[0]["checked_at"],
    }
    assert statuses[1] == {
        "key": "candidate-17",
        "state": "draft",
        "number": 17,
        "url": "https://github.com/acme/kernel/pull/17",
        "checked_at": statuses[1]["checked_at"],
    }
    assert client.calls[1] == (
        "/repos/acme/kernel/pulls",
        {
            "state": "all",
            "head": "acme:project-hermes/issue-17",
            "sort": "updated",
            "direction": "desc",
            "per_page": 10,
        },
    )


def test_one_failed_reference_does_not_block_the_batch() -> None:
    statuses = resolve_pull_request_statuses(
        FakeGitHubClient(),
        [
            {
                "key": "unavailable",
                "repository": "acme/kernel",
                "number": 999,
                "head_ref": None,
            },
            {
                "key": "spur-568",
                "repository": "ROCm/spur",
                "number": 568,
                "head_ref": None,
            },
        ],
    )

    assert statuses[0]["state"] == "unknown"
    assert statuses[0]["number"] is None
    assert statuses[1]["state"] == "merged"


def test_authenticated_failure_retries_read_only_status_anonymously() -> None:
    class BlockedAuthenticatedClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any] | None]] = []

        def get(
            self,
            path_or_url: str,
            params: dict[str, Any] | None = None,
        ) -> tuple[Any, dict[str, str]]:
            self.calls.append((path_or_url, params))
            raise GitHubError("organization rejects classic personal tokens")

    primary = BlockedAuthenticatedClient()
    fallback = FakeGitHubClient()

    statuses = resolve_pull_request_statuses(
        primary,
        [
            {
                "key": "spur-568",
                "repository": "ROCm/spur",
                "number": 568,
                "head_ref": None,
            }
        ],
        fallback_client=fallback,
    )

    assert statuses[0]["state"] == "merged"
    assert primary.calls == [("/repos/ROCm/spur/pulls/568", None)]
    assert fallback.calls == [("/repos/ROCm/spur/pulls/568", None)]


def test_authenticated_batch_api_validates_and_resolves_references() -> None:
    captured: list[list[dict[str, Any]]] = []

    def resolve(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
        captured.append(references)
        return [
            {
                "key": references[0]["key"],
                "state": "merged",
                "number": references[0]["number"],
                "url": "https://github.com/ROCm/spur/pull/568",
                "checked_at": "2026-08-21T00:00:00+00:00",
            }
        ]

    app = FastAPI()
    app.include_router(
        build_project_hermes_router(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            authenticate=lambda _request: ApiPrincipal(
                subject="dashboard-operator",
                role=ProjectRole.OPERATOR,
            ),
            action_context=lambda _run_id: ActionContext(),
            pull_request_status_resolver=resolve,
        ),
        prefix="/api",
    )
    client = TestClient(app)
    path = "/api/v2/project-hermes/github/pull-request-statuses"

    response = client.post(
        path,
        json={
            "schema_version": "pull-request-status-request.v1",
            "references": [
                {"key": "spur-568", "repository": "ROCm/spur", "number": 568}
            ],
        },
    )
    missing_identity = client.post(
        path,
        json={
            "schema_version": "pull-request-status-request.v1",
            "references": [{"key": "missing", "repository": "acme/kernel"}],
        },
    )
    duplicate_keys = client.post(
        path,
        json={
            "schema_version": "pull-request-status-request.v1",
            "references": [
                {"key": "same", "repository": "acme/kernel", "number": 1},
                {"key": "same", "repository": "acme/kernel", "number": 2},
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["statuses"][0]["state"] == "merged"
    assert captured == [
        [
            {
                "key": "spur-568",
                "repository": "ROCm/spur",
                "number": 568,
                "head_ref": None,
            }
        ]
    ]
    assert missing_identity.status_code == 422
    assert duplicate_keys.status_code == 422
