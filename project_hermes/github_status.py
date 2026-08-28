"""Read normalized pull-request states for the ProjectHermes dashboard."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from project_hermes.discovery import GitHubError
from project_hermes.models import utc_now


class GitHubReader(Protocol):
    def get(
        self,
        path_or_url: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]: ...


def _normalized_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    state = (
        "merged"
        if payload.get("merged_at")
        else "draft"
        if payload.get("draft") is True
        else str(payload.get("state") or "unknown").lower()
    )
    if state not in {"draft", "open", "merged", "closed"}:
        state = "unknown"
    number = payload.get("number")
    return {
        "state": state,
        "number": number if isinstance(number, int) and number > 0 else None,
        "url": (
            str(payload["html_url"])
            if isinstance(payload.get("html_url"), str)
            and payload["html_url"]
            else None
        ),
        "checked_at": utc_now().isoformat(),
    }


def _read_pull_request(
    client: GitHubReader,
    reference: Mapping[str, Any],
    repository: str,
) -> Mapping[str, Any] | None:
    if reference.get("number") is not None:
        payload, _headers = client.get(
            f"/repos/{repository}/pulls/{int(reference['number'])}"
        )
        return payload if isinstance(payload, dict) else None

    head_ref = str(reference["head_ref"])
    owner = repository.split("/", 1)[0]
    payload, _headers = client.get(
        f"/repos/{repository}/pulls",
        {
            "state": "all",
            "head": f"{owner}:{head_ref}",
            "sort": "updated",
            "direction": "desc",
            "per_page": 10,
        },
    )
    if not isinstance(payload, list):
        return None
    return next(
        (
            item
            for item in payload
            if isinstance(item, dict)
            and isinstance(item.get("head"), dict)
            and item["head"].get("ref") == head_ref
        ),
        None,
    )


def resolve_pull_request_statuses(
    client: GitHubReader,
    references: Sequence[Mapping[str, Any]],
    *,
    fallback_client: GitHubReader | None = None,
    repository_aliases: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Resolve PR identities, retrying public reads without credentials.

    Some organizations reject classic personal access tokens even for public
    repositories.  The status endpoint can safely retry those read-only REST
    requests anonymously while authenticated publication remains fail-closed.
    """

    aliases = {
        repository.casefold(): github_repository
        for repository, github_repository in (repository_aliases or {}).items()
    }
    results: list[dict[str, Any]] = []
    for reference in references:
        key = str(reference["key"])
        repository = str(reference["repository"])
        github_repository = aliases.get(repository.casefold(), repository)
        try:
            try:
                pull_request = _read_pull_request(
                    client,
                    reference,
                    github_repository,
                )
            except GitHubError:
                if fallback_client is None:
                    raise
                pull_request = _read_pull_request(
                    fallback_client,
                    reference,
                    github_repository,
                )
            status = (
                _normalized_status(pull_request)
                if pull_request is not None
                else {
                    "state": "unknown",
                    "number": None,
                    "url": None,
                    "checked_at": utc_now().isoformat(),
                }
            )
        except (GitHubError, TypeError, ValueError):
            status = {
                "state": "unknown",
                "number": None,
                "url": None,
                "checked_at": utc_now().isoformat(),
            }
        results.append({"key": key, **status})
    return results


__all__ = ["resolve_pull_request_statuses"]
