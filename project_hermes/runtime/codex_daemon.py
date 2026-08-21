"""One persistent Codex SDK app-server daemon per ProjectHermes task.

The daemon exposes only a task-private Unix domain socket. Internally, the
official pinned Python SDK owns the app-server subprocess over stdio. This
keeps Codex protocol details out of the control plane while preserving a
private, reconnectable task boundary.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import socketserver
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, cast

from project_hermes.config import assert_private_directory
from project_hermes.credentials import read_credentials_environment
from project_hermes.models import utc_now
from project_hermes.redaction import redact_text
from project_hermes.runtime.codex_protocol import (
    CodexCommand,
    CodexCommandKind,
    CodexReply,
    CodexTaskMetadata,
    CodexTaskSpec,
)

_MAX_REQUEST_BYTES = 1_048_576


def _jsonable(value: Any) -> Any:
    """Convert SDK/dataclass/Pydantic values into JSON-compatible data."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _jsonable(model_dump(mode="json"))
    if hasattr(value, "value"):
        return _jsonable(value.value)
    if hasattr(value, "__dict__"):
        return {
            key: _jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


class CodexDriver(Protocol):
    """Small driver surface used by the socket daemon."""

    @property
    def thread_id(self) -> str:
        """Return the persistent root thread id."""

    def run_turn(self, prompt: str) -> dict[str, Any]:
        """Run one root-thread turn."""

    def cancel(self) -> bool:
        """Interrupt the active root turn if one exists."""

    def close(self) -> None:
        """Close the SDK client and app-server subprocess."""


class OpenAICodexDriver:
    """Official ``openai-codex`` SDK adapter."""

    def __init__(
        self,
        spec: CodexTaskSpec,
        *,
        resume_thread_id: str | None = None,
    ) -> None:
        self._spec = spec
        self._verify_versions()
        self._render_native_config()

        from openai_codex import Codex, CodexConfig, Sandbox

        environment = {
            "CODEX_HOME": str(spec.codex_home()),
            "CODEX_SQLITE_HOME": str(spec.sqlite_home()),
            "ROCR_VISIBLE_DEVICES": "",
            "HIP_VISIBLE_DEVICES": "",
            "CUDA_VISIBLE_DEVICES": "",
        }
        environment.update(self._load_credential_environment())
        if (
            spec.provider_api_key_env
            and spec.provider_api_key_env not in environment
        ):
            raise ValueError(
                "credentials_file does not define provider_api_key_env"
            )

        overrides = (
            'sandbox_mode="workspace-write"',
            (
                "sandbox_workspace_write.writable_roots="
                f"[{json.dumps(str(spec.worktree_path.resolve()))}]"
            ),
            (
                "sandbox_workspace_write.network_access="
                + ("true" if spec.network_access else "false")
            ),
        )
        config = CodexConfig(
            cwd=str(spec.worktree_path),
            env=environment,
            config_overrides=overrides,
            client_name="project_hermes",
            client_title="ProjectHermes",
            client_version="0.1.0",
        )
        self._codex = Codex(config)
        if resume_thread_id:
            self._thread = self._codex.thread_resume(
                resume_thread_id,
                cwd=str(spec.worktree_path),
                model=spec.model,
                model_provider=spec.model_provider,
                sandbox=Sandbox.workspace_write,
            )
        else:
            self._thread = self._codex.thread_start(
                cwd=str(spec.worktree_path),
                model=spec.model,
                model_provider=spec.model_provider,
                sandbox=Sandbox.workspace_write,
            )

        self._active_turn: Any = None
        self._active_lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"codex-{spec.task_id}",
        )

    @property
    def thread_id(self) -> str:
        return str(self._thread.id)

    def run_turn(self, prompt: str) -> dict[str, Any]:
        from openai_codex import Sandbox

        with self._active_lock:
            if self._active_turn is not None:
                raise RuntimeError("a Codex turn is already active")
            handle = self._thread.turn(
                prompt,
                cwd=str(self._spec.worktree_path),
                model=self._spec.model,
                sandbox=Sandbox.workspace_write,
            )
            self._active_turn = handle

        future = self._executor.submit(handle.run)
        try:
            result = future.result(timeout=self._spec.turn_timeout_seconds)
        except FutureTimeout as exc:
            try:
                handle.interrupt()
            finally:
                future.cancel()
            raise TimeoutError(
                "Codex turn exceeded the task turn deadline"
            ) from exc
        finally:
            with self._active_lock:
                self._active_turn = None

        return {
            "turn_id": str(result.id),
            "status": _jsonable(result.status),
            "error": _jsonable(result.error),
            "final_response": result.final_response,
            "items": _jsonable(result.items),
            "usage": _jsonable(result.usage),
            "started_at": result.started_at,
            "completed_at": result.completed_at,
            "duration_ms": result.duration_ms,
        }

    def cancel(self) -> bool:
        with self._active_lock:
            handle = self._active_turn
        if handle is None:
            return False
        handle.interrupt()
        return True

    def close(self) -> None:
        self.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._codex.close()

    def _verify_versions(self) -> None:
        installed_sdk = importlib.metadata.version("openai-codex")
        installed_runtime = importlib.metadata.version("openai-codex-cli-bin")
        if installed_sdk != self._spec.sdk_version:
            raise RuntimeError(
                f"openai-codex {installed_sdk} is installed; "
                f"ProjectHermes requires {self._spec.sdk_version}"
            )
        if installed_runtime != self._spec.cli_version:
            raise RuntimeError(
                f"openai-codex-cli-bin {installed_runtime} is installed; "
                f"ProjectHermes requires {self._spec.cli_version}"
            )

    def _render_native_config(self) -> None:
        """Write the task's native Codex config without credentials."""

        home = self._spec.codex_home()
        assert_private_directory(home)
        assert_private_directory(self._spec.sqlite_home())
        config_path = home / "config.toml"
        lines = [
            "# Generated by ProjectHermes. Do not place credentials here.",
            'sandbox_mode = "workspace-write"',
        ]
        if self._spec.model:
            lines.append(f"model = {json.dumps(self._spec.model)}")
        if self._spec.model_provider:
            lines.append(
                f"model_provider = {json.dumps(self._spec.model_provider)}"
            )
        if self._spec.reasoning_effort:
            lines.append(
                "model_reasoning_effort = "
                f"{json.dumps(self._spec.reasoning_effort)}"
            )
        if self._spec.context_window is not None:
            lines.append(f"model_context_window = {self._spec.context_window}")
        if self._spec.provider_endpoint:
            provider = json.dumps(self._spec.model_provider)
            lines.extend(
                [
                    "",
                    f"[model_providers.{provider}]",
                    f"name = {json.dumps(self._spec.model_provider)}",
                    f"base_url = {json.dumps(self._spec.provider_endpoint)}",
                    f"env_key = {json.dumps(self._spec.provider_api_key_env)}",
                    f"wire_api = {json.dumps(self._spec.provider_wire_api)}",
                ]
            )
        lines.extend(
            [
                "",
                "[sandbox_workspace_write]",
                "writable_roots = "
                f"[{json.dumps(str(self._spec.worktree_path.resolve()))}]",
                "network_access = "
                + ("true" if self._spec.network_access else "false"),
                "",
            ]
        )
        descriptor = os.open(
            config_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(
                descriptor,
                "w",
                encoding="utf-8",
            ) as stream:
                descriptor = -1
                stream.write("\n".join(lines))
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _load_credential_environment(self) -> dict[str, str]:
        """Load only the explicit environment mapping from private YAML."""

        path = self._spec.credentials_file
        if path is None:
            return {}
        return read_credentials_environment(path)


class _ThreadingUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    task_daemon: TaskCodexDaemon


class _CodexRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
        if len(line) > _MAX_REQUEST_BYTES:
            reply = CodexReply(
                request_id="unknown",
                ok=False,
                error="request exceeds one MiB",
            )
        else:
            server = cast(_ThreadingUnixServer, self.server)
            reply = server.task_daemon.handle_payload(line)
        self.wfile.write(
            (
                json.dumps(reply.model_dump(mode="json"), ensure_ascii=True)
                + "\n"
            ).encode("utf-8")
        )
        self.wfile.flush()


class TaskCodexDaemon:
    """Task-private socket server with one root Codex thread."""

    def __init__(
        self,
        spec: CodexTaskSpec,
        *,
        driver_factory: Callable[..., CodexDriver] = OpenAICodexDriver,
    ) -> None:
        self.spec = spec
        self._driver_factory = driver_factory
        self._driver: CodexDriver | None = None
        self._server: _ThreadingUnixServer | None = None
        self._metadata_lock = threading.RLock()
        self._turn_lock = threading.Lock()
        self._events_lock = threading.RLock()
        self._sequence = self._last_event_sequence()
        self._metadata = self._load_or_create_metadata()

    def serve_forever(self) -> None:
        """Bind the private socket and serve until shutdown."""

        task_dir = self.spec.task_directory()
        assert_private_directory(task_dir)
        assert_private_directory(self.spec.codex_home())
        assert_private_directory(self.spec.sqlite_home())
        socket_path = self.spec.socket_path()
        if len(os.fsencode(socket_path)) >= 104:
            raise ValueError(
                "Codex socket path is too long; shorten runtime_root"
            )
        if socket_path.exists():
            socket_path.unlink()

        server = _ThreadingUnixServer(str(socket_path), _CodexRequestHandler)
        server.task_daemon = self
        self._server = server
        os.chmod(socket_path, 0o600)
        self._update_metadata(status="IDLE", pid=os.getpid(), last_error=None)
        self._append_event("daemon.started", {"pid": os.getpid()})
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            server.server_close()
            if self._driver is not None:
                self._driver.close()
                self._driver = None
            socket_path.unlink(missing_ok=True)
            self._update_metadata(status="CLOSED", active_turn_id=None)
            self._append_event("daemon.closed", {})

    def handle_payload(self, payload: bytes) -> CodexReply:
        """Validate and execute one socket command."""

        try:
            raw = json.loads(payload.decode("utf-8"))
            command = CodexCommand.model_validate(raw)
        except Exception as exc:
            return CodexReply(
                request_id="unknown",
                ok=False,
                error=f"invalid request: {exc}",
            )

        try:
            if command.command is CodexCommandKind.STATUS:
                result = self._metadata.model_dump(mode="json")
            elif command.command is CodexCommandKind.EVENTS:
                result = {
                    "events": self._read_events(command.after_sequence)
                }
            elif command.command is CodexCommandKind.RESULT:
                result = self._read_result()
            elif command.command is CodexCommandKind.RUN_TURN:
                if command.prompt is None or not command.prompt.strip():
                    raise ValueError("run_turn requires a non-empty prompt")
                result = self._run_turn(command.prompt)
            elif command.command is CodexCommandKind.CANCEL:
                cancelled = bool(self._driver and self._driver.cancel())
                self._append_event(
                    "turn.cancel_requested",
                    {"active": cancelled},
                )
                result = {"cancel_requested": cancelled}
            elif command.command is CodexCommandKind.SHUTDOWN:
                result = {"shutdown_requested": True}
                threading.Thread(
                    target=self._shutdown,
                    name=f"shutdown-{self.spec.task_id}",
                    daemon=True,
                ).start()
            else:  # pragma: no cover - StrEnum validation is exhaustive
                raise ValueError(f"unsupported command: {command.command}")
            return CodexReply(
                request_id=command.request_id,
                ok=True,
                payload=result,
            )
        except Exception as exc:
            error = redact_text(str(exc))
            self._update_metadata(last_error=error)
            self._append_event(
                "daemon.command_failed",
                {
                    "request_id": command.request_id,
                    "command": command.command.value,
                    "error": error,
                },
            )
            return CodexReply(
                request_id=command.request_id,
                ok=False,
                error=error,
            )

    def _run_turn(self, prompt: str) -> dict[str, Any]:
        if not self._turn_lock.acquire(blocking=False):
            raise RuntimeError("a root Codex turn is already running")
        try:
            driver = self._ensure_driver()
            self._update_metadata(
                status="RUNNING",
                root_thread_id=driver.thread_id,
                active_turn_id="pending",
                last_error=None,
            )
            self._append_event(
                "turn.started",
                {"root_thread_id": driver.thread_id},
            )
            result = driver.run_turn(prompt)
            turn_id = str(result.get("turn_id") or "")
            self._write_result(result)
            self._update_metadata(
                status="IDLE",
                root_thread_id=driver.thread_id,
                active_turn_id=None,
                last_error=None,
            )
            self._append_event(
                "turn.completed",
                {
                    "root_thread_id": driver.thread_id,
                    "turn_id": turn_id,
                    "status": result.get("status"),
                    "item_count": len(result.get("items") or []),
                },
            )
            return result
        except Exception as exc:
            error = redact_text(str(exc))
            failed = {
                "status": "failed",
                "thread_id": (
                    self._driver.thread_id if self._driver is not None else None
                ),
                "turn_id": None,
                "items": [],
                "final_response": None,
                "error": error,
                "completed_at": utc_now().isoformat(),
            }
            self._write_result(failed)
            self._update_metadata(
                status="FAILED",
                active_turn_id=None,
                last_error=error,
            )
            self._append_event(
                "turn.failed",
                {
                    "root_thread_id": failed["thread_id"],
                    "error": error,
                },
            )
            raise RuntimeError(error) from exc
        finally:
            self._turn_lock.release()

    def _ensure_driver(self) -> CodexDriver:
        if self._driver is None:
            resume_thread_id = self._metadata.root_thread_id
            self._driver = self._driver_factory(
                self.spec,
                resume_thread_id=resume_thread_id,
            )
            self._update_metadata(root_thread_id=self._driver.thread_id)
            self._append_event(
                "thread.resumed"
                if resume_thread_id
                else "thread.started",
                {"root_thread_id": self._driver.thread_id},
            )
        return self._driver

    def _shutdown(self) -> None:
        server = self._server
        if server is not None:
            server.shutdown()

    def _load_or_create_metadata(self) -> CodexTaskMetadata:
        path = self.spec.metadata_path()
        if path.exists():
            with path.open("r", encoding="utf-8") as stream:
                metadata = CodexTaskMetadata.model_validate(json.load(stream))
            expected = {
                "task_id": self.spec.task_id,
                "session_id": self.spec.session_id,
                "sdk_version": self.spec.sdk_version,
                "cli_version": self.spec.cli_version,
                "model_profile": self.spec.model_profile,
                "model": self.spec.model,
                "model_provider": self.spec.model_provider,
                "reasoning_mode": self.spec.reasoning_mode,
                "reasoning_effort": self.spec.reasoning_effort,
                "context_window": self.spec.context_window,
                "runtime_generation": self.spec.runtime_generation,
                "socket_path": str(self.spec.socket_path()),
                "codex_home": str(self.spec.codex_home()),
                "worktree_path": str(self.spec.worktree_path),
            }
            mismatched = [
                name
                for name, value in expected.items()
                if getattr(metadata, name) != value
            ]
            if mismatched:
                raise ValueError(
                    "task metadata differs from daemon spec: "
                    + ", ".join(mismatched)
                )
            return metadata
        metadata = CodexTaskMetadata(
            task_id=self.spec.task_id,
            session_id=self.spec.session_id,
            sdk_version=self.spec.sdk_version,
            cli_version=self.spec.cli_version,
            model_profile=self.spec.model_profile,
            model=self.spec.model,
            model_provider=self.spec.model_provider,
            reasoning_mode=self.spec.reasoning_mode,
            reasoning_effort=self.spec.reasoning_effort,
            context_window=self.spec.context_window,
            runtime_generation=self.spec.runtime_generation,
            socket_path=str(self.spec.socket_path()),
            codex_home=str(self.spec.codex_home()),
            worktree_path=str(self.spec.worktree_path),
        )
        self._write_metadata(metadata)
        return metadata

    def _update_metadata(self, **updates: Any) -> None:
        with self._metadata_lock:
            updates["updated_at"] = utc_now()
            self._metadata = CodexTaskMetadata.model_validate(
                self._metadata.model_copy(update=updates).model_dump()
            )
            self._write_metadata(self._metadata)

    def _write_metadata(self, metadata: CodexTaskMetadata) -> None:
        path = self.spec.metadata_path()
        assert_private_directory(path.parent)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                metadata.model_dump(mode="json"),
                sort_keys=True,
                indent=2,
                ensure_ascii=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def _append_event(self, event_type: str, payload: dict[str, Any]) -> None:
        with self._events_lock:
            self._sequence += 1
            event = {
                "sequence": self._sequence,
                "event_type": event_type,
                "task_id": self.spec.task_id,
                "session_id": self.spec.session_id,
                "payload": _jsonable(payload),
                "created_at": utc_now().isoformat(),
            }
            path = self.spec.events_path()
            with path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(event, sort_keys=True, ensure_ascii=True) + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(path, 0o600)

    def _read_events(self, after_sequence: int) -> list[dict[str, Any]]:
        path = self.spec.events_path()
        if not path.exists():
            return []
        with self._events_lock, path.open("r", encoding="utf-8") as stream:
            events = [json.loads(line) for line in stream if line.strip()]
        return [
            event
            for event in events
            if int(event.get("sequence") or 0) > after_sequence
        ]

    def _write_result(self, result: dict[str, Any]) -> None:
        path = self.spec.result_path()
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                _jsonable(result),
                sort_keys=True,
                indent=2,
                ensure_ascii=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def _read_result(self) -> dict[str, Any]:
        path = self.spec.result_path()
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as stream:
            result = json.load(stream)
        if not isinstance(result, dict):
            raise ValueError("stored Codex result is malformed")
        return result

    def _last_event_sequence(self) -> int:
        path = self.spec.events_path()
        if not path.exists():
            return 0
        last = 0
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    last = max(last, int(json.loads(line).get("sequence") or 0))
        return last


def _load_spec(path: Path) -> CodexTaskSpec:
    with path.open("r", encoding="utf-8") as stream:
        return CodexTaskSpec.model_validate(json.load(stream))


@contextmanager
def _exclusive_daemon_lock(spec: CodexTaskSpec) -> Iterator[None]:
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError(
            "task-private Codex locking requires a POSIX runtime"
        ) from exc
    assert_private_directory(spec.task_directory())
    path = spec.task_directory() / "daemon.lock"
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_RDWR | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(
                descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            raise RuntimeError(
                "another root Codex daemon already owns this task"
            ) from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one task-private ProjectHermes Codex daemon."
    )
    parser.add_argument("--spec", required=True, type=Path)
    args = parser.parse_args(argv)
    spec = _load_spec(args.spec)
    with _exclusive_daemon_lock(spec):
        TaskCodexDaemon(spec).serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
