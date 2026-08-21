from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from project_hermes.config import (
    IssueRelevancePolicy,
    PollingConfig,
)
from project_hermes.issue_relevance import issue_relevance_reasons
from project_hermes.polling import IssuePollingService
from project_hermes.polling_store import (
    PollingCandidate,
    PollingRepository,
    PollingRun,
    PollingRunStatus,
    SqlitePollingStore,
    WorkStatus,
)
from project_hermes.repository_skills import (
    RepositorySkillRegistry,
    repository_skill_digest,
)


NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("H20 decode produces invalid samples", "Reproduces on the H20."),
        ("NVIDIA backend crash", "The CUDA worker exits during decode."),
        ("Portable API", "This fails only when using the XPU backend."),
    ],
)
def test_non_amd_hardware_specific_issues_are_excluded(
    title: str,
    body: str,
) -> None:
    reasons = issue_relevance_reasons(
        policy=IssueRelevancePolicy.AMD_OR_PORTABLE,
        title=title,
        body=body,
        labels=[],
    )

    assert reasons
    assert "without an AMD/ROCm signal" in reasons[0]


def test_explicit_amd_signal_keeps_cross_vendor_issue_eligible() -> None:
    assert issue_relevance_reasons(
        policy=IssueRelevancePolicy.AMD_OR_PORTABLE,
        title="ROCm parity for the H20 decode regression",
        body="Implement and validate the equivalent path on gfx1100.",
        labels=["module: rocm"],
    ) == []


def test_hardware_neutral_issue_survives_cuda_environment_dump() -> None:
    assert issue_relevance_reasons(
        policy=IssueRelevancePolicy.AMD_OR_PORTABLE,
        title="Optimizer state_dict loses a parameter group",
        body=(
            "Loading a saved optimizer changes the group count on CPU too.\n"
            "PyTorch build metadata: CUDA 12.8; CUDA available: true.\n"
            "No accelerator is required by the reproducer."
        ),
        labels=["module: optimizer", "triaged"],
    ) == []


@pytest.mark.parametrize(
    ("title", "body"),
    [
        (
            "CUDA initialization occurs in a CPU-only torch.cond graph",
            "The self-contained CPU-only reproducer fails before any GPU op.",
        ),
        (
            "XPU environment report accompanies an optimizer regression",
            "The wrong result reproduces on CPU with the attached script.",
        ),
        (
            "H100 was used by the reporter",
            "This is a hardware-agnostic compiler rewrite and requires no accelerator.",
        ),
        (
            "Reduction gives the wrong result on CUDA",
            "The same wrong result occurs on both CPU and CUDA.",
        ),
    ],
)
def test_explicit_cpu_or_general_path_wins_over_non_amd_words(
    title: str,
    body: str,
) -> None:
    assert issue_relevance_reasons(
        policy=IssueRelevancePolicy.AMD_OR_PORTABLE,
        title=title,
        body=body,
        labels=["module: cuda"],
    ) == []


def test_reported_gpu_model_without_requirement_is_not_an_exclusion() -> None:
    assert issue_relevance_reasons(
        policy=IssueRelevancePolicy.AMD_OR_PORTABLE,
        title="Chained normalization produces incorrect gradients",
        body="Reporter environment: H100. The operator path is generic Triton.",
        labels=["module: inductor"],
    ) == []


def test_cpu_success_does_not_override_an_explicit_cuda_only_failure() -> None:
    reasons = issue_relevance_reasons(
        policy=IssueRelevancePolicy.AMD_OR_PORTABLE,
        title="CUDA-only fused kernel crash",
        body="CPU works correctly; this failure reproduces only on CUDA.",
        labels=["module: cuda"],
    )

    assert reasons
    assert "cuda" in reasons[0]


def test_vendor_ownership_label_alone_is_not_a_hardware_requirement() -> None:
    assert issue_relevance_reasons(
        policy=IssueRelevancePolicy.AMD_OR_PORTABLE,
        title="Sidecar broken pipes should include diagnostic statistics",
        body="Both exit-detection paths should use the same logging helper.",
        labels=["module: cuda", "triaged"],
    ) == []


