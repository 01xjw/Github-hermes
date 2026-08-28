"""Controller-owned IssueTask to isolated Codex worker launch."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from project_hermes.artifacts import SqliteArtifactRegistry
from project_hermes.candidate_ingest import WorkerCandidatePayload
from project_hermes.config import ProjectHermesConfig
from project_hermes.execution import (
    ExecutionCoordinator,
    ExecutionRequest,
    ExecutionStatus,
)
from project_hermes.kubernetes_jobs import CodexWorkerMetadata
from project_hermes.models import IssueTask, StrictModel
from project_hermes.release import VerifiedActiveRelease, parse_runtime_image
from project_hermes.repositories import RepositoryBaseline
from project_hermes.repository_skills import (
    RepositorySkillReference,
    RepositorySkillRegistry,
)
from project_hermes.resources import ExecutionEnvironment, GpuRequest, WorkspaceLease
from project_hermes.supply_chain import TaskSourceBundleMetadata
from project_hermes.workspaces import GitWorkspaceManager


class RepositoryResolver(Protocol):
    """Resolve a fixed repository's validated GitHub clone identity."""

    def resolve(self, repository: str) -> RepositoryBaseline:
        """Return canonical clone metadata and a current immutable baseline."""


class IssueLaunchResult(StrictModel):
    """Durable identities created for one Issue lane."""

    task_id: str
    run_id: str
    workspace_lease_id: str
    source_bundle_request_id: str
    source_bundle_digest: str
    execution_id: str
    execution_status: ExecutionStatus


