from __future__ import annotations

from pathlib import Path

from project_hermes.assurance import (
    CompletionEntry,
    CompletionLayer,
    CompletionStatus,
    EvidenceRecord,
    ReviewProgress,
    ReviewRecord,
    ReviewPacket,
    ReviewRole,
    ReviewVerdict,
)
from project_hermes.assurance_store import SqliteAssuranceStore
from project_hermes.controller import ProjectHermesController
from project_hermes.execution import (
    BackendExecution,
    ExecutionCoordinator,
    ExecutionObservation,
    ExecutionRequest,
    ExecutionStatus,
    SqliteExecutionStore,
)
from project_hermes.models import IssueTask, ProjectRole
from project_hermes.resource_managers import GpuPool
from project_hermes.resources import (
    ArtifactKind,
    ArtifactManifest,
    ExecutionEnvironment,
    GpuRequest,
)
from project_hermes.store import SqliteRunStore
from project_hermes.work_graph import (
    ActionKind,
    ActionRequest,
    LifecycleStatus,
    WorkNode,
    WorkNodeKind,
)


def test_dynamic_task_flow_reaches_independent_candidate_gate(
    tmp_path: Path,
    issue_task: IssueTask,
) -> None:
    runs = SqliteRunStore(tmp_path / "runs.db")
    assurance = SqliteAssuranceStore(tmp_path / "assurance.db")
    controller = ProjectHermesController(runs, assurance)
    controller.create_task_run(issue_task, run_id="run-1")
    node = WorkNode(
        node_id="implementation",
        run_id="run-1",
        kind=WorkNodeKind.AGENT_INVOCATION,
        capability="implement",
        requested_role=ProjectRole.CODEX,
    )
    decision, _ = controller.submit_action(
        "run-1",
        ActionRequest(
            request_id="create-implementation",
            task_id="task-1",
            requested_by=ProjectRole.PROJECT_HERMES,
            action=ActionKind.CREATE_NODE,
            payload=node.model_dump(mode="json"),
        ),
    )
    assert decision.allowed

    runs.queue_node("run-1", node.node_id)
    claimed = runs.claim_ready_node(
        "run-1",
        owner_token="codex-session",
        role=ProjectRole.CODEX,
        lease_seconds=30,
    )
    assert claimed is not None
    runs.transition_node(
        "run-1",
        node.node_id,
        LifecycleStatus.RUNNING,
        owner_token="codex-session",
    )
    runs.transition_node(
        "run-1",
        node.node_id,
        LifecycleStatus.COMPLETED,
        owner_token="codex-session",
    )

    matrix = assurance.get_completion_matrix("task-1")
    completion_evidence_ids = []
    for layer in matrix.goal_paths["correctness"].required_layers:
        evidence_id = f"evidence-{layer.value}"
        completion_evidence_ids.append(evidence_id)
        assurance.put_evidence(
            EvidenceRecord(
                evidence_id=evidence_id,
                task_id="task-1",
                node_id="implementation",
                path_id="correctness",
                repository="acme/kernel",
                completion_layer=layer,
                producer_role=ProjectRole.RUNNER,
                base_sha="0123456789abcdef",
                code_diff_sha="abcdef1234",
                goal_revision=0,
                environment={"runner": "fixture"},
                command=["python", "-m", "pytest"],
                result={"exit_code": 0},
            )
        )
        matrix.update(
            CompletionEntry(
                layer=layer,
                status=CompletionStatus.SATISFIED,
                evidence_ids=[evidence_id],
                code_diff_sha="abcdef1234",
                goal_revision=0,
            ),
            path_id="correctness",
        )
    assurance.put_completion_matrix(matrix)
    packet = assurance.put_review_packet(
        ReviewPacket(
            packet_id="review-packet-1",
            task_id="task-1",
            repository="acme/kernel",
            absolute_repository_path=str(tmp_path),
            base_sha="0123456789abcdef",
            branch="project-hermes/task-1",
            code_diff_sha="abcdef1234",
            goal_revision=0,
            complete_diff_ref="artifact://diff/abcdef1234",
            includes_untracked=True,
            must_preserve=["Existing non-target behavior."],
            non_goals=["Support for unrelated hardware."],
            completion_matrix_digest=matrix.fingerprint(),
            target_hardware={"architecture": "gfx942"},
            evidence_ids=completion_evidence_ids,
        )
    )
    for role, session in (
        (ReviewRole.COMPLETION_AUDITOR, "audit-session"),
        (ReviewRole.MINIMAL_DIFF_REVIEWER, "diff-session"),
    ):
        review_evidence_id = f"evidence-{role.value}"
        assurance.put_evidence(
            EvidenceRecord(
                evidence_id=review_evidence_id,
                task_id="task-1",
                node_id=f"review-{role.value}",
                path_id="correctness",
                repository="acme/kernel",
                completion_layer=CompletionLayer.CI_REVIEWED,
                producer_role=ProjectRole.REVIEWER,
                base_sha="0123456789abcdef",
                code_diff_sha="abcdef1234",
                goal_revision=0,
                environment={"reviewer": role.value},
                command=["review", role.value],
                result={"verdict": "APPROVE"},
            )
        )
        assurance.add_review(
            ReviewRecord(
                review_id=f"review-{role.value}",
                task_id="task-1",
                role=role,
                review_cycle=1,
                reviewer_session_id=session,
                packet_id=packet.packet_id,
                packet_digest=packet.content_hash,
                code_diff_sha="abcdef1234",
                goal_revision=0,
                verdict=ReviewVerdict.APPROVE,
                evidence_ids=[review_evidence_id],
                summary="The candidate passes this independent gate.",
                progress=(
                    ReviewProgress.IMPROVED
                    if role is ReviewRole.COMPLETION_AUDITOR
                    else None
                ),
                progress_explanation=(
                    "The diff satisfies the frozen correctness goal."
                    if role is ReviewRole.COMPLETION_AUDITOR
                    else None
                ),
                assessed_goal_path_ids=(
                    ["correctness"]
                    if role is ReviewRole.COMPLETION_AUDITOR
                    else []
                ),
            )
        )

    gate = controller.candidate_gate(
        "task-1",
        code_diff_sha="abcdef1234",
        goal_revision=0,
        repository_bases={"acme/kernel": "0123456789abcdef"},
        implementer_session_id="codex-session",
    )
    assert gate.approved
    assert gate.repository_diff_shas == {
        "acme/kernel": "abcdef1234"
    }
    assert runs.get_run("run-1").completed_at is not None
    assert [event.sequence for event in runs.list_events("run-1")] == list(
        range(1, 10)
    )


