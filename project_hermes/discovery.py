"""Shared GitHub API primitives used by ProjectHermes polling."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Iterator, Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator

from project_hermes.models import StrictModel

GITHUB_API_ROOT = "https://api.github.com"
GITHUB_ACCEPT = "application/vnd.github+json"

_LINKED_PULL_REQUESTS_QUERY = """
query LinkedPullRequests(
  $owner: String!, $name: String!, $number: Int!, $cursor: String
) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      timelineItems(
        first: 100,
        after: $cursor,
        itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT]
      ) {
        pageInfo { hasNextPage endCursor }
        nodes {
          __typename
          ... on CrossReferencedEvent {
            source {
              __typename
              ... on PullRequest {
                number
                url
                repository { nameWithOwner }
              }
            }
          }
          ... on ConnectedEvent {
            subject {
              __typename
              ... on PullRequest {
                number
                url
                repository { nameWithOwner }
              }
            }
          }
        }
      }
    }
  }
}
"""


class GitHubError(RuntimeError):
    """A bounded GitHub API request or response failure."""


@dataclass(slots=True)
class GitHubApiStatus:
    authenticated: bool
    remaining: int | None = None
    limit: int | None = None
    reset_at: int | None = None
    requests: int = 0


class LinkedPullRequestIdentity(StrictModel):
    repository: str
    number: int = Field(ge=1)
    url: str
    relationship: Literal["connected", "cross_referenced"]

    @model_validator(mode="after")
    def validate_identity(self) -> "LinkedPullRequestIdentity":
        parsed = urlparse(self.url)
        expected_path = f"/{self.repository}/pull/{self.number}"
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != expected_path
        ):
            raise ValueError(
                "linked pull request URL must match its GitHub identity"
            )
        return self


class GitHubClient:
    """Small retrying GitHub client with explicit network entry points."""

    def __init__(
        self,
        token: str | None = None,
        *,
        max_retries: int = 3,
        timeout_seconds: int = 60,
        api_root: str = GITHUB_API_ROOT,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.token = (
            discover_github_token() if token is None else token.strip() or None
        )
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self.api_root = api_root.rstrip("/")
        self._opener = opener
        self._sleep = sleeper
        self._clock = clock
        self.status = GitHubApiStatus(authenticated=bool(self.token))
        self.truncated = False

    def get(
        self,
        path_or_url: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        return self._request_json(path_or_url, params=params)

    def paginate(
        self,
        path: str,
        params: dict[str, Any],
        *,
        stop_before: datetime | None = None,
        date_field: str = "updated_at",
        max_pages: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        self.truncated = False
        page = 1
        while True:
            query = {
                **params,
                "per_page": params.get("per_page", 100),
                "page": page,
            }
            payload, headers = self.get(path, query)
            if not isinstance(payload, list):
                raise GitHubError(
                    f"expected a list response from {path}, "
                    f"received {type(payload).__name__}"
                )
            if not payload:
                return
            reached_cutoff = False
            for item in payload:
                if not isinstance(item, dict):
                    raise GitHubError(f"received a non-object item from {path}")
                value = item.get(date_field)
                if (
                    stop_before is not None
                    and value
                    and parse_github_time(str(value)) < stop_before
                ):
                    reached_cutoff = True
                    continue
                yield item
            has_next = 'rel="next"' in headers.get("link", "")
            if (
                reached_cutoff
                or len(payload) < int(query["per_page"])
                or not has_next
            ):
                return
            if max_pages is not None and page >= max_pages:
                self.truncated = True
                return
            page += 1

    def recent_open_issues(
        self,
        repository: str,
        cutoff: datetime,
        *,
        max_pages: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        for item in self.paginate(
            f"/repos/{repository}/issues",
            {"state": "open", "sort": "created", "direction": "desc"},
            stop_before=cutoff,
            date_field="created_at",
            max_pages=max_pages,
        ):
            if "pull_request" in item or item.get("state") != "open":
                continue
            created_at = item.get("created_at")
            if not created_at or parse_github_time(str(created_at)) < cutoff:
                continue
            yield item

    def linked_pull_requests(
        self,
        repository: str,
        issue_number: int,
        *,
        max_pages: int | None = None,
    ) -> list[LinkedPullRequestIdentity]:
        owner, name = _split_repository(repository)
        cursor: str | None = None
        page = 0
        identities: dict[tuple[str, int], LinkedPullRequestIdentity] = {}
        while True:
            page += 1
            payload = self._graphql(
                _LINKED_PULL_REQUESTS_QUERY,
                {
                    "owner": owner,
                    "name": name,
                    "number": issue_number,
                    "cursor": cursor,
                },
            )
            try:
                timeline = payload["data"]["repository"]["issue"][
                    "timelineItems"
                ]
                nodes = timeline["nodes"] or []
                page_info = timeline["pageInfo"]
            except (KeyError, TypeError) as exc:
                raise GitHubError(
                    "GitHub returned an invalid issue timeline response"
                ) from exc
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                node_type = node.get("__typename")
                if node_type == "CrossReferencedEvent":
                    pull_request = node.get("source")
                    relationship = "cross_referenced"
                elif node_type == "ConnectedEvent":
                    pull_request = node.get("subject")
                    relationship = "connected"
                else:
                    continue
                if (
                    not isinstance(pull_request, dict)
                    or pull_request.get("__typename") != "PullRequest"
                ):
                    continue
                pull_repository = (pull_request.get("repository") or {}).get(
                    "nameWithOwner"
                )
                if not pull_repository:
                    continue
                identity = LinkedPullRequestIdentity(
                    repository=str(pull_repository),
                    number=int(pull_request["number"]),
                    url=str(pull_request["url"]),
                    relationship=relationship,
                )
                key = (identity.repository.casefold(), identity.number)
                current = identities.get(key)
                if current is None or identity.relationship < current.relationship:
                    identities[key] = identity
            if not page_info.get("hasNextPage"):
                break
            if max_pages is not None and page >= max_pages:
                self.truncated = True
                raise GitHubError(
                    "linked pull request timeline reached its pagination limit"
                )
            cursor = page_info.get("endCursor")
            if not cursor:
                raise GitHubError(
                    "GitHub timeline pagination omitted its next cursor"
                )
        return sorted(
            identities.values(),
            key=lambda item: (
                item.repository.casefold(),
                item.number,
                item.relationship,
            ),
        )

    def _graphql(
        self,
        query: str,
        variables: dict[str, Any],
    ) -> dict[str, Any]:
        payload, _ = self._request_json(
            "/graphql",
            method="POST",
            payload={"query": query, "variables": variables},
        )
        if not isinstance(payload, dict):
            raise GitHubError("GitHub GraphQL returned a non-object response")
        errors = payload.get("errors")
        if errors:
            messages = [
                str(item.get("message") or "unknown GraphQL error")
                for item in errors
                if isinstance(item, dict)
            ]
            raise GitHubError(
                "GitHub GraphQL request failed: "
                + "; ".join(messages or ["unknown error"])
            )
        return payload

    def _request_json(
        self,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        url = self._request_url(path_or_url, params)
        headers = {
            "Accept": GITHUB_ACCEPT,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "project-hermes-issue-polling/1",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if payload is not None:
            data = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"

        for attempt in range(self.max_retries + 1):
            try:
                request = urllib.request.Request(
                    url,
                    data=data,
                    headers=headers,
                    method=method,
                )
                with self._opener(
                    request,
                    timeout=self.timeout_seconds,
                ) as response:
                    response_headers = {
                        key.casefold(): value
                        for key, value in response.headers.items()
                    }
                    response_payload = json.loads(
                        response.read().decode("utf-8")
                    )
                    self._update_status(response_headers)
                    self.status.requests += 1
                    return response_payload, response_headers
            except urllib.error.HTTPError as error:
                response_headers = {
                    key.casefold(): value
                    for key, value in error.headers.items()
                }
                self._update_status(response_headers)
                body = error.read().decode("utf-8", errors="replace")
                if error.code in {403, 429} and attempt < self.max_retries:
                    self._wait_for_limit(response_headers, attempt)
                    continue
                if error.code >= 500 and attempt < self.max_retries:
                    self._sleep(2**attempt)
                    continue
                raise GitHubError(
                    f"GitHub API returned HTTP {error.code} for {url}: "
                    f"{body[:500]}"
                ) from error
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt < self.max_retries:
                    self._sleep(2**attempt)
                    continue
                raise GitHubError(
                    f"GitHub request failed for {url}: {error}"
                ) from error
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise GitHubError(
                    f"GitHub returned invalid JSON for {url}"
                ) from error
        raise AssertionError("GitHub retry loop exhausted")

    def _request_url(
        self,
        path_or_url: str,
        params: dict[str, Any] | None,
    ) -> str:
        url = (
            path_or_url
            if path_or_url.startswith(("https://", "http://"))
            else f"{self.api_root}{path_or_url}"
        )
        parsed = urllib.parse.urlsplit(url)
        api = urllib.parse.urlsplit(self.api_root)
        if (
            parsed.scheme != "https"
            or parsed.hostname != api.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("GitHub API URL must remain on the configured host")
        if params:
            query = urllib.parse.urlencode(params)
            url = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, query, "")
            )
        return url

    def _update_status(self, headers: dict[str, str]) -> None:
        for attribute, header in (
            ("remaining", "x-ratelimit-remaining"),
            ("limit", "x-ratelimit-limit"),
            ("reset_at", "x-ratelimit-reset"),
        ):
            value = headers.get(header)
            if value is not None:
                try:
                    setattr(self.status, attribute, int(value))
                except ValueError:
                    continue

    def _wait_for_limit(
        self,
        headers: dict[str, str],
        attempt: int,
    ) -> None:
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                self._sleep(min(int(retry_after), 120))
                return
            except ValueError:
                pass
        reset_at = headers.get("x-ratelimit-reset")
        if reset_at and headers.get("x-ratelimit-remaining") == "0":
            try:
                wait = max(1, int(reset_at) - int(self._clock()) + 1)
                self._sleep(min(wait, 120))
                return
            except ValueError:
                pass
        self._sleep(2**attempt)


def issue_evidence_score(issue: dict[str, Any]) -> int:
    """Compute a stable evidence score from Issue-only metadata."""

    reactions = issue.get("reactions") or {}
    positive_reactions = sum(
        _non_negative_int(reactions.get(name))
        for name in ("+1", "heart", "rocket", "eyes")
    )
    body_score = 4 if str(issue.get("body") or "").strip() else 0
    label_score = min(
        sum(
            bool(isinstance(label, dict) and label.get("name"))
            for label in issue.get("labels") or []
        ),
        10,
    )
    return (
        body_score
        + label_score
        + min(_non_negative_int(issue.get("comments")), 20)
        + min(positive_reactions, 20)
    )


def discover_github_token() -> str | None:
    """Discover a GitHub token without making a network request."""

    for name in ("GITHUB_FINE_GRAINED_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(name)
        if token and token.strip():
            return token.strip()
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def parse_github_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("GitHub timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _split_repository(repository: str) -> tuple[str, str]:
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("repository must use owner/name form")
    return parts[0], parts[1]


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "GitHubApiStatus",
    "GitHubClient",
    "GitHubError",
    "LinkedPullRequestIdentity",
    "discover_github_token",
    "issue_evidence_score",
    "parse_github_time",
]
