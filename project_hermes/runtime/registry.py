"""Runtime adapter registry."""

from __future__ import annotations

from threading import RLock

from project_hermes.runtime.base import AgentRuntime


class RuntimeRegistry:
    """Thread-safe registry that rejects accidental runtime replacement."""

    def __init__(self) -> None:
        self._runtimes: dict[str, AgentRuntime] = {}
        self._lock = RLock()

    def register(self, runtime: AgentRuntime, *, replace: bool = False) -> None:
        name = runtime.name.strip().lower()
        if not name:
            raise ValueError("runtime name cannot be empty")
        with self._lock:
            if name in self._runtimes and not replace:
                raise ValueError(f"runtime already registered: {name}")
            self._runtimes[name] = runtime

    def get(self, name: str) -> AgentRuntime:
        key = name.strip().lower()
        with self._lock:
            try:
                return self._runtimes[key]
            except KeyError as exc:
                raise KeyError(f"unknown runtime: {name}") from exc

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._runtimes))
