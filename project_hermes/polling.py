"""Scheduled GitHub Issue polling with mechanical filtering and Work enqueue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, Iterable, Literal
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


@dataclass(frozen=True, slots=True)
class _RepositoryDiscoveryBatch:
    candidates: tuple[PollingCandidate, ...]
    window_start: datetime
    window_end: datetime
    scan_mode: Literal["rolling", "fresh", "backfill"]
    truncated: bool = False
    next_backfill_cursor: datetime | None = None


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
                require_screening_select=self.config.issue_screening_enabled,
                now=timestamp,
            )
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
        timestamp = _utc(now or utc_now())
        try:
            return self._run(now=timestamp)
        except Exception:
            # Never leave a durable run wedged in RUNNING after an unexpected
            # request or parsing failure. This is the same fail-closed recovery
            # performed when the process restarts after an interruption.
            self.store.recover_interrupted_run(now=timestamp)
            raise
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
            scan_window_start = run.cutoff
            scan_window_end = now
            scan_mode: Literal["rolling", "fresh", "backfill"] = "rolling"
            next_backfill_cursor: datetime | None = None
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
                batch = self._repository_batch(
                    run_id=run.run_id,
                    repository=repository,
                    repository_config=repository_config,
                    now=now,
                    limit=repository_limit,
                )
                scan_window_start = batch.window_start
                scan_window_end = batch.window_end
                scan_mode = batch.scan_mode
                next_backfill_cursor = batch.next_backfill_cursor
                for candidate in batch.candidates:
                    repository_issues += 1
                    issues_seen += 1
                    _stored, was_queued = self.store.save_candidate(
                        candidate,
                        enqueue_eligible=(
                            not self.config.require_operator_selection
                            and not self.config.issue_screening_enabled
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
                if batch.truncated:
                    partial += 1
                    scanned += 1
                    detail = (
                        f"bounded {scan_mode} scan reached the configured "
                        "open-Issue limit; continuing repository rotation"
                    )
                    errors.append(f"{repository_config.repository}: {detail}")
                    self.store.finish_repository_scan(
                        PollingRunRepository(
                            run_id=run.run_id,
                            repository_id=repository_id_value,
                            repository=repository_config.repository,
                            status=RepositoryScanStatus.PARTIAL,
                            window_start=scan_window_start,
                            window_end=scan_window_end,
                            scan_mode=scan_mode,
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
                        window_start=scan_window_start,
                        window_end=scan_window_end,
                        scan_mode=scan_mode,
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
                    window_start=scan_window_start,
                    window_end=scan_window_end,
                    scan_mode=scan_mode,
                    issues_seen=repository_issues,
                    candidates_matched=repository_matched,
                    work_items_queued=repository_queued,
                    started_at=started_at,
                    completed_at=utc_now(),
                ),
                next_backfill_cursor=next_backfill_cursor,
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

    def _repository_batch(
        self,
        *,
        run_id: str,
        repository: PollingRepository,
        repository_config: PollingRepositoryConfig,
        now: datetime,
        limit: int,
    ) -> _RepositoryDiscoveryBatch:
        """Choose a fresh or one-day historical window for one repository."""

        github_repository = repository_config.resolved_github_repository
        cursor = self.store.repository_backfill_cursor(repository.repository)
        if cursor is None:
            window_start = now - timedelta(days=self.config.rolling_window_days)
            candidates, truncated = self._collect_candidates(
                self.client.recent_open_issues(
                    github_repository,
                    window_start,
                    max_pages=self.config.max_issue_pages_per_repository,
                ),
                run_id=run_id,
                repository=repository,
                repository_config=repository_config,
                discovered_at=now,
                limit=limit,
            )
            return _RepositoryDiscoveryBatch(
                candidates=candidates,
                window_start=window_start,
                window_end=now,
                scan_mode="rolling",
                truncated=truncated,
                # Revisit the partial UTC day at the old boundary before
                # moving to days that precede the initial rolling window.
                next_backfill_cursor=_utc_day_start(window_start),
            )

        today = _utc_day_start(now)
        fresh_candidates, fresh_truncated = self._collect_candidates(
            self.client.recent_open_issues(
                github_repository,
                today,
                max_pages=self.config.max_issue_pages_per_repository,
            ),
            run_id=run_id,
            repository=repository,
            repository_config=repository_config,
            discovered_at=now,
            limit=limit,
        )
        if fresh_truncated or any(
            self.store.candidate_snapshot_changed(candidate)
            for candidate in fresh_candidates
        ):
            return _RepositoryDiscoveryBatch(
                candidates=fresh_candidates,
                window_start=today,
                window_end=now,
                scan_mode="fresh",
                truncated=fresh_truncated,
            )

        window_start = min(cursor, today - timedelta(days=1))
        window_end = window_start + timedelta(days=1)
        candidates, truncated = self._collect_candidates(
            self.client.open_issues_created_between(
                github_repository,
                window_start,
                window_end,
                max_pages=self.config.max_issue_pages_per_repository,
            ),
            run_id=run_id,
            repository=repository,
            repository_config=repository_config,
            discovered_at=now,
            limit=limit,
        )
        return _RepositoryDiscoveryBatch(
            candidates=candidates,
            window_start=window_start,
            window_end=window_end,
            scan_mode="backfill",
            truncated=truncated,
            next_backfill_cursor=window_start - timedelta(days=1),
        )

    def _collect_candidates(
        self,
        issues: Iterable[dict[str, Any]],
        *,
        run_id: str,
        repository: PollingRepository,
        repository_config: PollingRepositoryConfig,
        discovered_at: datetime,
        limit: int,
    ) -> tuple[tuple[PollingCandidate, ...], bool]:
        """Materialize a bounded API iterator and detect local truncation."""

        self.client.truncated = False
        candidates: list[PollingCandidate] = []
        overflow = False
        for issue in issues:
            if len(candidates) >= limit:
                overflow = True
                break
            candidates.append(
                self._candidate(
                    run_id=run_id,
                    repository=repository,
                    repository_config=repository_config,
                    issue=issue,
                    discovered_at=discovered_at,
                )
            )
        return tuple(candidates), bool(
            overflow or getattr(self.client, "truncated", False)
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
        github_repository = config.resolved_github_repository
        payload, _headers = self.client.get(f"/repos/{github_repository}")
        if not isinstance(payload, dict):
            raise GitHubError("GitHub repository response is not an object")
        repository_id_value = int(payload["id"])
        canonical_name = str(payload["full_name"])
        if canonical_name.casefold() != github_repository.casefold():
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
            repository=config.repository,
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
        accepted_paths = {
            f"/{repository.repository}/issues/{number}".casefold(),
            (
                f"/{repository_config.resolved_github_repository}/issues/"
                f"{number}"
            ).casefold(),
        }
        parsed = urlparse(issue_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.path.rstrip("/").casefold() not in accepted_paths
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


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("polling timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _utc_day_start(value: datetime) -> datetime:
    return _utc(value).replace(hour=0, minute=0, second=0, microsecond=0)


__all__ = ["IssuePollingService"]
