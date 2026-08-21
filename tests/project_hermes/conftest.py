from __future__ import annotations

import pytest

from project_hermes.models import (
    GoalPath,
    IssueTask,
    PermissionBudget,
    RepositoryResponsibility,
    ResourceLimits,
    TargetHardware,
    TriageDecision,
)


@pytest.fixture
def issue_task() -> IssueTask:
    return IssueTask(
        task_id="task-1",
        issue_urls=["https://github.com/acme/kernel/issues/17"],
        named_baseline="main@0123456789abcdef",
        goals=[
            GoalPath(
                path_id="correctness",
                description="Repair the kernel result.",
                repository="acme/kernel",
                acceptance_criteria=["The regression test passes."],
                required_completion_layers=[
                    "implemented",
                    "gate_verified",
                    "ci_reviewed",
                ],
            )
        ],
        repositories=[
            RepositoryResponsibility(
                repository="acme/kernel",
                responsibilities=["Own the kernel implementation."],
            )
        ],
        target_hardware=TargetHardware(
            gpu_architectures=["gfx942"],
            minimum_gpu_count=1,
            environment_name="ci-gpu",
        ),
        permissions=PermissionBudget(
            readable_repositories=["acme/kernel"],
            writable_repositories=["acme/kernel"],
            may_request_execution=True,
        ),
        resource_limits=ResourceLimits(
            max_parallel_nodes=4,
            max_gpu_count=2,
            max_model_concurrency=3,
        ),
        triage_decision=TriageDecision.APPROVE,
        triage_evidence_refs=["evidence-triage"],
    )