class IssueLauncher:
    """Build all worker inputs from locked controller state, never agent input."""

    node_id = "project-codex-worker"

    def __init__(
        self,
        config: ProjectHermesConfig,
        repository_resolver: RepositoryResolver,
        workspaces: GitWorkspaceManager,
        artifacts: SqliteArtifactRegistry,
        execution: ExecutionCoordinator,
        active_release: VerifiedActiveRelease,
    ) -> None:
        self.config = config
        self.repository_resolver = repository_resolver
        self.workspaces = workspaces
        self.artifacts = artifacts
        self.execution = execution
        self.active_release = active_release
        self.repository_skills = RepositorySkillRegistry(config.polling)
        if config.polling.require_repository_skills:
            self.repository_skills.validate_all()

    def launch(self, task: IssueTask, run_id: str) -> IssueLaunchResult:
        """Create or recover one deterministic source bundle and execution."""

        if len(task.repositories) != 1:
            raise ValueError("Issue launcher requires exactly one repository")
        repository = task.repositories[0].repository
        repository_skill = self._repository_skill(task, repository)
        baseline_ref, baseline_sha = _named_baseline(task.named_baseline)
        resolved = self.repository_resolver.resolve(repository)
        lease = self._workspace(
            task,
            repository=repository,
            clone_source=resolved.clone_source,
            legacy_clone_sources=(
                (f"https://github.com/{repository}.git",)
                if resolved.github_repository is not None
                else ()
            ),
            baseline_sha=baseline_sha,
        )
        self.workspaces.assert_clean(lease)
        source_candidate_digest = self.workspaces.candidate_digest(lease)
        source_request_id = _identifier(
            "source",
            task.task_id,
            baseline_sha,
        )
        source_metadata = TaskSourceBundleMetadata(
            task_id=task.task_id,
            repository=repository,
            workspace_lease_id=lease.lease_id,
            base_sha=baseline_sha,
            candidate_digest=source_candidate_digest,
        )
        try:
            source_record = self.artifacts.require_source_bundle(source_request_id)
        except KeyError:
            source_record = self.artifacts.create_source_bundle(
                lease.worktree_path,
                request_id=source_request_id,
                metadata=source_metadata,
            )
        if source_record.metadata != source_metadata:
            raise ValueError(
                "persisted source bundle differs from controller launch state"
            )

        request = self._execution_request(
            task,
            run_id=run_id,
            repository=repository,
            baseline_ref=baseline_ref,
            baseline_sha=baseline_sha,
            lease=lease,
            source_request_id=source_request_id,
            source_digest=source_record.manifest.resolved_digest,
            source_candidate_digest=source_candidate_digest,
            repository_skill=repository_skill,
        )
        execution = self.execution.submit(request)
        if execution.status in {
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            raise RuntimeError(
                execution.blocker or f"worker execution ended {execution.status.value}"
            )
        return IssueLaunchResult(
            task_id=task.task_id,
            run_id=run_id,
            workspace_lease_id=lease.lease_id,
            source_bundle_request_id=source_request_id,
            source_bundle_digest=source_record.manifest.resolved_digest,
            execution_id=execution.execution_id,
            execution_status=execution.status,
        )

    def _workspace(
        self,
        task: IssueTask,
        *,
        repository: str,
        clone_source: str,
        legacy_clone_sources: tuple[str, ...],
        baseline_sha: str,
    ) -> WorkspaceLease:
        owner = _identifier("issue-workspace", task.task_id)
        active = [
            lease
            for lease in self.workspaces.active_for_task(task.task_id)
            if lease.repository == repository
        ]
        if len(active) > 1:
            raise RuntimeError("multiple active worktrees exist for one Issue")
        if active:
            lease = active[0]
            if lease.owner_session_id != owner or lease.base_sha != baseline_sha:
                raise ValueError(
                    "active Issue worktree differs from locked launch identity"
                )
            return lease
        return self.workspaces.acquire(
            task_id=task.task_id,
            repository=repository,
            source=clone_source,
            base_ref=baseline_sha,
            owner_session_id=owner,
            allowed_existing_sources=legacy_clone_sources,
        )

    def _execution_request(
        self,
        task: IssueTask,
        *,
        run_id: str,
        repository: str,
        baseline_ref: str,
        baseline_sha: str,
        lease: WorkspaceLease,
        source_request_id: str,
        source_digest: str,
        source_candidate_digest: str,
        repository_skill: RepositorySkillReference | None,
    ) -> ExecutionRequest:
        runtime_image = self.active_release.runtime_image
        image_repository, image_digest = parse_runtime_image(runtime_image)
        route = self.config.codex.route(self.config.codex.primary_profile)
        handoff_route = self.config.hermes.route_for_worker_handoff()
        if handoff_route.provider_wire_api != "chat":
            raise ValueError("Hermes handoff route must use chat")
        worker = CodexWorkerMetadata(
            release_digest=self.active_release.record.active_release_digest,
            source_bundle_request_id=source_request_id,
            source_bundle_digest=source_digest,
            base_sha=baseline_sha,
            model_profile=route.profile,
            model=route.model,
            model_provider=route.model_provider,
            provider_endpoint=route.provider_endpoint,
            provider_api_key_env=route.provider_api_key_env,
            provider_wire_api=route.provider_wire_api,
            reasoning_effort=route.reasoning_effort,
            context_window=route.context_window,
            handoff_model_profile=handoff_route.profile,
            handoff_model=handoff_route.model,
            handoff_model_provider=handoff_route.model_provider,
            handoff_provider_endpoint=handoff_route.provider_endpoint,
            handoff_provider_api_key_env=handoff_route.provider_api_key_env,
            handoff_provider_wire_api=handoff_route.provider_wire_api,
            repository_skill_name=(
                repository_skill.name if repository_skill is not None else None
            ),
            repository_skill_digest=(
                repository_skill.digest if repository_skill is not None else None
            ),
        )
        request_id = _identifier("worker-request", task.task_id)
        gpu = self._gpu_request(
            task,
            request_id=_identifier("worker-gpu", task.task_id),
            image_digest=image_digest,
        )
        return ExecutionRequest(
            request_id=request_id,
            task_id=task.task_id,
            run_id=run_id,
            node_id=self.node_id,
            repository=repository,
            workspace_lease_id=lease.lease_id,
            candidate_digest=source_candidate_digest,
            command=[
                "codex",
                "exec",
                "--json",
                "--output-last-message",
                "/outputs/result.json",
                _worker_prompt(
                    task,
                    execution_id_placeholder=(
                        "Use PROJECT_HERMES_EXECUTION_ID from the environment"
                    ),
                    repository=repository,
                    baseline_ref=baseline_ref,
                    baseline_sha=baseline_sha,
                    source_candidate_digest=source_candidate_digest,
                    repository_skill=repository_skill,
                ),
            ],
            environment=ExecutionEnvironment.KUBERNETES,
            image_repository=image_repository,
            image_digest=image_digest,
            artifact_request_ids=[source_request_id],
            artifact_byte_limit=task.resource_limits.max_artifact_bytes,
            gpu=gpu,
            timeout_seconds=min(
                self.config.codex.turn_timeout_seconds,
                self.config.kubernetes_jobs.max_timeout_seconds,
            ),
            network_access=False,
            idempotency_key="project-codex-worker.v1",
            metadata={
                "codex_worker": worker.model_dump(mode="json"),
                "controller_owned": True,
                "result_contract": "project-hermes-worker-candidate.v1",
            },
            created_at=task.locked_at,
        )

    def _repository_skill(
        self,
        task: IssueTask,
        repository: str,
    ) -> RepositorySkillReference | None:
        """Lock a task to the exact configured repository Skill identity."""

        configured = self.repository_skills.resolve(repository)
        raw = task.metadata.get("repository_skill")
        if raw is None:
            if configured is not None:
                raise ValueError(
                    "locked IssueTask is missing its repository Skill identity"
                )
            return None
        if not isinstance(raw, dict):
            raise ValueError("IssueTask repository_skill must be an object")
        supplied = RepositorySkillReference.model_validate(raw)
        if configured is None:
            raise ValueError(
                "IssueTask names a repository Skill outside the configured registry"
            )
        if (
            supplied.repository.casefold() != repository.casefold()
            or supplied.name != configured.name
            or supplied.digest != configured.digest
        ):
            raise ValueError(
                "IssueTask repository Skill differs from the configured release"
            )
        return configured

    def _gpu_request(
        self,
        task: IssueTask,
        *,
        request_id: str,
        image_digest: str,
    ) -> GpuRequest | None:
        count = task.target_hardware.minimum_gpu_count
        if count == 0:
            return None
        if (
            count > task.resource_limits.max_gpu_count
            or count > self.config.polling.max_global_gpus
        ):
            raise ValueError("locked task GPU request exceeds polling limits")
        requested = task.target_hardware.gpu_architectures
        architecture: str | None = None
        if requested:
            for candidate in requested:
                matching = [
                    device
                    for device in self.config.gpu_devices
                    if device.healthy and device.architecture == candidate
                ]
                if len(matching) >= count:
                    architecture = candidate
                    break
            if architecture is None:
                raise ValueError(
                    "configured GPU pool cannot satisfy target architecture"
                )
        return GpuRequest(
            request_id=request_id,
            task_id=task.task_id,
            count=count,
            architecture=architecture,
            topology=task.target_hardware.topology,
            image_digest=image_digest,
            environment=ExecutionEnvironment.KUBERNETES,
            created_at=task.locked_at,
        )


def _worker_prompt(
    task: IssueTask,
    *,
    execution_id_placeholder: str,
    repository: str,
    baseline_ref: str,
    baseline_sha: str,
    source_candidate_digest: str,
    repository_skill: RepositorySkillReference | None,
) -> str:
    schema = WorkerCandidatePayload.model_json_schema()
    identity = {
        "execution_id": execution_id_placeholder,
        "task_id": task.task_id,
        "repository": repository,
        "source_candidate_digest": source_candidate_digest,
        "base_ref": baseline_ref,
        "base_sha": baseline_sha,
        "result_path": "/outputs/result.json",
    }
    skill_instruction = (
        "Before inspecting or changing code, invoke the controller-locked "
        f"repository Skill `${repository_skill.name}` and follow it for this "
        "task. Do not load a Skill for another repository.\n"
        if repository_skill is not None
        else ""
    )
    return (
        "Implement the locked ProjectHermes IssueTask in the current worktree. "
        "Do not access GitHub, publish, push, or expand repository scope. "
        "Commit the finished local changes with the fixed identity "
        "'ProjectHermes Worker <worker@project-hermes.invalid>' and report "
        "the resulting local commit chain and HEAD in the result. "
        "Finish by returning exactly one JSON object matching the supplied "
        "schema, without a prose summary or code fence. Do not write "
        "/outputs/result.json with a tool; ProjectHermes captures your final "
        "response there. "
        "Set execution_id to the PROJECT_HERMES_EXECUTION_ID environment value. "
        f"Set head_ref exactly to project-hermes/{task.task_id}. "
        "files[] is the production-only review projection: omit every local "
        "test-overlay path (test/, tests/, any /test segment, *_test.py, "
        ".spec.ts, and .test.ts), even when that file remains in the local "
        "commit. Preserve test execution evidence in checks[] instead.\n"
        + skill_instruction
        + "\n"
        "Controller identity:\n"
        + json.dumps(identity, sort_keys=True, ensure_ascii=True)
        + "\n\nResult JSON schema:\n"
        + json.dumps(schema, sort_keys=True, ensure_ascii=True)
        + "\n\nLocked IssueTask:\n"
        + task.model_dump_json()
    )


def _named_baseline(value: str) -> tuple[str, str]:
    branch, separator, sha = value.rpartition("@")
    if (
        not separator
        or not branch
        or len(sha) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in sha)
    ):
        raise ValueError("locked task named_baseline is malformed")
    return branch, sha


def _identifier(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


__all__ = [
    "IssueLaunchResult",
    "IssueLauncher",
    "RepositoryResolver",
]
