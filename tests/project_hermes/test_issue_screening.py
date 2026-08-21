from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from project_hermes.config import ProjectHermesConfig
from project_hermes.issue_screening import IssueScreeningService
from project_hermes.polling_store import (
    MachineCompatibility,
    PollingCandidate,
    PollingRepository,
    PollingRun,
    PollingRunStatus,
    ScreeningDecision,
    SqlitePollingStore,
)
from project_hermes.runtime.base import RuntimeRequest


NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)


class _Session:
    def __init__(self, request: RuntimeRequest, response: dict[str, Any]) -> None:
        self.session_id = request.session_id or "missing"
        self.response = response
        self.closed = False

    def run_runtime_turn(self, request: RuntimeRequest) -> dict[str, Any]:
        assert request.session_id == self.session_id
        return {"final_response": json.dumps(self.response)}

    def close(self) -> None:
        self.closed = True


class _Factory:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.requests: list[RuntimeRequest] = []
        self.sessions: list[_Session] = []

    def create_screening(self, request: RuntimeRequest) -> _Session:
        self.requests.append(request)
        session = _Session(request, self.response)
        self.sessions.append(session)
        return session


class _SequencedFactory:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[RuntimeRequest] = []
        self.sessions: list[_Session] = []

    def create_screening(self, request: RuntimeRequest) -> _Session:
        self.requests.append(request)
        response = self.responses[len(self.sessions)]
        session = _Session(request, response)
        self.sessions.append(session)
        return session


def _profile(root: Path) -> Path:
    profile = root / "screening-profile"
    profile.mkdir()
    (profile / "SOUL.md").write_text(
        "# Generic soul\n\nJudge environment fit without repository exceptions.\n",
        encoding="utf-8",
    )
    (profile / "AGENT.md").write_text(
        "# Generic contract\n\nReturn every supplied candidate decision.\n",
        encoding="utf-8",
    )
    return profile


def _config(tmp_path: Path, profile: Path) -> ProjectHermesConfig:
    return ProjectHermesConfig(
        project_root=tmp_path,
        control_plane={
            "mode": "sqlite",
            "sqlite_path": tmp_path / "control-plane.db",
        },
        polling={
            "sqlite_path": tmp_path / "polling.db",
            "repositories": [{"repository": "acme/alpha"}],
            "minimum_evidence_score": 0,
            "require_operator_selection": True,
            "issue_screening_enabled": True,
            "issue_screening_profile_path": profile,
            "require_screening_select_for_operator": True,
        },
        codex={"enabled": False, "runtime_root": tmp_path / "runtime"},
    )


