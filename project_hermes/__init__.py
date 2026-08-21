"""ProjectHermes control-plane extensions for Hermes Agent.

The package is intentionally additive. It builds on Hermes' public agent,
plugin, Kanban, and Codex runtime surfaces without replacing the upstream
conversation loop.
"""

from project_hermes.controller import (
    CandidateGateResult,
    ProjectHermesController,
)
from project_hermes.execution import (
    ExecutionCoordinator,
    ExecutionRequest,
    ExecutionStatus,
)
from project_hermes.models import GoalRevision, IssueTask, ProjectRole
from project_hermes.polling import IssuePollingService
from project_hermes.polling_store import (
    PollingCandidate,
    SqlitePollingStore,
    WorkItem,
    WorkStatus,
)
from project_hermes.project_manager import MainHermesProjectManager
from project_hermes.policy import PolicyDecision, PolicyEngine
from project_hermes.runtime.base import AgentRuntime
from project_hermes.sessions import SessionCoordinator
from project_hermes.work_graph import (
    ActionRequest,
    LifecycleStatus,
    WorkGraph,
    WorkNode,
    WorkNodeKind,
)

__all__ = [
    "ActionRequest",
    "AgentRuntime",
    "CandidateGateResult",
    "ExecutionCoordinator",
    "ExecutionRequest",
    "ExecutionStatus",
    "GoalRevision",
    "IssueTask",
    "IssuePollingService",
    "LifecycleStatus",
    "MainHermesProjectManager",
    "PolicyDecision",
    "PolicyEngine",
    "PollingCandidate",
    "ProjectHermesController",
    "ProjectRole",
    "SessionCoordinator",
    "SqlitePollingStore",
    "WorkGraph",
    "WorkItem",
    "WorkNode",
    "WorkNodeKind",
    "WorkStatus",
]

__version__ = "0.1.0"
