from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from agent import runtime_cwd
from project_hermes.config import ProjectHermesConfig
from project_hermes.credentials import (
    CONTROLLER_CREDENTIAL_SECRET_FILES,
    install_controller_credentials,
    read_credentials_environment,
)
from project_hermes.models import ProjectRole
from project_hermes.runtime.base import (
    RuntimeHandle,
    RuntimeModelRoute,
    RuntimeRequest,
    RuntimeStatus,
)
from project_hermes.runtime.hermes import HermesRuntimeAdapter
from project_hermes.runtime.hermes_agent import HermesAgentFactory


class _FakeSessionDB:
    def __init__(
        self,
        db_path: Path,
        *,
        sessions: dict[str, dict[str, Any]] | None = None,
        histories: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.db_path = db_path
        self.sessions = sessions or {}
        self.histories = histories or {}
        self.resume_safety_checks: list[str] = []
        self.reopened: list[str] = []
        self.history_calls: list[tuple[str, bool, bool]] = []
        self.closed = False

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self.sessions.get(session_id)

    def assert_resume_safe(self, session_id: str) -> int:
        self.resume_safety_checks.append(session_id)
        return len(self.histories.get(session_id, ()))

    def reopen_session(self, session_id: str) -> None:
        self.reopened.append(session_id)

    def get_messages_as_conversation(
        self,
        session_id: str,
        *,
        include_ancestors: bool,
        repair_alternation: bool,
    ) -> list[dict[str, Any]]:
        self.history_calls.append(
            (session_id, include_ancestors, repair_alternation)
        )
        return list(self.histories.get(session_id, ()))

    def close(self) -> None:
        self.closed = True


class _FakeAIAgent:
    def __init__(
        self,
        constructor_kwargs: dict[str, Any],
        *,
        results: list[dict[str, Any]] | None = None,
    ) -> None:
        self.constructor_kwargs = constructor_kwargs
        self.base_url = constructor_kwargs["base_url"]
        self.api_key = constructor_kwargs["api_key"]
        self.provider = constructor_kwargs["provider"]
        self.api_mode = constructor_kwargs["api_mode"]
        self.model = constructor_kwargs["model"]
        self.session_id = constructor_kwargs["session_id"]
        self._session_db = constructor_kwargs["session_db"]
        self._fallback_chain: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.valid_tool_names: set[str] = set()
        self.enabled_toolsets = constructor_kwargs["enabled_toolsets"]
        self.disabled_toolsets = constructor_kwargs["disabled_toolsets"]
        self._skip_mcp_refresh = False
        self._owns_session_db = False
        self._session_db_created = False
        self._kanban_worker_guidance = "unexpected"
        self.constructed_cwd = runtime_cwd.resolve_agent_cwd()
        self.results = list(results or [])
        self.turns: list[dict[str, Any]] = []
        self.closed = False
        self.interrupted = False

    def run_conversation(
        self,
        prompt: str,
        *,
        task_id: str,
        conversation_history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.turns.append(
            {
                "prompt": prompt,
                "task_id": task_id,
                "conversation_history": conversation_history,
                "cwd": runtime_cwd.resolve_agent_cwd(),
            }
        )
        if self.results:
            return self.results.pop(0)
        return {
            "final_response": "completed",
            "messages": [
                *(conversation_history or []),
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": "completed"},
            ],
            "failed": False,
            "interrupted": False,
        }

    def interrupt(self, *, hard_cancel: bool = False) -> None:
        self.interrupted = hard_cancel

    def close(self) -> None:
        self.closed = True


def _route(
    *,
    profile: str = "hermes-main",
    model: str = "main-model",
) -> dict[str, Any]:
    return {
        "profile": profile,
        "model": model,
        "model_provider": "digitalocean",
        "provider_endpoint": "https://models.example.test/v1",
        "provider_api_key_env": "MODEL_ACCESS_KEY",
        "provider_wire_api": "chat",
        "reasoning_mode": "provider_default",
        "reasoning_effort": None,
    }


def _config(
    tmp_path: Path,
    *,
    allowed_tools: tuple[str, ...] = (),
    reviewer_route: dict[str, Any] | None = None,
) -> ProjectHermesConfig:
    return ProjectHermesConfig(
        project_root=tmp_path,
        hermes={
            "session_db_path": tmp_path / "hermes-sessions.db",
            "allowed_tools": allowed_tools,
            "loop_interval_seconds": 2.5,
            "credentials_file": tmp_path / "credentials.yaml",
            "model_profile": _route(),
            "reviewer_profile": reviewer_route,
        },
    )


def _runtime_route(config: ProjectHermesConfig) -> RuntimeModelRoute:
    return RuntimeModelRoute.model_validate(
        config.hermes.model_profile.model_dump()
    )


def _request(
    config: ProjectHermesConfig,
    *,
    request_id: str = "request-1",
    prompt: str = "Inspect the Work queue.",
    session_id: str = "project-manager-session",
    cwd: Path | None = None,
) -> RuntimeRequest:
    return RuntimeRequest(
        request_id=request_id,
        task_id="project-hermes-manager",
        role=ProjectRole.PROJECT_HERMES,
        prompt=prompt,
        cwd=str(cwd or config.project_root),
        session_id=session_id,
        model_route=_runtime_route(config),
    )


def test_credentials_reader_enforces_private_regular_file_and_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text(
        "environment:\n"
        "  MODEL_ACCESS_KEY: secret-value\n"
        "  GITHUB_TOKEN: github-value\n",
        encoding="utf-8",
    )
    os.chmod(credentials, 0o600)

    assert read_credentials_environment(credentials) == {
        "MODEL_ACCESS_KEY": "secret-value",
        "GITHUB_TOKEN": "github-value",
    }

    os.chmod(credentials, 0o640)
    with pytest.raises(ValueError, match="0600"):
        read_credentials_environment(credentials)
    os.chmod(credentials, 0o600)

    symlink = tmp_path / "credentials-link.yaml"
    symlink.symlink_to(credentials)
    with pytest.raises(ValueError, match="non-symlink"):
        read_credentials_environment(symlink)

    monkeypatch.setattr(os, "geteuid", lambda: credentials.stat().st_uid + 1)
    with pytest.raises(ValueError, match="owned by the current user"):
        read_credentials_environment(credentials)


def test_credentials_reader_rejects_noncredential_environment_names(
    tmp_path: Path,
) -> None:
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text(
        "environment:\n  PATH: /untrusted/bin\n",
        encoding="utf-8",
    )
    os.chmod(credentials, 0o600)

    with pytest.raises(ValueError, match="non-credential"):
        read_credentials_environment(credentials)


def test_controller_credentials_install_every_model_role_atomically(
    tmp_path: Path,
) -> None:
    secret_directory = tmp_path / "secret"
    secret_directory.mkdir()
    expected: dict[str, str] = {}
    for environment_name, secret_file_name in CONTROLLER_CREDENTIAL_SECRET_FILES:
        value = f"credential-for-{environment_name.casefold()}"
        (secret_directory / secret_file_name).write_text(
            value + "\n",
            encoding="utf-8",
        )
        expected[environment_name] = value

    assert set(expected) == {
        "MODEL_ACCESS_KEY",
        "MINIMAX_CN_API_KEY",
        "DEEPSEEK_API_KEY",
    }
    destination = tmp_path / "credentials.yaml"
    install_controller_credentials(secret_directory, destination)

    assert destination.stat().st_mode & 0o777 == 0o600
    assert read_credentials_environment(destination) == expected


def test_controller_credentials_fail_closed_when_one_role_is_missing(
    tmp_path: Path,
) -> None:
    secret_directory = tmp_path / "secret"
    secret_directory.mkdir()
    for environment_name, secret_file_name in CONTROLLER_CREDENTIAL_SECRET_FILES:
        if environment_name == "DEEPSEEK_API_KEY":
            continue
        (secret_directory / secret_file_name).write_text(
            "present\n",
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        install_controller_credentials(
            secret_directory,
            tmp_path / "credentials.yaml",
        )


def test_factory_builds_exact_route_cwd_tools_and_session_db(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        allowed_tools=("work_status", "work_action"),
    )
    dbs: list[_FakeSessionDB] = []
    agents: list[_FakeAIAgent] = []
    definitions = [
        {"type": "function", "function": {"name": "work_action"}},
        {"type": "function", "function": {"name": "unapproved_tool"}},
        {"type": "function", "function": {"name": "work_status"}},
    ]

    def db_factory(path: Path) -> _FakeSessionDB:
        db = _FakeSessionDB(path)
        dbs.append(db)
        return db

    def constructor(**kwargs: Any) -> _FakeAIAgent:
        agent = _FakeAIAgent(kwargs)
        agents.append(agent)
        return agent

    factory = HermesAgentFactory(
        config,
        agent_constructor=constructor,
        session_db_factory=db_factory,
        credential_loader=lambda _path: {
            "MODEL_ACCESS_KEY": "super-secret-value"
        },
        tool_definition_loader=lambda: definitions,
    )
    request = _request(config)
    session = factory.create(request)

    assert len(agents) == 1
    agent = agents[0]
    assert agent.constructed_cwd == tmp_path
    assert agent.constructor_kwargs["enabled_toolsets"] == []
    assert agent.constructor_kwargs["skip_memory"] is True
    assert agent.constructor_kwargs["skip_background_review"] is True
    assert agent.constructor_kwargs["session_db"] is dbs[0]
    assert agent.constructor_kwargs["api_key"] == "super-secret-value"
    assert [item["function"]["name"] for item in agent.tools] == [
        "work_status",
        "work_action",
    ]
    assert agent.valid_tool_names == {"work_status", "work_action"}
    assert agent._skip_mcp_refresh is True
    assert agent._owns_session_db is False
    assert dbs[0].db_path == config.hermes.session_db_path
    assert factory.known_secrets() == ("super-secret-value",)

    result = session.run_runtime_turn(request)
    assert result["final_response"] == "completed"
    assert agent.turns[0]["cwd"] == tmp_path
    factory.attest(session, request)

    agent.tools.append(
        {"type": "function", "function": {"name": "unapproved_tool"}}
    )
    with pytest.raises(ValueError, match="exact tool allowlist"):
        factory.attest(session, request)

    session.close()
    assert agent.closed
    assert dbs[0].closed


def test_factory_restores_full_history_and_reopens_session(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
    ]
    dbs: list[_FakeSessionDB] = []
    agents: list[_FakeAIAgent] = []

    def db_factory(path: Path) -> _FakeSessionDB:
        db = _FakeSessionDB(
            path,
            sessions={
                "project-manager-session": {"id": "project-manager-session"}
            },
            histories={"project-manager-session": history},
        )
        dbs.append(db)
        return db

    def constructor(**kwargs: Any) -> _FakeAIAgent:
        agent = _FakeAIAgent(kwargs)
        agents.append(agent)
        return agent

    factory = HermesAgentFactory(
        config,
        agent_constructor=constructor,
        session_db_factory=db_factory,
        credential_loader=lambda _path: {"MODEL_ACCESS_KEY": "secret"},
        tool_definition_loader=lambda: [],
    )
    request = _request(config, request_id="restore-request")
    handle = RuntimeHandle(
        runtime_name="hermes",
        session_id="project-manager-session",
        task_id="project-hermes-manager",
        status=RuntimeStatus.IDLE,
        model_route=_runtime_route(config),
        worktree_path=str(tmp_path),
    )

    session = factory.restore(handle, request)
    result = session.run_runtime_turn(request)

    assert dbs[0].resume_safety_checks == ["project-manager-session"]
    assert dbs[0].reopened == ["project-manager-session"]
    assert dbs[0].history_calls == [
        ("project-manager-session", True, True)
    ]
    assert agents[0]._session_db_created is True
    assert agents[0].turns[0]["conversation_history"] == history
    assert result["messages"][:2] == history


def test_factory_restore_fails_closed_for_unknown_session(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    dbs: list[_FakeSessionDB] = []

    def db_factory(path: Path) -> _FakeSessionDB:
        db = _FakeSessionDB(path)
        dbs.append(db)
        return db

    factory = HermesAgentFactory(
        config,
        agent_constructor=lambda **kwargs: _FakeAIAgent(kwargs),
        session_db_factory=db_factory,
        credential_loader=lambda _path: {"MODEL_ACCESS_KEY": "secret"},
        tool_definition_loader=lambda: [],
    )
    request = _request(config)
    handle = RuntimeHandle(
        runtime_name="hermes",
        session_id="project-manager-session",
        task_id="project-hermes-manager",
        status=RuntimeStatus.IDLE,
        model_route=_runtime_route(config),
    )

    with pytest.raises(KeyError, match="unknown durable Hermes session"):
        factory.restore(handle, request)
    assert dbs[0].closed


def test_factory_rejects_cwd_outside_project_root(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    config = _config(project_root)
    factory = HermesAgentFactory(
        config,
        agent_constructor=lambda **kwargs: _FakeAIAgent(kwargs),
        session_db_factory=_FakeSessionDB,
        credential_loader=lambda _path: {"MODEL_ACCESS_KEY": "secret"},
        tool_definition_loader=lambda: [],
    )

    with pytest.raises(ValueError, match="inside project_root"):
        factory.create(_request(config, cwd=outside))


@pytest.mark.parametrize(
    ("raw", "expected_status", "expected_event"),
    [
        (
            {
                "final_response": "api_key=secret-value",
                "failed": True,
                "error": "nested secret-value",
                "details": {
                    "items": ["Bearer secret-value", {"token": "secret-value"}]
                },
            },
            RuntimeStatus.FAILED,
            "turn.failed",
        ),
        (
            {
                "final_response": "stopped secret-value",
                "interrupted": True,
                "interrupt_message": "Bearer secret-value",
            },
            RuntimeStatus.CANCELLED,
            "turn.cancelled",
        ),
    ],
)
def test_adapter_maps_terminal_states_and_recursively_redacts(
    raw: dict[str, Any],
    expected_status: RuntimeStatus,
    expected_event: str,
) -> None:
    agent = _FakeAIAgent(
        {
            "base_url": "https://models.example.test/v1",
            "api_key": "secret-value",
            "provider": "digitalocean",
            "api_mode": "chat_completions",
            "model": "main-model",
            "session_id": "project-manager-session",
            "session_db": object(),
            "enabled_toolsets": [],
            "disabled_toolsets": None,
        },
        results=[raw],
    )
    adapter = HermesRuntimeAdapter(
        lambda _request: agent,
        attest_agent=lambda _agent, _request: None,
        known_secrets=("secret-value",),
    )
    request = RuntimeRequest(
        request_id="request-1",
        task_id="project-hermes-manager",
        role=ProjectRole.PROJECT_HERMES,
        prompt="Run one turn.",
        session_id="project-manager-session",
    )

    handle = adapter.start(request)
    result = adapter.result(handle)
    events = list(adapter.events(handle))

    assert handle.status is expected_status
    assert result is not None
    assert result.status is expected_status
    assert "secret-value" not in json.dumps(
        result.model_dump(mode="json"),
        sort_keys=True,
    )
    assert events[-1].event_type == expected_event
    assert "secret-value" not in json.dumps(
        events[-1].model_dump(mode="json"),
        sort_keys=True,
    )


def test_adapter_does_not_hold_session_lock_during_agent_turn() -> None:
    turn_started = Event()
    release_turn = Event()

    class BlockingAgent:
        def __init__(self) -> None:
            self.calls = 0
            self.interrupted = False

        def run_conversation(
            self,
            prompt: str,
            *,
            task_id: str,
        ) -> dict[str, Any]:
            del prompt, task_id
            self.calls += 1
            if self.calls == 1:
                return {"final_response": "ready"}
            turn_started.set()
            assert release_turn.wait(2)
            return {
                "final_response": "cancelled",
                "interrupted": self.interrupted,
            }

        def interrupt(self) -> None:
            self.interrupted = True
            release_turn.set()

    agent = BlockingAgent()
    adapter = HermesRuntimeAdapter(
        lambda _request: agent,
        attest_agent=lambda _agent, _request: None,
    )
    first = RuntimeRequest(
        request_id="first",
        task_id="project-hermes-manager",
        role=ProjectRole.PROJECT_HERMES,
        prompt="first",
        session_id="project-manager-session",
    )
    handle = adapter.start(first)
    second = first.model_copy(
        update={"request_id": "second", "prompt": "second"}
    )
    resumed: list[RuntimeHandle] = []
    thread = Thread(target=lambda: resumed.append(adapter.resume(handle, second)))
    thread.start()
    assert turn_started.wait(1)

    cancelled = adapter.cancel(handle)
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert agent.interrupted
    assert cancelled.status is RuntimeStatus.CANCELLED
    assert resumed[0].status is RuntimeStatus.CANCELLED


def test_adapter_reconnect_rejects_route_changes() -> None:
    route = RuntimeModelRoute.model_validate(_route())
    other_route = RuntimeModelRoute.model_validate(
        _route(profile="reviewer", model="review-model")
    )
    handle = RuntimeHandle(
        runtime_name="hermes",
        session_id="project-manager-session",
        task_id="project-hermes-manager",
        status=RuntimeStatus.IDLE,
        model_route=route,
    )
    request = RuntimeRequest(
        request_id="reconnect",
        task_id="project-hermes-manager",
        role=ProjectRole.PROJECT_HERMES,
        prompt="continue",
        session_id="project-manager-session",
        model_route=other_route,
    )
    restored: list[RuntimeHandle] = []
    adapter = HermesRuntimeAdapter(
        lambda _request: object(),
        attest_agent=lambda _agent, _request: None,
        agent_restorer=lambda value, _request: restored.append(value),
    )

    with pytest.raises(ValueError, match="model profile differs"):
        adapter.reconnect(handle, request)
    assert restored == []
