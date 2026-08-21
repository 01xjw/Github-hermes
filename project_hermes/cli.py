"""Operator CLI for ProjectHermes control-plane extensions."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from project_hermes.assurance import (
    CleanupTombstone,
    CompletionMatrix,
    EvidenceRecord,
    KnowledgeCandidate,
    KnowledgeCommit,
    RepositoryReviewInput,
    ReviewGate,
    ReviewPacket,
    ReviewRecord,
)
from project_hermes.assurance_store import open_assurance_store
from project_hermes.config import (
    PINNED_CODEX_SDK_VERSION,
    ProjectHermesConfig,
    config_fingerprint,
    load_config,
)
from project_hermes.controller import (
    CandidateGateResult,
    ProjectHermesController,
)
from project_hermes.discovery import GitHubClient
from project_hermes.english import scan_english_only
from project_hermes.execution import (
    BackendExecution,
    ExecutionObservation,
    ExecutionRecord,
    ExecutionRequest,
)
from project_hermes.models import (
    CapabilityMatrix,
    GoalRevision,
    IssueTask,
    RepositoryResponsibility,
)
from project_hermes.polling import IssuePollingService
from project_hermes.polling_store import (
    PollingCandidate,
    PollingRun,
    PollingTask,
    WorkItem,
    WorkPlan,
    open_polling_store,
)
from project_hermes.resources import (
    ArtifactManifest,
    ArtifactRequest,
    GpuDevice,
    GpuLease,
    GpuRequest,
    ResourceBundle,
    WorkspaceLease,
)
from project_hermes.publication import (
    CiCheckRecord,
    PublicationApproval,
    PublicationGateResult,
    PullRequestCandidate,
    RemoteCandidateObservation,
)
from project_hermes.review_resolution import (
    ResolutionGateResult,
    ReviewResolution,
)
from project_hermes.runtime.base import (
    RuntimeEvent,
    RuntimeHandle,
    RuntimeRequest,
    RuntimeResult,
)
from project_hermes.runtime.codex_protocol import (
    CodexCommand,
    CodexReply,
    CodexTaskMetadata,
    CodexTaskSpec,
)
from project_hermes.sessions import RuntimeBinding
from project_hermes.store import open_run_store
from project_hermes.supply_chain import (
    ImageSourceMap,
    ModelSourceMap,
    SshRelayConfig,
    SupplyRecord,
)
from project_hermes.work_graph import (
    ActionRequest,
    AgentEvent,
    AgentSession,
    PipelineRun,
    WorkGraph,
    WorkNode,
)

_SCHEMA_MODELS = (
    ActionRequest,
    AgentEvent,
    AgentSession,
    ArtifactManifest,
    ArtifactRequest,
    BackendExecution,
    CapabilityMatrix,
    CandidateGateResult,
    CiCheckRecord,
    CleanupTombstone,
    CodexCommand,
    CodexReply,
    CodexTaskMetadata,
    CodexTaskSpec,
    CompletionMatrix,
    EvidenceRecord,
    ExecutionObservation,
    ExecutionRecord,
    ExecutionRequest,
    GoalRevision,
    GpuDevice,
    GpuLease,
    GpuRequest,
    ImageSourceMap,
    IssueTask,
    KnowledgeCandidate,
    KnowledgeCommit,
    ModelSourceMap,
    PipelineRun,
    PollingCandidate,
    PollingRun,
    PollingTask,
    ProjectHermesConfig,
    PublicationApproval,
    PublicationGateResult,
    PullRequestCandidate,
    RepositoryReviewInput,
    RepositoryResponsibility,
    ResourceBundle,
    ResolutionGateResult,
    ReviewRecord,
    ReviewGate,
    ReviewPacket,
    ReviewResolution,
    RemoteCandidateObservation,
    RuntimeBinding,
    RuntimeEvent,
    RuntimeHandle,
    RuntimeRequest,
    RuntimeResult,
    SshRelayConfig,
    SupplyRecord,
    WorkGraph,
    WorkItem,
    WorkNode,
    WorkPlan,
    WorkspaceLease,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project-hermes",
        description="Operate the ProjectHermes event-driven control plane.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    init_parser = subcommands.add_parser(
        "init",
        help="Create a safe default ProjectHermes configuration.",
    )
    init_parser.add_argument(
        "--config",
        type=Path,
        default=Path("project-hermes.yaml"),
    )
    init_parser.add_argument("--force", action="store_true")

    validate_parser = subcommands.add_parser(
        "validate-config",
        help="Validate configuration and secret-file policy.",
    )
    validate_parser.add_argument("config", type=Path)

    schema_parser = subcommands.add_parser(
        "export-schemas",
        help="Export versioned JSON Schemas.",
    )
    schema_parser.add_argument("output", type=Path)

    create_parser = subcommands.add_parser(
        "create-run",
        help="Create a run from a locked issue-task.v2 contract.",
    )
    create_parser.add_argument("--config", required=True, type=Path)
    create_parser.add_argument("--task", required=True, type=Path)
    create_parser.add_argument("--run-id")

    poll_parser = subcommands.add_parser(
        "poll-now",
        help="Run the configured GitHub Issue polling task now.",
    )
    poll_parser.add_argument("--config", required=True, type=Path)

    polling_status_parser = subcommands.add_parser(
        "polling-status",
        help="Show polling, repository, candidate, and Work status.",
    )
    polling_status_parser.add_argument("--config", required=True, type=Path)

    show_parser = subcommands.add_parser(
        "show-run",
        help="Show a run, graph, and append-only events.",
    )
    show_parser.add_argument("--config", required=True, type=Path)
    show_parser.add_argument("run_id")

    english_parser = subcommands.add_parser(
        "check-english",
        help="Check ProjectHermes-owned files for untranslated prose.",
    )
    english_parser.add_argument(
        "paths",
        type=Path,
        nargs="*",
    )

    doctor_parser = subcommands.add_parser(
        "doctor",
        help="Check ProjectHermes runtime prerequisites.",
    )
    doctor_parser.add_argument("config", type=Path)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return _init_config(args.config, force=args.force)
    if args.command == "validate-config":
        return _validate_config(args.config)
    if args.command == "export-schemas":
        return _export_schemas(args.output)
    if args.command == "create-run":
        return _create_run(args.config, args.task, run_id=args.run_id)
    if args.command == "poll-now":
        return _poll_now(args.config)
    if args.command == "polling-status":
        return _polling_status(args.config)
    if args.command == "show-run":
        return _show_run(args.config, args.run_id)
    if args.command == "check-english":
        return _check_english(args.paths)
    if args.command == "doctor":
        return _doctor(args.config)
    raise AssertionError(f"unhandled command: {args.command}")


def _init_config(path: Path, *, force: bool) -> int:
    resolved = path.resolve()
    if resolved.exists() and not force:
        print(f"Refusing to overwrite existing configuration: {resolved}")
        return 2
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = ProjectHermesConfig().model_dump(mode="json")
    resolved.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )
    print(f"Created {resolved}")
    print("Codex execution remains disabled until credentials are configured.")
    return 0


def _validate_config(path: Path) -> int:
    config = load_config(path)
    print(
        json.dumps(
            {
                "ok": True,
                "schema_version": config.schema_version,
                "fingerprint": config_fingerprint(config),
                "control_plane_mode": config.control_plane.mode.value,
                "codex_enabled": config.codex.enabled,
                "codex_sdk_version": config.codex.sdk_version,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _export_schemas(output: Path) -> int:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    expected_files = {
        f"{_schema_id(model)}.schema.json" for model in _SCHEMA_MODELS
    }
    for existing in output.glob("*.schema.json"):
        if existing.name not in expected_files:
            existing.unlink()
    for model in _SCHEMA_MODELS:
        schema = model.model_json_schema()
        schema_id = _schema_id(model)
        target = output / f"{schema_id}.schema.json"
        target.write_text(
            json.dumps(
                schema,
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
            )
            + "\n",
            encoding="utf-8",
        )
    print(f"Exported {len(_SCHEMA_MODELS)} schemas to {output}")
    return 0


def _create_run(
    config_path: Path,
    task_path: Path,
    *,
    run_id: str | None,
) -> int:
    config = load_config(config_path)
    task = IssueTask.model_validate(_load_document(task_path))
    store = open_run_store(config.control_plane)
    assurance = open_assurance_store(config.control_plane)
    run = ProjectHermesController(
        store,
        assurance,
    ).create_task_run(task, run_id=run_id)
    print(run.model_dump_json(indent=2))
    return 0


def _show_run(config_path: Path, run_id: str) -> int:
    config = load_config(config_path)
    store = open_run_store(config.control_plane)
    payload = {
        "run": store.get_run(run_id).model_dump(mode="json"),
        "graph": store.load_graph(run_id).model_dump(mode="json"),
        "events": [
            event.model_dump(mode="json")
            for event in store.list_events(run_id)
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _poll_now(config_path: Path) -> int:
    config = load_config(config_path)
    store = open_polling_store(config.polling)
    client = GitHubClient(
        max_retries=config.polling.github_max_retries,
        timeout_seconds=config.polling.github_request_timeout_seconds,
    )
    result = IssuePollingService(client, store, config.polling).run_now()
    print(result.model_dump_json(indent=2))
    return 0


def _polling_status(config_path: Path) -> int:
    config = load_config(config_path)
    store = open_polling_store(config.polling)
    payload = store.overview()
    latest = store.latest_run()
    payload["repositories"] = [
        item.model_dump(mode="json") for item in store.list_repositories()
    ]
    payload["latest_run_repositories"] = (
        [
            item.model_dump(mode="json")
            for item in store.list_run_repositories(latest.run_id)
        ]
        if latest is not None
        else []
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _check_english(paths: list[Path]) -> int:
    if not paths:
        repository_root = Path(__file__).resolve().parents[1]
        paths = [
            repository_root / "project_hermes",
            repository_root / "docs" / "project-hermes",
            repository_root / "schemas" / "project-hermes",
            repository_root / "PROJECT_HERMES.md",
            repository_root / "project-hermes.example.yaml",
        ]
    violations = scan_english_only([path.resolve() for path in paths])
    if not violations:
        print("English-only check passed.")
        return 0
    for violation in violations:
        print(
            f"{violation.path}:{violation.line}: "
            f"untranslated script: {violation.excerpt}"
        )
    return 1


def _doctor(path: Path) -> int:
    config = load_config(path)
    checks: list[dict[str, Any]] = [
        {
            "check": "configuration",
            "ok": True,
            "detail": config_fingerprint(config),
        },
        {
            "check": "git",
            "ok": shutil.which("git") is not None,
            "detail": shutil.which("git") or "git was not found",
        },
    ]
    if config.codex.enabled:
        for distribution in ("openai-codex", "openai-codex-cli-bin"):
            try:
                installed = importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError:
                installed = None
            checks.append(
                {
                    "check": distribution,
                    "ok": installed == PINNED_CODEX_SDK_VERSION,
                    "detail": installed or "not installed",
                }
            )
    print(json.dumps({"checks": checks}, indent=2, sort_keys=True))
    return 0 if all(check["ok"] for check in checks) else 1


def _load_document(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _schema_id(model: type[BaseModel]) -> str:
    field = model.model_fields.get("schema_version")
    if field is not None and field.default:
        return str(field.default)
    return model.__name__.lower()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
