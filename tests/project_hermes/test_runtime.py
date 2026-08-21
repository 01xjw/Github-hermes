from __future__ import annotations

from pathlib import Path

from project_hermes.models import ProjectRole
from project_hermes.runtime.base import RuntimeRequest, RuntimeStatus
from project_hermes.runtime.codex_daemon import (
    OpenAICodexDriver,
    TaskCodexDaemon,
)
from project_hermes.runtime.codex_protocol import (
    CodexCommand,
    CodexCommandKind,
    CodexTaskSpec,
)
from project_hermes.runtime.hermes import HermesRuntimeAdapter


class FakeHermesAgent:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.closed = False

    def run_conversation(self, prompt: str, *, task_id: str) -> dict:
        self.prompts.append(prompt)
        return {
            "final_response": f"{task_id}: {prompt}",
            "tool_calls": [],
        }

    def close(self) -> None:
        self.closed = True


class FakeCodexDriver:
    def __init__(
        self,
        spec: CodexTaskSpec,
        *,
        resume_thread_id: str | None = None,
    ) -> None:
        del spec
        self.thread_id = resume_thread_id or "thread-new"
        self.closed = False

    def run_turn(self, prompt: str) -> dict:
        return {
            "status": "completed",
            "thread_id": self.thread_id,
            "turn_id": f"turn-{prompt}",
            "items": [{"type": "message", "text": prompt}],
            "final_response": prompt.upper(),
        }

    def cancel(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


def _command(kind: CodexCommandKind, **kwargs) -> bytes:
    command = CodexCommand(
        request_id=f"request-{kind.value}",
        command=kind,
        **kwargs,
    )
    return command.model_dump_json().encode("utf-8")


def test_hermes_adapter_preserves_long_lived_agent_identity() -> None:
    agents: list[FakeHermesAgent] = []

    def factory(request: RuntimeRequest) -> FakeHermesAgent:
        del request
        agent = FakeHermesAgent()
        agents.append(agent)
        return agent

    adapter = HermesRuntimeAdapter(
        factory,
        attest_agent=lambda agent, request: None,
    )
    handle = adapter.start(
        RuntimeRequest(
            request_id="request-1",
            task_id="task-1",
            role=ProjectRole.PROJECT_HERMES,
            prompt="Inspect the issue.",
            session_id="session-1",
        )
    )
    handle = adapter.resume(
        handle,
        RuntimeRequest(
            request_id="request-2",
            task_id="task-1",
            role=ProjectRole.PROJECT_HERMES,
            prompt="Plan the next evidence-backed action.",
        ),
    )

    assert handle.status is RuntimeStatus.IDLE
    assert len(agents) == 1
    assert agents[0].prompts == [
        "Inspect the issue.",
        "Plan the next evidence-backed action.",
    ]
    assert adapter.result(handle).final_response.startswith("task-1:")
    assert [event.sequence for event in adapter.events(handle)] == [1, 2, 3, 4]

    adapter.close(handle)
    assert agents[0].closed


def test_hermes_reconnect_continues_durable_event_sequence() -> None:
    first = HermesRuntimeAdapter(
        lambda _request: FakeHermesAgent(),
        attest_agent=lambda _agent, _request: None,
    )
    handle = first.start(
        RuntimeRequest(
            request_id="request-first",
            task_id="project-hermes-manager",
            role=ProjectRole.PROJECT_HERMES,
            prompt="Start project management.",
            session_id="project-manager-session",
        )
    )
    assert [event.sequence for event in first.events(handle)] == [1, 2]

    restarted = HermesRuntimeAdapter(
        lambda _request: FakeHermesAgent(),
        attest_agent=lambda _agent, _request: None,
        agent_restorer=lambda _handle, _request: FakeHermesAgent(),
    )
    request = RuntimeRequest(
        request_id="request-resume",
        task_id="project-hermes-manager",
        role=ProjectRole.PROJECT_HERMES,
        prompt="Continue project management.",
        session_id="project-manager-session",
        metadata={"after_runtime_event_sequence": 2},
    )
    connected = restarted.reconnect(handle, request)
    resumed = restarted.resume(connected, request)

    assert [
        event.sequence
        for event in restarted.events(resumed, after_sequence=2)
    ] == [3, 4, 5]


def test_codex_daemon_persists_thread_events_and_latest_result(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    spec = CodexTaskSpec(
        task_id="task-1",
        session_id="codex-session-1",
        worktree_path=worktree,
        runtime_root=tmp_path / "runtime",
        reasoning_effort="xhigh",
        context_window=1_000_000,
    )
    daemon = TaskCodexDaemon(spec, driver_factory=FakeCodexDriver)
    native_driver = object.__new__(OpenAICodexDriver)
    native_driver._spec = spec
    native_driver._render_native_config()

    turn = daemon.handle_payload(
        _command(CodexCommandKind.RUN_TURN, prompt="implement")
    )
    result = daemon.handle_payload(_command(CodexCommandKind.RESULT))
    events = daemon.handle_payload(_command(CodexCommandKind.EVENTS))

    assert turn.ok
    assert turn.payload["final_response"] == "IMPLEMENT"
    assert result.payload == turn.payload
    assert [
        event["event_type"] for event in events.payload["events"]
    ] == ["thread.started", "turn.started", "turn.completed"]

    resumed = TaskCodexDaemon(spec, driver_factory=FakeCodexDriver)
    second_turn = resumed.handle_payload(
        _command(CodexCommandKind.RUN_TURN, prompt="verify")
    )
    status = resumed.handle_payload(_command(CodexCommandKind.STATUS))
    assert second_turn.ok
    assert status.payload["root_thread_id"] == "thread-new"
    native_config = (spec.codex_home() / "config.toml").read_text(
        encoding="utf-8"
    )
    assert 'model_reasoning_effort = "xhigh"' in native_config
    assert "model_context_window = 1000000" in native_config
    assert spec.metadata_path().stat().st_mode & 0o777 == 0o600
    assert spec.result_path().stat().st_mode & 0o777 == 0o600
