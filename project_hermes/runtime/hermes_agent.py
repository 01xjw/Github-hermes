"""Production construction and restoration for ProjectHermes AIAgents."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterator
from uuid import uuid4

from agent import runtime_cwd

from project_hermes.config import (
    HermesRuntimeConfig,
    ModelReasoningMode,
    ModelRouteConfig,
    ProjectHermesConfig,
    assert_private_directory,
)
from project_hermes.credentials import read_credentials_environment
from project_hermes.runtime.base import (
    RuntimeHandle,
    RuntimeModelRoute,
    RuntimeRequest,
)
from project_hermes.runtime.hermes import HermesRuntimeAdapter

AgentConstructor = Callable[..., Any]
SessionDBFactory = Callable[[Path], Any]
CredentialLoader = Callable[[str | Path], dict[str, str]]
ToolDefinitionLoader = Callable[[], list[dict[str, Any]]]


@contextmanager
def _scoped_session_cwd(cwd: Path) -> Iterator[None]:
    token = runtime_cwd.set_session_cwd(str(cwd))
    try:
        yield
    finally:
        token.var.reset(token)


def _default_agent_constructor() -> AgentConstructor:
    from run_agent import AIAgent

    return AIAgent


def _default_session_db_factory(path: Path) -> Any:
    from hermes_state import SessionDB

    return SessionDB(path)


def _default_tool_definition_loader() -> list[dict[str, Any]]:
    from model_tools import get_tool_definitions

    return get_tool_definitions(
        enabled_toolsets=["all"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )


class HermesAgentSession:
    """Long-lived AIAgent plus its controller-owned persistence handle."""

    def __init__(
        self,
        agent: Any,
        session_db: Any,
        *,
        logical_session_id: str,
        native_session_id: str,
        model_route: RuntimeModelRoute,
        default_cwd: Path,
        allowed_cwd_root: Path,
        allowed_tools: tuple[str, ...],
        api_key: str,
        conversation_history: list[dict[str, Any]] | None = None,
    ) -> None:
        self.agent = agent
        self.session_db = session_db
        self.logical_session_id = logical_session_id
        self.native_session_id = native_session_id
        self.model_route = model_route
        self.default_cwd = default_cwd
        self.allowed_cwd_root = allowed_cwd_root
        self.allowed_tools = allowed_tools
        self._api_key = api_key
        self._conversation_history = conversation_history
        self._closed = False

    @property
    def session_id(self) -> str:
        return self.logical_session_id

    def run_runtime_turn(self, request: RuntimeRequest) -> dict[str, Any]:
        if request.model_route != self.model_route:
            raise ValueError("Hermes turn model route differs from its session")
        cwd = self._resolve_turn_cwd(request.cwd)
        return self._run(
            request.prompt,
            task_id=request.task_id,
            cwd=cwd,
        )

    def run_conversation(
        self,
        prompt: str,
        *,
        task_id: str,
    ) -> dict[str, Any]:
        return self._run(prompt, task_id=task_id, cwd=self.default_cwd)

    def request_interrupt(self) -> None:
        interrupt = getattr(self.agent, "interrupt", None)
        if not callable(interrupt):
            return
        try:
            interrupt(hard_cancel=True)
        except TypeError:
            interrupt()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self.agent, "close", None)
            if callable(close):
                close()
        finally:
            self.session_db.close()

    def _run(
        self,
        prompt: str,
        *,
        task_id: str,
        cwd: Path,
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("Hermes agent session is closed")
        kwargs: dict[str, Any] = {"task_id": task_id}
        if self._conversation_history is not None:
            kwargs["conversation_history"] = self._conversation_history
        with _scoped_session_cwd(cwd):
            result = self.agent.run_conversation(prompt, **kwargs)
        if not isinstance(result, dict):
            raise TypeError("AIAgent.run_conversation() must return a mapping")
        native_session_id = str(getattr(self.agent, "session_id", "") or "")
        if not native_session_id:
            raise RuntimeError("AIAgent lost its native session identity")
        self.native_session_id = native_session_id
        messages = result.get("messages")
        if isinstance(messages, list):
            self._conversation_history = [
                dict(message)
                for message in messages
                if isinstance(message, dict)
            ]
        return result

    def _resolve_turn_cwd(self, value: str | None) -> Path:
        if value is None:
            return self.default_cwd
        cwd = Path(value).expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError("Hermes turn cwd must be an existing directory")
        if not cwd.is_relative_to(self.allowed_cwd_root):
            raise ValueError("Hermes cwd must remain inside project_root")
        return cwd


class HermesAgentFactory:
    """Create, restore, and attest least-privilege Hermes sessions."""

    def __init__(
        self,
        config: ProjectHermesConfig,
        *,
        agent_constructor: AgentConstructor | None = None,
        session_db_factory: SessionDBFactory | None = None,
        credential_loader: CredentialLoader = read_credentials_environment,
        tool_definition_loader: ToolDefinitionLoader | None = None,
    ) -> None:
        self.config = config
        self.runtime_config = config.hermes
        self.project_root = Path(config.project_root).expanduser().resolve()
        self.session_db_path = self._project_path(
            self.runtime_config.session_db_path
        )
        self.credentials_file = (
            None
            if self.runtime_config.credentials_file is None
            else self._project_path(self.runtime_config.credentials_file)
        )
        self._agent_constructor = agent_constructor
        self._session_db_factory = (
            session_db_factory or _default_session_db_factory
        )
        self._credential_loader = credential_loader
        self._tool_definition_loader = (
            tool_definition_loader or _default_tool_definition_loader
        )
        self._secrets: set[str] = set()
        self._secrets_lock = RLock()

    def create(self, request: RuntimeRequest) -> HermesAgentSession:
        """Create a new Main Hermes session on the configured main route."""

        session_id = request.session_id or f"hermes-{uuid4().hex}"
        effective_request = request.model_copy(
            update={"session_id": session_id}
        )
        return self._build(
            effective_request,
            route_config=self.runtime_config.model_profile,
            allowed_tools=self.runtime_config.allowed_tools,
            restore=False,
        )

    def restore(
        self,
        handle: RuntimeHandle,
        request: RuntimeRequest,
    ) -> HermesAgentSession:
        """Restore a Main Hermes session and its full durable conversation."""

        if handle.runtime_name != HermesRuntimeAdapter.name:
            raise ValueError("runtime handle belongs to a different adapter")
        if request.task_id != handle.task_id:
            raise ValueError("restore request belongs to a different task")
        expected = self._validate_route(
            request,
            self.runtime_config.model_profile,
        )
        if handle.model_route != expected:
            raise ValueError("runtime handle model route is not Main Hermes")
        if (
            request.session_id is not None
            and request.session_id != handle.session_id
        ):
            raise ValueError("restore request names a different session")
        effective_request = request.model_copy(
            update={"session_id": handle.session_id}
        )
        return self._build(
            effective_request,
            route_config=self.runtime_config.model_profile,
            allowed_tools=self.runtime_config.allowed_tools,
            restore=True,
        )

    def attest(
        self,
        session: Any,
        request: RuntimeRequest,
    ) -> None:
        """Fail unless a session exactly matches Main Hermes policy."""

        if not isinstance(session, HermesAgentSession):
            raise TypeError("Hermes factory produced an unsupported session")
        expected = self._validate_route(
            request,
            self.runtime_config.model_profile,
        )
        self._attest_session(
            session,
            request=request,
            expected=expected,
            allowed_tools=self.runtime_config.allowed_tools,
        )

    def known_secrets(self) -> tuple[str, ...]:
        """Return credential values already loaded by this factory."""

        with self._secrets_lock:
            return tuple(self._secrets)

    def create_reviewer(
        self,
        request: RuntimeRequest,
    ) -> HermesAgentSession:
        """Create one capability-free independent reviewer session."""

        return self._build(
            request,
            route_config=self.runtime_config.route_for_review(),
            allowed_tools=(),
            restore=False,
        )

    def create_screening(
        self,
        request: RuntimeRequest,
    ) -> HermesAgentSession:
        """Create one capability-free Subagent on the Main Hermes route."""

        return self._build(
            request,
            route_config=self.runtime_config.model_profile,
            allowed_tools=(),
            restore=False,
        )

    def _build(
        self,
        request: RuntimeRequest,
        *,
        route_config: ModelRouteConfig,
        allowed_tools: tuple[str, ...],
        restore: bool,
    ) -> HermesAgentSession:
        expected = self._validate_route(request, route_config)
        if request.session_id is None:
            raise ValueError("Hermes sessions require a session_id")
        cwd = self._request_cwd(request.cwd)
        api_key = self._load_api_key(expected)
        assert_private_directory(self.session_db_path.parent)
        session_db = self._session_db_factory(self.session_db_path)
        agent: Any = None
        try:
            history: list[dict[str, Any]] | None = None
            native_session_id = request.session_id
            if restore:
                get_session = getattr(session_db, "get_session", None)
                if not callable(get_session):
                    raise TypeError(
                        "Hermes SessionDB does not provide get_session()"
                    )
                if get_session(request.session_id) is None:
                    raise KeyError(
                        f"unknown durable Hermes session: {request.session_id}"
                    )
                resolve_resume = getattr(
                    session_db,
                    "resolve_resume_session_id",
                    None,
                )
                if callable(resolve_resume):
                    native_session_id = str(
                        resolve_resume(request.session_id)
                        or request.session_id
                    )
                if get_session(native_session_id) is None:
                    raise KeyError(
                        "unknown durable Hermes continuation session: "
                        f"{native_session_id}"
                    )
                assert_resume_safe = getattr(
                    session_db,
                    "assert_resume_safe",
                    None,
                )
                if callable(assert_resume_safe):
                    assert_resume_safe(native_session_id)
                get_history = getattr(
                    session_db,
                    "get_messages_as_conversation",
                    None,
                )
                if not callable(get_history):
                    raise TypeError(
                        "Hermes SessionDB does not provide "
                        "get_messages_as_conversation()"
                    )
                history = get_history(
                    native_session_id,
                    include_ancestors=True,
                    repair_alternation=True,
                )
                if not isinstance(history, list) or any(
                    not isinstance(message, dict) for message in history
                ):
                    raise TypeError(
                        "Hermes SessionDB returned malformed conversation history"
                    )
                reopen_session = getattr(
                    session_db,
                    "reopen_session",
                    None,
                )
                if not callable(reopen_session):
                    raise TypeError(
                        "Hermes SessionDB does not provide reopen_session()"
                    )
                reopen_session(native_session_id)
            tool_definitions = self._tool_definitions(allowed_tools)
            constructor = (
                self._agent_constructor or _default_agent_constructor()
            )
            with _scoped_session_cwd(cwd):
                agent = constructor(
                    base_url=expected.provider_endpoint,
                    api_key=api_key,
                    provider=expected.model_provider,
                    requested_provider=expected.model_provider,
                    api_mode=self._api_mode(expected),
                    model=expected.model,
                    reasoning_config=self._reasoning_config(expected),
                    enabled_toolsets=[],
                    disabled_toolsets=None,
                    save_trajectories=False,
                    quiet_mode=True,
                    tool_progress_mode="off",
                    session_id=native_session_id,
                    platform="project-hermes",
                    skip_memory=True,
                    skip_background_review=True,
                    session_db=session_db,
                )
            agent.tools = list(tool_definitions)
            agent.valid_tool_names = set(allowed_tools)
            agent.enabled_toolsets = []
            agent.disabled_toolsets = None
            agent._skip_mcp_refresh = True
            agent._kanban_worker_guidance = ""
            agent._owns_session_db = False
            if restore:
                agent._session_db_created = True
            session = HermesAgentSession(
                agent,
                session_db,
                logical_session_id=request.session_id,
                native_session_id=native_session_id,
                model_route=expected,
                default_cwd=cwd,
                allowed_cwd_root=self.project_root,
                allowed_tools=allowed_tools,
                api_key=api_key,
                conversation_history=history,
            )
        except Exception:
            if agent is not None:
                close = getattr(agent, "close", None)
                if callable(close):
                    close()
            session_db.close()
            raise

        try:
            self._attest_session(
                session,
                request=request,
                expected=expected,
                allowed_tools=allowed_tools,
            )
        except Exception:
            session.close()
            raise
        return session

    def _attest_session(
        self,
        session: HermesAgentSession,
        *,
        request: RuntimeRequest,
        expected: RuntimeModelRoute,
        allowed_tools: tuple[str, ...],
    ) -> None:
        agent = session.agent
        if session.model_route != expected:
            raise ValueError("Hermes session route attestation failed")
        if session.session_id != request.session_id:
            raise ValueError("Hermes session identity attestation failed")
        if (
            str(getattr(agent, "session_id", ""))
            != session.native_session_id
        ):
            raise ValueError("Hermes native session identity attestation failed")
        if session.allowed_cwd_root != self.project_root:
            raise ValueError("Hermes cwd policy attestation failed")
        if session.default_cwd != self._request_cwd(request.cwd):
            raise ValueError("Hermes cwd attestation failed")
        if str(getattr(agent, "model", "")) != expected.model:
            raise ValueError("Hermes model attestation failed")
        if str(getattr(agent, "provider", "")) != expected.model_provider:
            raise ValueError("Hermes provider attestation failed")
        if (
            str(getattr(agent, "base_url", "")).rstrip("/")
            != expected.provider_endpoint.rstrip("/")
        ):
            raise ValueError("Hermes endpoint attestation failed")
        if getattr(agent, "api_mode", None) != self._api_mode(expected):
            raise ValueError("Hermes wire API attestation failed")
        if getattr(agent, "api_key", None) != session._api_key:
            raise ValueError("Hermes credential attestation failed")
        if getattr(agent, "_session_db", None) is not session.session_db:
            raise ValueError("Hermes SessionDB ownership attestation failed")
        db_path = getattr(session.session_db, "db_path", self.session_db_path)
        if Path(db_path).resolve() != self.session_db_path:
            raise ValueError("Hermes SessionDB path attestation failed")
        names = tuple(
            definition.get("function", {}).get("name")
            for definition in getattr(agent, "tools", ())
        )
        if names != allowed_tools:
            raise ValueError("Hermes exact tool allowlist attestation failed")
        if set(getattr(agent, "valid_tool_names", ())) != set(allowed_tools):
            raise ValueError("Hermes tool validation scope attestation failed")
        if getattr(agent, "enabled_toolsets", None) != []:
            raise ValueError("Hermes toolset expansion is not disabled")
        if getattr(agent, "_skip_mcp_refresh", False) is not True:
            raise ValueError("Hermes MCP expansion is not disabled")
        if getattr(agent, "_fallback_chain", ()) not in ([], ()):
            raise ValueError("Hermes fallback model expansion is not disabled")

    def _validate_route(
        self,
        request: RuntimeRequest,
        route_config: ModelRouteConfig,
    ) -> RuntimeModelRoute:
        expected = RuntimeModelRoute.model_validate(route_config.model_dump())
        if request.model_route is None:
            raise ValueError("Hermes requests require an explicit model_route")
        if request.model_route != expected:
            raise ValueError(
                "Hermes request model_route differs from configured policy"
            )
        if request.model is not None and request.model != expected.model:
            raise ValueError("Hermes request model differs from model_route")
        return expected

    def _load_api_key(self, route: RuntimeModelRoute) -> str:
        if self.credentials_file is None:
            raise ValueError("Hermes credentials_file is not configured")
        environment = self._credential_loader(self.credentials_file)
        api_key = environment.get(route.provider_api_key_env)
        if not api_key:
            raise ValueError(
                "Hermes credentials_file does not define provider_api_key_env"
            )
        with self._secrets_lock:
            self._secrets.update(environment.values())
        return api_key

    def _tool_definitions(
        self,
        allowed_tools: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        if not allowed_tools:
            return []
        available = {
            definition.get("function", {}).get("name"): definition
            for definition in self._tool_definition_loader()
        }
        missing = [name for name in allowed_tools if name not in available]
        if missing:
            raise ValueError(
                "Hermes allowed_tools are unavailable: "
                + ", ".join(missing)
            )
        return [available[name] for name in allowed_tools]

    def _request_cwd(self, value: str | None) -> Path:
        cwd = (
            Path(value).expanduser().resolve()
            if value is not None
            else self.project_root
        )
        if not cwd.is_dir():
            raise ValueError("Hermes cwd must be an existing directory")
        if not cwd.is_relative_to(self.project_root):
            raise ValueError("Hermes cwd must remain inside project_root")
        return cwd

    def _project_path(self, value: Path) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.project_root / path
        return path.resolve()

    @staticmethod
    def _api_mode(route: RuntimeModelRoute) -> str:
        if route.provider_wire_api == "chat":
            return "chat_completions"
        return "codex_responses"

    @staticmethod
    def _reasoning_config(
        route: RuntimeModelRoute,
    ) -> dict[str, Any] | None:
        if route.reasoning_mode is ModelReasoningMode.DISABLED:
            return {"enabled": False}
        if route.reasoning_mode is ModelReasoningMode.ADAPTIVE:
            return {
                "enabled": True,
                "effort": route.reasoning_effort or "medium",
            }
        if route.reasoning_effort:
            return {"effort": route.reasoning_effort}
        return None


def build_hermes_runtime(
    config: ProjectHermesConfig,
    *,
    factory: HermesAgentFactory | None = None,
    **factory_kwargs: Any,
) -> HermesRuntimeAdapter:
    """Build the production Main Hermes runtime and recovery hooks."""

    if factory is not None and factory_kwargs:
        raise ValueError("factory and factory_kwargs are mutually exclusive")
    agent_factory = factory or HermesAgentFactory(config, **factory_kwargs)
    return HermesRuntimeAdapter(
        agent_factory.create,
        attest_agent=agent_factory.attest,
        agent_restorer=agent_factory.restore,
        known_secrets=agent_factory.known_secrets,
    )


def hermes_runtime_config(
    config: ProjectHermesConfig,
) -> HermesRuntimeConfig:
    """Return the runtime config for callers that only need loop policy."""

    return config.hermes


__all__ = [
    "HermesAgentFactory",
    "HermesAgentSession",
    "build_hermes_runtime",
    "hermes_runtime_config",
]
