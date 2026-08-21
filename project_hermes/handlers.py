"""Built-in outer-loop action handler factories."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from project_hermes.assurance import ReviewPacket
from project_hermes.assurance_store import AssuranceStore
from project_hermes.controller import ActionHandler
from project_hermes.execution import ExecutionCoordinator, ExecutionRequest
from project_hermes.models import IssueTask, ProjectRole
from project_hermes.policy import PolicyContext
from project_hermes.redaction import redact_data
from project_hermes.resources import WorkspaceLease
from project_hermes.work_graph import ActionRequest


def execution_action_handler(
    coordinator: ExecutionCoordinator,
    *,
    workspace_lookup: Callable[[str], WorkspaceLease],
    workspace_digest: Callable[[WorkspaceLease], str],
    run_id_lookup: Callable[[str], str] | None = None,
) -> ActionHandler:
    """Create a policy-adjacent execution admission handler."""

    def handle(
        task: IssueTask,
        action: ActionRequest,
        context: PolicyContext,
    ):
        execution = ExecutionRequest.model_validate(action.payload)
        execution_payload = execution.model_dump(mode="json")
        if redact_data(execution_payload) != execution_payload:
            raise ValueError(
                "execution request contains credential-shaped data"
            )
        if execution.task_id != task.task_id:
            raise ValueError("execution request belongs to another task")
        if run_id_lookup is not None:
            expected_run_id = run_id_lookup(task.task_id)
            if execution.run_id not in {None, expected_run_id}:
                raise ValueError("execution request belongs to another run")
            execution = execution.model_copy(
                update={"run_id": expected_run_id}
            )
        if execution.node_id != action.node_id:
            raise ValueError("execution request belongs to another node")
        if execution.repository != action.repository:
            raise ValueError("execution repository differs from the action")
        if execution.artifact_byte_limit not in {
            None,
            task.resource_limits.max_artifact_bytes,
        }:
            raise PermissionError(
                "agent cannot change the locked artifact byte limit"
            )
        execution = execution.model_copy(
            update={
                "artifact_byte_limit": (
                    task.resource_limits.max_artifact_bytes
                )
            }
        )
        workspace = workspace_lookup(execution.workspace_lease_id)
        if (
            workspace.task_id != task.task_id
            or workspace.repository != execution.repository
            or workspace.released_at is not None
        ):
            raise PermissionError(
                "execution does not have an active matching workspace lease"
            )
        if (
            action.requested_by is not ProjectRole.CONTROL_PLANE
            and (
                context.actor_session_id is None
                or workspace.owner_session_id != context.actor_session_id
            )
        ):
            raise PermissionError(
                "execution workspace is owned by another session"
            )
        if workspace_digest(workspace) != execution.candidate_digest:
            raise ValueError(
                "execution candidate digest differs from the leased worktree"
            )
        if execution.network_access and not task.permissions.may_access_network:
            raise PermissionError("task permission budget forbids network access")
        if execution.gpu is not None:
            if execution.gpu.count > task.resource_limits.max_gpu_count:
                raise PermissionError(
                    "GPU request exceeds the locked task resource limit"
                )
            allowed_architectures = set(
                task.target_hardware.gpu_architectures
            )
            if (
                execution.gpu.architecture
                and allowed_architectures
                and execution.gpu.architecture.casefold()
                not in allowed_architectures
            ):
                raise PermissionError(
                    "GPU architecture is outside the locked target hardware"
                )
        return coordinator.submit(execution)

    return handle


def review_request_handler(
    assurance: AssuranceStore,
    *,
    schedule: Callable[[ReviewPacket], None],
) -> ActionHandler:
    """Create a handler that freezes input before reviewer fan-out."""

    def handle(
        task: IssueTask,
        action: ActionRequest,
        context: PolicyContext,
    ) -> ReviewPacket:
        packet = ReviewPacket.model_validate(action.payload)
        packet_payload = packet.model_dump(
            mode="json",
            exclude_computed_fields=True,
        )
        if redact_data(packet_payload) != packet_payload:
            raise ValueError(
                "review packet contains credential-shaped data"
            )
        if packet.task_id != task.task_id:
            raise ValueError("review packet belongs to another task")
        if packet.repository != action.repository:
            raise ValueError("review repository differs from the action")
        if packet.goal_revision != task.revision:
            raise ValueError("review packet uses a stale goal revision")
        if packet.non_goals != task.non_goals:
            raise ValueError("review packet changed the locked non-goals")
        if packet.must_preserve != task.must_preserve:
            raise ValueError(
                "review packet changed the must-preserve behavior"
            )
        if packet.target_hardware != task.target_hardware.model_dump(
            mode="json"
        ):
            raise ValueError(
                "review packet changed the locked target hardware"
            )
        matrix = assurance.get_completion_matrix(task.task_id)
        if packet.completion_matrix_digest != matrix.fingerprint():
            raise ValueError("review packet has a stale completion matrix")
        expected_repositories = {goal.repository for goal in task.goals}
        if set(packet.repository_inputs) != expected_repositories:
            raise ValueError(
                "review packet must include every locked task repository"
            )
        required_evidence_ids = {
            evidence_id
            for _, _, entry in matrix.evidence_bindings()
            for evidence_id in entry.evidence_ids
        }
        if not required_evidence_ids.issubset(packet.evidence_ids):
            raise ValueError(
                "review packet omits completion evidence from the matrix"
            )
        for evidence_id in packet.evidence_ids:
            evidence = assurance.get_evidence(evidence_id)
            if (
                evidence.task_id != task.task_id
                or evidence.code_diff_sha != packet.code_diff_sha
                or evidence.goal_revision != packet.goal_revision
            ):
                raise ValueError(
                    f"review packet contains stale evidence: {evidence_id}"
                )
        for repository, review_input in packet.repository_inputs.items():
            root = context.worktree_roots.get(repository)
            if root is None:
                root = context.repository_roots.get(repository)
            if root is None:
                raise PermissionError(
                    "review packet has no controller-owned repository root "
                    f"for {repository}"
                )
            if (
                Path(review_input.absolute_repository_path).resolve()
                != root.resolve()
            ):
                raise PermissionError(
                    "review packet path differs from the controller-owned "
                    f"root for {repository}"
                )
        stored = assurance.put_review_packet(packet)
        schedule(stored)
        return stored

    return handle