def test_execution_waits_for_artifact_before_allocating_gpu(
    tmp_path: Path,
) -> None:
    manifests: dict[str, ArtifactManifest] = {}

    class Backend:
        name = "fixture"

        def start(
            self,
            execution_id: str,
            request: ExecutionRequest,
            *,
            gpu_ids: tuple[str, ...],
            artifacts: tuple[ArtifactManifest, ...],
        ) -> BackendExecution:
            assert gpu_ids == ("gpu-0",)
            assert [item.request_id for item in artifacts] == ["model-1"]
            return BackendExecution(
                execution_id=execution_id,
                backend=self.name,
                native_id="job-1",
            )

        def observe(
            self,
            execution: BackendExecution,
        ) -> ExecutionObservation:
            return ExecutionObservation(
                execution_id=execution.execution_id,
                status=ExecutionStatus.SUCCEEDED,
                terminated=True,
                exit_code=0,
            )

        def cancel(self, execution: BackendExecution) -> None:
            del execution

    pool = GpuPool(["gpu-0"])
    coordinator = ExecutionCoordinator(
        SqliteExecutionStore(tmp_path / "execution.db"),
        Backend(),
        artifact_lookup=manifests.get,
        gpu_pool=pool,
    )
    image_digest = "sha256:" + "a" * 64
    coordinator_record = coordinator.submit(
        ExecutionRequest(
            request_id="execution-request-1",
            task_id="task-1",
            node_id="validate",
            repository="acme/kernel",
            workspace_lease_id="workspace-1",
            candidate_digest="b" * 64,
            command=["python", "-m", "pytest"],
            environment=ExecutionEnvironment.GPU_CLUSTER,
            image_digest=image_digest,
            artifact_request_ids=["model-1"],
            gpu=GpuRequest(
                request_id="gpu-request-1",
                task_id="task-1",
                count=1,
                image_digest=image_digest,
            ),
            idempotency_key="validate-candidate-b",
        )
    )
    assert coordinator_record.status is ExecutionStatus.WAITING_ARTIFACT
    assert pool.available_ids() == ("gpu-0",)

    manifests["model-1"] = ArtifactManifest(
        request_id="model-1",
        task_id="task-1",
        kind=ArtifactKind.MODEL,
        source_uri="controller://models/example",
        resolved_digest="sha256:" + "c" * 64,
        local_path="/models/example",
        byte_size=1,
    )
    coordinator_record = coordinator.reconcile(
        coordinator_record.execution_id
    )
    assert coordinator_record.status is ExecutionStatus.RUNNING
    assert pool.available_ids() == ()

    coordinator_record = coordinator.reconcile(
        coordinator_record.execution_id
    )
    assert coordinator_record.status is ExecutionStatus.SUCCEEDED
    assert pool.available_ids() == ("gpu-0",)