def test_amd_native_repository_does_not_reject_vendor_comparison() -> None:
    assert issue_relevance_reasons(
        policy=IssueRelevancePolicy.AMD_NATIVE,
        title="Compare H20 and gfx1100 dispatch metadata",
        body="The ROCm-owned implementation needs an interoperability fixture.",
        labels=[],
    ) == []


def test_startup_reconciliation_blocks_old_h20_pending_work(
    tmp_path: Path,
) -> None:
    store = SqlitePollingStore(tmp_path / "polling.db")
    store.configure_task(enabled=True, interval_seconds=900, now=NOW)
    run = PollingRun(
        run_id="poll-old",
        status=PollingRunStatus.RUNNING,
        cutoff=NOW,
        repositories_requested=1,
        started_at=NOW,
    )
    store.begin_run(run)
    store.upsert_repository(
        PollingRepository(
            repository_id=101,
            repository="pytorch/pytorch",
            default_branch="main",
            html_url="https://github.com/pytorch/pytorch",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    candidate = PollingCandidate(
        candidate_id="candidate-old-h20",
        repository_id=101,
        repository="pytorch/pytorch",
        issue_number=99,
        issue_url="https://github.com/pytorch/pytorch/issues/99",
        title="H20 attention kernel crashes",
        body="The issue reproduces only on NVIDIA H20.",
        labels=["module: cuda"],
        author="reporter",
        comments=1,
        evidence_score=5,
        eligible=True,
        created_at=NOW,
        updated_at=NOW,
        first_seen_at=NOW,
        last_seen_at=NOW,
        latest_run_id=run.run_id,
    )
    _stored, queued = store.save_candidate(candidate)
    assert queued
    config = PollingConfig(
        sqlite_path=store.path,
        repositories=({"repository": "pytorch/pytorch"},),
    )
    service = IssuePollingService(object(), store, config)  # type: ignore[arg-type]

    service._reconcile_pending_relevance(now=NOW)

    stored = store.get_candidate(candidate.candidate_id)
    work = store.list_work_items()[0]
    assert stored.eligible is False
    assert work.status is WorkStatus.BLOCKED
    assert "H20" in " ".join(stored.filter_reasons).upper()


def _write_skill(root: Path, name: str) -> Path:
    skill = root / name
    (skill / "agents").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Solve fixture Issues.\n---\n\n"
        "# Fixture Skill\n\nFollow the repository contract.\n",
        encoding="utf-8",
    )
    (skill / "agents" / "openai.yaml").write_text(
        "interface:\n"
        '  display_name: "Fixture Skill"\n'
        '  short_description: "Resolve fixture repository Issues safely"\n'
        f'  default_prompt: "Use ${name} for this Issue."\n',
        encoding="utf-8",
    )
    return skill


def test_repository_skill_registry_locks_name_and_content_digest(
    tmp_path: Path,
) -> None:
    skills = tmp_path / "skills"
    skill = _write_skill(skills, "solve-acme-alpha")
    config = PollingConfig(
        repository_skills_path=skills,
        require_repository_skills=True,
        repositories=(
            {
                "repository": "acme/alpha",
                "skill_name": "solve-acme-alpha",
            },
        ),
    )

    reference = RepositorySkillRegistry(config).validate_all()[0]

    assert reference.repository == "acme/alpha"
    assert reference.name == "solve-acme-alpha"
    assert reference.digest == repository_skill_digest(skill.resolve())


def test_repository_skill_registry_rejects_symlinked_content(
    tmp_path: Path,
) -> None:
    skills = tmp_path / "skills"
    skill = _write_skill(skills, "solve-acme-alpha")
    external = tmp_path / "external.txt"
    external.write_text("outside", encoding="utf-8")
    (skill / "unsafe.txt").symlink_to(external)
    config = PollingConfig(
        repository_skills_path=skills,
        require_repository_skills=True,
        repositories=(
            {
                "repository": "acme/alpha",
                "skill_name": "solve-acme-alpha",
            },
        ),
    )

    with pytest.raises(ValueError, match="symbolic links"):
        RepositorySkillRegistry(config).validate_all()
