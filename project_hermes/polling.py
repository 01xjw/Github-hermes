"""Scheduled GitHub Issue polling with mechanical filtering and Work enqueue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, Iterable
from urllib.parse import urlparse
from uuid import uuid4

from project_hermes.config import PollingConfig, PollingRepositoryConfig
from project_hermes.discovery import (
    GitHubClient,
    GitHubError,
    issue_evidence_score,
    parse_github_time,
)
from project_hermes.issue_relevance import issue_relevance_reasons
from project_hermes.models import utc_now
from project_hermes.polling_store import (
    PollingCandidate,
    PollingRepository,
    PollingRun,
    PollingRunRepository,
    PollingRunStatus,
    RepositoryScanStatus,
    SqlitePollingStore,
    candidate_id,
)
from project_hermes.redaction import redact_text


class IssuePollingService:
    """Run one bounded scan over the configured repository registry."""

    def __init__(
        self,
        client: GitHubClient,
        store: SqlitePollingStore,
        config: PollingConfig,
    ) -> None:
        self.client = client
        self.store = store
        self.config = config
        self._run_lock = Lock()
        self._relevance_reconciled = False
        self._repository_configs = {
            item.repository.casefold(): item for item in config.repositories
        }
        if self.config.require_operator_selection:
            self.store.hold_unselected_work_items()

    def run_if_due(self, *, now: datetime | None = None) -> PollingRun | None:
        timestamp = _utc(now or utc_now())
        self._reconcile_pending_relevance(now=timestamp)
        if not self.config.require_operator_selection:
            self.store.enqueue_waiting_candidates(
                max_pending_work_items=self.config.max_pending_work_items,
                now=timestamp,
            )
            if (
                self.store.pending_work_count()
                >= self.config.max_pending_work_items
            ):
                return None
        if not self.store.is_due(now=timestamp):
            return None
        return self.run_now(now=timestamp)

    def _reconcile_pending_relevance(self, *, now: datetime) -> None:
        """Apply a stricter hardware policy to every pre-existing candidate."""

        if self._relevance_reconciled:
            return
        total = self.store.count_candidates(eligible=True)
        candidates = [
            candidate
            for offset in range(0, total, 500)
            for candidate in self.store.list_candidates(
                eligible=True,
                limit=500,
                offset=offset,
            )
        ]
        for candidate in candidates:
            repository = self._repository_configs.get(
                candidate.repository.casefold()
            )
            if repository is None or not repository.enabled:
                reasons = [
                    "repository is no longer enabled by the polling registry"
                ]
            else:
                reasons = issue_relevance_reasons(
                    policy=repository.relevance_policy,
                    title=candidate.title,
                    body=candidate.body,
                    labels=candidate.labels,
                )
            if reasons:
                self.store.exclude_candidate(
                    candidate.candidate_id,
                    reasons=reasons,
                    now=now,
                )
        self._relevance_reconciled = True

    def run_now(self, *, now: datetime | None = None) -> PollingRun:
        """Execute a full scan, rejecting concurrent manual or timer runs."""

        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("a polling run is already active")
        try:
            return self._run(now=_utc(now or utc_now()))
        finally:
            self._run_lock.release()

    def _run(self, *, now: datetime) -> PollingRun:
        configured_repositories = [
            repository
            for repository in self.config.repositories
            if repository.enabled
        ]
        last_attempts = self.store.repository_last_attempts()
        configured_order = {
            repository.repository.casefold(): index
            for index, repository in enumerate(configured_repositories)
        }
        repositories = sorted(
            configured_repositories,
            key=lambda repository: (
                repository.repository.casefold() in last_attempts,
                last_attempts.get(repository.repository.casefold(), now),
                configured_order[repository.repository.casefold()],
            ),
        )[: self.config.repositories_per_run]
        run = PollingRun(
            run_id=f"poll-{uuid4().hex}",
            status=PollingRunStatus.RUNNING,
            cutoff=now - timedelta(days=self.config.rolling_window_days),
            repositories_requested=len(repositories),
            started_at=now,
        )
        self.store.begin_run(run)
        scanned = 0
        partial = 0
        failed = 0
        issues_seen = 0
        matched = 0
        queued = 0
        errors: list[str] = []
        for repository_config in repositories:
            started_at = utc_now()
            self.store.begin_repository_scan(
                run.run_id,
                repository_config.repository,
                started_at=started_at,
            )
            repository_id_value: int | None = None
            repository_issues = 0
            repository_matched = 0
            repository_queued = 0
            try:
                repository = self._repository_metadata(
                    repository_config,
                    now=started_at,
                )
                repository_id_value = repository.repository_id
                self.store.upsert_repository(repository)
                remaining = max(
                    0,
                    self.config.max_candidates_per_run - matched,
                )
                repository_limit = min(
                    self.config.max_candidates_per_repository,
                    remaining,
                )
                # ``recent_open_issues`` resets this flag when iteration
                # starts. A zero remaining budget never starts the generator,
                # so clear the previous repository's state explicitly.
                self.client.truncated = False
                for issue in _take(
                    self.client.recent_open_issues(
                        repository.repository,
                        run.cutoff,
                        max_pages=(
                            self.config.max_issue_pages_per_repository
                        ),
                    ),
                    repository_limit,
                ):
                    repository_issues += 1
                    issues_seen += 1
                    candidate = self._candidate(
                        run_id=run.run_id,
                        repository=repository,
                        repository_config=repository_config,
                        issue=issue,
                        discovered_at=now,
                    )
                    _stored, was_queued = self.store.save_candidate(
                        candidate,
                        enqueue_eligible=(
                            not self.config.require_operator_selection
                        ),
                        max_pending_work_items=(
                            self.config.max_pending_work_items
                        ),
                    )
                    if candidate.eligible:
                        repository_matched += 1
                        matched += 1
                    if was_queued:
                        repository_queued += 1
                        queued += 1
                if getattr(self.client, "truncated", False):
                    partial += 1
                    scanned += 1
                    detail = (
                        "bounded scan reached the configured open-Issue page "
                        "limit; continuing repository rotation"
                    )
                    errors.append(f"{repository_config.repository}: {detail}")
                    self.store.finish_repository_scan(
                        PollingRunRepository(
                            run_id=run.run_id,
                            repository_id=repository_id_value,
                            repository=repository_config.repository,
                            status=RepositoryScanStatus.PARTIAL,
                            issues_seen=repository_issues,
                            candidates_matched=repository_matched,
                            work_items_queued=repository_queued,
                            started_at=started_at,
                            completed_at=utc_now(),
                            error=detail,
                        )
                    )
                    continue
            except (GitHubError, KeyError, TypeError, ValueError) as exc:
                failed += 1
                detail = redact_text(str(exc)) or type(exc).__name__
                errors.append(f"{repository_config.repository}: {detail}")
                self.store.finish_repository_scan(
                    PollingRunRepository(
                        run_id=run.run_id,
                        repository_id=repository_id_value,
                        repository=repository_config.repository,
                        status=RepositoryScanStatus.FAILED,
                        issues_seen=repository_issues,
                        candidates_matched=repository_matched,
                        work_items_queued=repository_queued,
                        started_at=started_at,
                        completed_at=utc_now(),
                        error=detail,
                    )
                )
                continue
            scanned += 1
            self.store.finish_repository_scan(
                PollingRunRepository(
                    run_id=run.run_id,
                    repository_id=repository_id_value,
                    repository=repository_config.repository,
                    status=RepositoryScanStatus.COMPLETED,
                    issues_seen=repository_issues,
                    candidates_matched=repository_matched,
                    work_items_queued=repository_queued,
                    started_at=started_at,
                    completed_at=utc_now(),
                )
            )

        if failed == len(repositories) and repositories:
            status = PollingRunStatus.FAILED
        elif failed or partial:
            status = PollingRunStatus.PARTIAL
        else:
            status = PollingRunStatus.COMPLETED
        completed_at = utc_now()
        completed = run.model_copy(
            update={
                "status": status,
                "repositories_scanned": scanned,
                "repositories_failed": failed,
                "issues_seen": issues_seen,
                "candidates_matched": matched,
                "work_items_queued": queued,
                "completed_at": completed_at,
                "error": "; ".join(errors[:10]) if errors else None,
            }
        )
        return self.store.finish_run(
            PollingRun.model_validate(completed),
            next_run_at=self._next_repository_due_at(completed_at),
        )

    def select_candidate(
        self,
        candidate_id_value: str,
        *,
        selected_by: str,
        now: datetime | None = None,
    ):
        """Admit one operator-approved Issue into the unchanged Work flow."""

        if not self.config.require_operator_selection:
            raise RuntimeError("operator Issue selection is not enabled")
        return self.store.select_candidate_for_work(
            candidate_id_value,
            selected_by=selected_by,
            max_pending_work_items=self.config.max_pending_work_items,
            now=now,
        )

    def _next_repository_due_at(self, completed_at: datetime) -> datetime:
        """Schedule the next repo immediately until the registry is caught up."""

        last_attempts = self.store.repository_last_attempts()
        due_at: list[datetime] = []
        for repository in self.config.repositories:
            if not repository.enabled:
                continue
            last_attempt = last_attempts.get(repository.repository.casefold())
            if last_attempt is None:
                return completed_at
            due_at.append(
                last_attempt
                + timedelta(seconds=self.config.interval_seconds)
            )
        return min(due_at, default=completed_at + timedelta(
            seconds=self.config.interval_seconds
        ))

    def _repository_metadata(
        self,
        config: PollingRepositoryConfig,
        *,
        now: datetime,
    ) -> PollingRepository:
        payload, _headers = self.client.get(f"/repos/{config.repository}")
        if not isinstance(payload, dict):
            raise GitHubError("GitHub repository response is not an object")
        repository_id_value = int(payload["id"])
        canonical_name = str(payload["full_name"])
        if canonical_name.casefold() != config.repository.casefold():
            raise GitHubError(
                "GitHub repository identity differs from configured owner/name"
            )
        html_url = str(payload["html_url"])
        parsed = urlparse(html_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.path.rstrip("/").casefold()
            != f"/{canonical_name}".casefold()
            or parsed.query
            or parsed.fragment
        ):
            raise GitHubError("GitHub repository URL is not canonical")
        default_branch = str(payload.get("default_branch") or "").strip()
        if not default_branch:
            raise GitHubError("GitHub repository has no default branch")
        return PollingRepository(
            repository_id=repository_id_value,
            repository=canonical_name,
            enabled=config.enabled,
            default_branch=default_branch,
            html_url=html_url,
            created_at=now,
            updated_at=now,
        )

    def _candidate(
        self,
        *,
        run_id: str,
        repository: PollingRepository,
        repository_config: PollingRepositoryConfig,
        issue: dict[str, Any],
        discovered_at: datetime,
    ) -> PollingCandidate:
        if "pull_request" in issue or issue.get("state") != "open":
            raise ValueError("polling accepts only open GitHub Issues")
        number = int(issue["number"])
        issue_url = str(issue["html_url"])
        expected_path = f"/{repository.repository}/issues/{number}".casefold()
        parsed = urlparse(issue_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.path.rstrip("/").casefold() != expected_path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Issue URL does not match its repository identity")
        labels = sorted(
            {
                str(label.get("name")).strip()
                for label in issue.get("labels") or []
                if isinstance(label, dict) and str(label.get("name") or "").strip()
            },
            key=str.casefold,
        )
        label_keys = {label.casefold() for label in labels}
        body = str(issue.get("body") or "")
        title = str(issue.get("title") or "").strip()
        if not title:
            title = f"Issue #{number}"
        score = issue_evidence_score(issue)
        reasons: list[str] = []
        excluded = set(self.config.exclude_labels) | set(
            repository_config.exclude_labels
        )
        excluded_present = sorted(label_keys & excluded)
        if excluded_present:
            reasons.append(
                "excluded labels: " + ", ".join(excluded_present)
            )
        if (
            repository_config.include_labels
            and not label_keys.intersection(repository_config.include_labels)
        ):
            reasons.append("none of the configured include labels are present")
        threshold = (
            repository_config.minimum_evidence_score
            if repository_config.minimum_evidence_score is not None
            else self.config.minimum_evidence_score
        )
        if score < threshold:
            reasons.append(
                f"evidence score {score} is below configured minimum {threshold}"
            )
        if self.config.require_body and not body.strip():
            reasons.append("Issue has no body")
        if bool(issue.get("locked")):
            reasons.append("Issue discussion is locked")
        reasons.extend(
            issue_relevance_reasons(
                policy=repository_config.relevance_policy,
                title=title,
                body=body,
                labels=labels,
            )
        )
        created_at = parse_github_time(str(issue["created_at"]))
        updated_raw = issue.get("updated_at") or issue["created_at"]
        updated_at = parse_github_time(str(updated_raw))
        if not str(issue.get("title") or "").strip():
            reasons.append("Issue title is empty")
        user = issue.get("user")
        author = (
            str(user.get("login"))
            if isinstance(user, dict) and user.get("login")
            else None
        )
        return PollingCandidate(
            candidate_id=candidate_id(repository.repository_id, number),
            repository_id=repository.repository_id,
            repository=repository.repository,
            issue_number=number,
            issue_url=issue_url,
            title=title,
            body=body,
            labels=labels,
            author=author,
            comments=max(0, int(issue.get("comments") or 0)),
            evidence_score=score,
            eligible=not reasons,
            filter_reasons=reasons,
            created_at=created_at,
            updated_at=updated_at,
            first_seen_at=discovered_at,
            last_seen_at=discovered_at,
            latest_run_id=run_id,
        )


def _take(items: Iterable[dict[str, Any]], limit: int) -> Iterable[dict[str, Any]]:
    if limit <= 0:
        return
    for index, item in enumerate(items):
        if index >= limit:
            return
        yield item


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("polling timestamps must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ["IssuePollingService"]