def _candidate(store: SqlitePollingStore) -> PollingCandidate:
    store.configure_task(enabled=True, interval_seconds=900, now=NOW)
    run = PollingRun(
        run_id="poll-screening",
        status=PollingRunStatus.RUNNING,
        cutoff=NOW,
        repositories_requested=1,
        started_at=NOW,
    )
    store.begin_run(run)
    store.upsert_repository(
        PollingRepository(
            repository_id=101,
            repository="acme/alpha",
            default_branch="main",
            html_url="https://github.com/acme/alpha",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    candidate = PollingCandidate(
        candidate_id="poll-candidate-screening",
        repository_id=101,
        repository="acme/alpha",
        issue_number=7,
        issue_url="https://github.com/acme/alpha/issues/7",
        title="CPU-only reproducer is incorrectly filtered",
        body="The attached CPU-only reproducer returns the wrong result.",
        labels=["module: cuda"],
        author="reporter",
        comments=2,
        evidence_score=5,
        eligible=False,
        filter_reasons=["mechanical vendor-word false positive"],
        created_at=NOW,
        updated_at=NOW,
        first_seen_at=NOW,
        last_seen_at=NOW,
        latest_run_id=run.run_id,
    )
    store.save_candidate(candidate, enqueue_eligible=False)
    return candidate


def _select(candidate_id: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "decision": "SELECT",
        "machine_compatibility": "COMPATIBLE",
        "task_kind": "LOCAL_CODE_OR_TEST",
        "reason": "The report includes a self-contained CPU path.",
        "required_environment": {
            "operating_systems": ["linux"],
            "cpu_architectures": ["amd64"],
            "minimum_cpu_cores": 1,
            "minimum_memory_gib": 4,
            "gpu_count": 0,
            "gpu_architectures": [],
            "network_access_required": False,
            "external_system_write_required": False,
            "external_dependencies": [],
        },
        "evidence": ["Issue states that the CPU-only reproducer is failing."],
        "uncertainties": [],
    }


def _defer(candidate_id: str) -> dict[str, Any]:
    decision = _select(candidate_id)
    decision.update({
        "decision": "DEFER",
        "machine_compatibility": "NEEDS_PROBE",
        "reason": "The worker image capability needs one bounded probe.",
        "uncertainties": [
            "Run importlib.util.find_spec for the named runtime; the "
            "guided-training operator will submit the result to re-screen."
        ],
    })
    decision["required_environment"]["external_dependencies"] = ["named runtime"]
    return decision


def test_screener_persists_complete_decision_and_can_override_mechanical_filter(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    config = _config(tmp_path, profile)
    store = SqlitePollingStore(config.polling.sqlite_path)
    candidate = _candidate(store)
    factory = _Factory({"decisions": [_select(candidate.candidate_id)]})

    result = IssueScreeningService(config, store, factory).screen_next()

    assert result is not None
    assert result.decisions == {candidate.candidate_id: ScreeningDecision.SELECT}
    stored = store.get_current_screening(candidate.candidate_id)
    assert stored is not None
    assert stored.reason == "The report includes a self-contained CPU path."
    assert stored.profile_digest == result.profile_digest
    assert store.screening_counts() == {
        "SELECT": 1,
        "DEFER": 0,
        "REJECT": 0,
        "PENDING": 0,
    }
    assert factory.sessions[0].closed is True
    assert factory.requests[0].role.value == "screener"
    assert "BEGIN_SOUL_MD" in factory.requests[0].prompt

    work = store.select_candidate_for_work(
        candidate.candidate_id,
        selected_by="operator",
        max_pending_work_items=1,
        require_screening_select=True,
        now=NOW,
    )
    assert work.candidate_id == candidate.candidate_id


def test_screening_gate_rejects_pending_or_non_select_decision(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    config = _config(tmp_path, profile)
    store = SqlitePollingStore(config.polling.sqlite_path)
    candidate = _candidate(store)

    with pytest.raises(ValueError, match="Subagent SELECT"):
        store.select_candidate_for_work(
            candidate.candidate_id,
            selected_by="operator",
            max_pending_work_items=1,
            require_screening_select=True,
            now=NOW,
        )


def test_screener_requires_exact_complete_ordered_batch(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    config = _config(tmp_path, profile)
    store = SqlitePollingStore(config.polling.sqlite_path)
    _candidate(store)
    factory = _Factory({"decisions": [_select("unexpected-candidate")]})

    with pytest.raises(ValueError, match="candidate_coverage"):
        IssueScreeningService(config, store, factory).screen_next()


def test_screener_retries_invalid_batch_in_fresh_session_and_persists_only_valid(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    config = _config(tmp_path, profile)
    store = SqlitePollingStore(config.polling.sqlite_path)
    candidate = _candidate(store)
    invalid = _select(candidate.candidate_id)
    invalid["uncertainties"] = [
        {"probe": "This raw invalid object must not be echoed to the retry."}
    ]
    factory = _SequencedFactory(
        [
            {"decisions": [invalid]},
            {"decisions": [_select(candidate.candidate_id)]},
        ]
    )

    result = IssueScreeningService(config, store, factory).screen_next()

    assert result is not None
    assert result.session_id == factory.sessions[1].session_id
    assert len(factory.sessions) == 2
    assert all(session.closed for session in factory.sessions)
    assert factory.sessions[0].session_id != factory.sessions[1].session_id
    assert "SCREENING_PROTOCOL_FEEDBACK" not in factory.requests[0].prompt
    retry_prompt = factory.requests[1].prompt
    assert "SCREENING_PROTOCOL_FEEDBACK" in retry_prompt
    assert '"path":"decisions.0.uncertainties.0"' in retry_prompt
    assert '"error_type":"string_type"' in retry_prompt
    assert "This raw invalid object" not in retry_prompt
    assert factory.requests[0].context["screening_protocol_attempt"] == 1
    assert factory.requests[1].context["screening_protocol_attempt"] == 2
    history = store.list_current_screening_history(candidate.candidate_id)
    assert len(history) == 1
    assert history[0].session_id == factory.sessions[1].session_id
    assert history[0].decision is ScreeningDecision.SELECT


def test_screener_protocol_retry_exhaustion_is_fail_closed(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    config = _config(tmp_path, profile)
    config = config.model_copy(
        update={
            "polling": config.polling.model_copy(
                update={"issue_screening_protocol_retries": 1}
            )
        }
    )
    store = SqlitePollingStore(config.polling.sqlite_path)
    candidate = _candidate(store)
    invalid = _select(candidate.candidate_id)
    invalid["uncertainties"] = [{"probe": "still invalid"}]
    factory = _SequencedFactory(
        [{"decisions": [invalid]}, {"decisions": [invalid]}]
    )

    with pytest.raises(ValueError, match="exhausted 2 protocol attempt"):
        IssueScreeningService(config, store, factory).screen_next()

    assert len(factory.sessions) == 2
    assert all(session.closed for session in factory.sessions)
    assert factory.sessions[0].session_id != factory.sessions[1].session_id
    assert store.get_current_screening(candidate.candidate_id) is None
    assert store.screening_counts()["PENDING"] == 1


def test_controller_rejects_select_outside_machine_gpu_architecture(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    config = _config(tmp_path, profile)
    store = SqlitePollingStore(config.polling.sqlite_path)
    candidate = _candidate(store)
    decision = _select(candidate.candidate_id)
    decision["required_environment"] = {
        **decision["required_environment"],
        "gpu_count": 1,
        "gpu_architectures": ["h100"],
    }
    factory = _Factory({"decisions": [decision]})

    with pytest.raises(ValueError, match="machine_envelope_violation"):
        IssueScreeningService(config, store, factory).screen_next()


def test_rescreen_appends_revision_and_preserves_initial_decision(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    config = _config(tmp_path, profile)
    store = SqlitePollingStore(config.polling.sqlite_path)
    candidate = _candidate(store)
    factory = _Factory({"decisions": [_defer(candidate.candidate_id)]})
    service = IssueScreeningService(config, store, factory)

    service.screen_next()
    initial = store.get_current_screening(candidate.candidate_id)
    assert initial is not None
    assert initial.revision == 1
    assert initial.decision is ScreeningDecision.DEFER

    factory.response = {"decisions": [_select(candidate.candidate_id)]}
    result = service.rescreen_candidate(
        candidate.candidate_id,
        requested_by="dashboard-operator",
        reason="A bounded worker-image probe resolved the only uncertainty.",
        probe_evidence=[
            "On the production AMD worker, the named runtime import succeeded."
        ],
    )

    assert result.decisions == {candidate.candidate_id: ScreeningDecision.SELECT}
    current = store.get_current_screening(candidate.candidate_id)
    history = store.list_current_screening_history(candidate.candidate_id)
    assert current is not None
    assert current.revision == 2
    assert current.screening_trigger == "operator_rescreen"
    assert current.requested_by == "dashboard-operator"
    assert current.probe_evidence == [
        "On the production AMD worker, the named runtime import succeeded."
    ]
    assert [item.revision for item in history] == [1, 2]
    assert history[0] == initial
    assert history[0].decision is ScreeningDecision.DEFER
    assert history[1] == current
    assert factory.requests[-1].context["screening_request_kind"] == (
        "operator_rescreen"
    )
    assert "bounded_probe_evidence" in factory.requests[-1].prompt

    work = store.select_candidate_for_work(
        candidate.candidate_id,
        selected_by="dashboard-operator",
        max_pending_work_items=1,
        require_screening_select=True,
        now=NOW,
    )
    assert work.candidate_id == candidate.candidate_id

    with pytest.raises(ValueError, match="after Work was created"):
        service.rescreen_candidate(
            candidate.candidate_id,
            requested_by="dashboard-operator",
            reason="This must not rewrite the admitted decision.",
            probe_evidence=["A later observation."],
        )

    with pytest.raises(ValueError, match="after Work was created"):
        store.save_screening_revision(
            current.model_copy(
                update={
                    "revision": 3,
                    "session_id": "issue-screening-race-loser",
                }
            )
        )


def test_controller_rejects_select_with_unresolved_external_dependency(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    config = _config(tmp_path, profile)
    store = SqlitePollingStore(config.polling.sqlite_path)
    candidate = _candidate(store)
    decision = _select(candidate.candidate_id)
    decision["required_environment"]["external_dependencies"] = [
        "model weights that are not in the repository"
    ]
    factory = _Factory({"decisions": [decision]})

    with pytest.raises(ValueError, match="machine_envelope_violation"):
        IssueScreeningService(config, store, factory).screen_next()


def test_operator_admission_rejects_legacy_select_with_open_dependency(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    config = _config(tmp_path, profile)
    store = SqlitePollingStore(config.polling.sqlite_path)
    candidate = _candidate(store)
    factory = _Factory({"decisions": [_defer(candidate.candidate_id)]})
    IssueScreeningService(config, store, factory).screen_next()
    initial = store.get_current_screening(candidate.candidate_id)
    assert initial is not None
    legacy_select = initial.model_copy(
        update={
            "decision": ScreeningDecision.SELECT,
            "machine_compatibility": MachineCompatibility.COMPATIBLE,
            "uncertainties": [],
            "revision": 2,
            "screening_trigger": "operator_rescreen",
            "requested_by": "legacy-import",
            "request_reason": "Represent an older unclosed SELECT record.",
            "probe_evidence": ["The dependency was not actually resolved."],
        }
    )
    store.save_screening_revision(legacy_select)

    assert store.operator_selection_state(
        candidate.candidate_id,
        require_screening_select=True,
    ) == {"selected": False, "selectable": False}
    with pytest.raises(ValueError, match="dependency-closed"):
        store.select_candidate_for_work(
            candidate.candidate_id,
            selected_by="dashboard-operator",
            max_pending_work_items=1,
            require_screening_select=True,
            now=NOW,
        )


def test_machine_envelope_exposes_proven_software_and_probe_consumer(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    config = _config(tmp_path, profile)
    config = config.model_copy(
        update={
            "polling": config.polling.model_copy(
                update={
                    "issue_screening_software_capabilities": (
                        "ROCm PyTorch; torch.cuda is the HIP compatibility namespace",
                        "pytest is installed",
                    ),
                    "issue_screening_unavailable_capabilities": (
                        "NVIDIA CUDA and nvcc",
                    ),
                    "issue_screening_validation_constraints": (
                        "CPU mocks cannot validate hardware behavior",
                    ),
                    "issue_screening_probe_consumer": (
                        "Authenticated operator submits bounded evidence through "
                        "the immutable re-screen action"
                    ),
                }
            )
        }
    )
    store = SqlitePollingStore(config.polling.sqlite_path)
    factory = _Factory({"decisions": [_select("unused")]})

    envelope = IssueScreeningService(config, store, factory).machine_envelope

    assert envelope.software_capabilities == [
        "ROCm PyTorch; torch.cuda is the HIP compatibility namespace",
        "pytest is installed",
    ]
    assert envelope.unavailable_capabilities == ["NVIDIA CUDA and nvcc"]
    assert envelope.validation_constraints == [
        "CPU mocks cannot validate hardware behavior"
    ]
    assert envelope.bounded_probe_consumer == (
        "Authenticated operator submits bounded evidence through the immutable "
        "re-screen action"
    )
