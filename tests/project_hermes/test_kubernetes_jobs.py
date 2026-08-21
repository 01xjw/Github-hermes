from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from project_hermes.config import KubernetesJobConfig
from project_hermes.credentials import CONTROLLER_CREDENTIAL_SECRET_FILES
from project_hermes.execution import (
    BackendExecution,
    ExecutionCoordinator,
    ExecutionRequest,
    ExecutionStatus,
    SqliteExecutionStore,
)
from project_hermes.kubernetes_jobs import (
    CodexKubernetesWorkerBackend,
    CodexWorkerMetadata,
    KubernetesJobBackend,
)
from project_hermes.resources import (
    ArtifactManifest,
    ExecutionEnvironment,
    GpuRequest,
)
from project_hermes.repository_skills import repository_skill_digest
from project_hermes.supply_chain import (
    TaskSourceBundleMetadata,
    create_task_source_bundle,
)


class FakeKubernetesApi:
    def __init__(self) -> None:
        self.jobs: dict[tuple[str, str], dict[str, Any]] = {}
        self.network_policies: dict[tuple[str, str], dict[str, Any]] = {}
        self.pods: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []
        self.deleted_network_policies: list[tuple[str, str]] = []
        self.create_job_error: Exception | None = None

    def create_job(
        self,
        namespace: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        if self.create_job_error is not None:
            raise self.create_job_error
        name = manifest["metadata"]["name"]
        self.jobs[(namespace, name)] = manifest
        return manifest

    def get_job(
        self,
        namespace: str,
        name: str,
    ) -> dict[str, Any] | None:
        return self.jobs.get((namespace, name))

    def list_job_pods(
        self,
        namespace: str,
        name: str,
    ) -> list[dict[str, Any]]:
        del namespace, name
        return self.pods

    def delete_job(self, namespace: str, name: str) -> bool:
        self.deleted.append((namespace, name))
        return self.jobs.pop((namespace, name), None) is not None

    def create_network_policy(
        self,
        namespace: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        name = manifest["metadata"]["name"]
        self.network_policies[(namespace, name)] = manifest
        return manifest

    def get_network_policy(
        self,
        namespace: str,
        name: str,
    ) -> dict[str, Any] | None:
        return self.network_policies.get((namespace, name))

    def delete_network_policy(self, namespace: str, name: str) -> bool:
        self.deleted_network_policies.append((namespace, name))
        return self.network_policies.pop((namespace, name), None) is not None


def _request(*, gpu_count: int = 1) -> ExecutionRequest:
    image_digest = "sha256:" + "a" * 64
    return ExecutionRequest(
        request_id="request-1",
        task_id="task-1",
        run_id="run-1",
        node_id="validate",
        repository="acme/kernel",
        workspace_lease_id="workspace-1",
        candidate_digest="b" * 64,
        command=["python", "-c", "print('ok')"],
        environment=ExecutionEnvironment.KUBERNETES,
        image_repository="rocm/pytorch",
        image_digest=image_digest,
        gpu=(
            GpuRequest(
                request_id="gpu-request-1",
                task_id="task-1",
                count=gpu_count,
                architecture="gfx1100",
                image_digest=image_digest,
            )
            if gpu_count
            else None
        ),
        timeout_seconds=600,
        network_access=False,
        idempotency_key="task-1-validation",
    )


def _backend(
    api: FakeKubernetesApi,
    **overrides: Any,
) -> KubernetesJobBackend:
    values: dict[str, Any] = {
        "enabled": True,
        "node_name": "wx-k8s-test-s-002",
    }
    values.update(overrides)
    return KubernetesJobBackend(
        KubernetesJobConfig(**values),
        api=api,
    )


def test_job_manifest_is_digest_pinned_same_node_and_resource_bounded() -> None:
    api = FakeKubernetesApi()
    backend = _backend(api)

    execution = backend.start(
        "execution-1",
        _request(),
        gpu_ids=("logical-gpu-0",),
        artifacts=(),
    )

    namespace, name = execution.native_id.split("/", 1)
    manifest = api.jobs[(namespace, name)]
    pod = manifest["spec"]["template"]
    container = pod["spec"]["containers"][0]
    assert container["image"] == ("docker.io/rocm/pytorch@sha256:" + "a" * 64)
    assert pod["spec"]["nodeSelector"] == {
        "kubernetes.io/hostname": "wx-k8s-test-s-002",
        "amd.com/gpu.arch": "gfx1100",
    }
    assert container["resources"]["limits"]["amd.com/gpu"] == "1"
    assert container["securityContext"]["privileged"] is False
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert pod["metadata"]["labels"]["project-hermes.io/network-access"] == "denied"
    environment = {item["name"]: item["value"] for item in container["env"]}
    assert environment["HERMES_KANBAN_TASK"] == "task-1"
    assert environment["HERMES_KANBAN_RUN_ID"] == "run-1"


def test_job_backend_rejects_unapproved_image_repository() -> None:
    api = FakeKubernetesApi()
    backend = _backend(api)
    request = _request().model_copy(
        update={"image_repository": "untrusted.example/unknown/image"}
    )

    with pytest.raises(PermissionError, match="not allowed"):
        backend.start(
            "execution-2",
            ExecutionRequest.model_validate(request.model_dump()),
            gpu_ids=("logical-gpu-0",),
            artifacts=(),
        )

    assert not api.jobs


def test_job_observation_requires_terminal_job_condition() -> None:
    api = FakeKubernetesApi()
    backend = _backend(api)
    execution = backend.start(
        "execution-3",
        _request(gpu_count=0),
        gpu_ids=(),
        artifacts=(),
    )
    namespace, name = execution.native_id.split("/", 1)
    api.jobs[(namespace, name)]["status"] = {
        "succeeded": 1,
        "startTime": "2026-08-13T00:00:00Z",
        "completionTime": "2026-08-13T00:00:12Z",
        "conditions": [
            {
                "type": "Complete",
                "status": "True",
                "reason": "CompletionsReached",
            }
        ],
    }
    api.pods = [
        {
            "status": {
                "containerStatuses": [
                    {
                        "state": {
                            "terminated": {
                                "exitCode": 0,
                            }
                        }
                    }
                ]
            }
        }
    ]

    observation = backend.observe(execution)

    assert observation.status is ExecutionStatus.SUCCEEDED
    assert observation.terminated
    assert observation.exit_code == 0
    assert observation.duration_ms == 12_000


def test_job_cancel_waits_for_foreground_deletion() -> None:
    api = FakeKubernetesApi()
    backend = _backend(api)
    execution = backend.start(
        "execution-4",
        _request(gpu_count=0),
        gpu_ids=(),
        artifacts=(),
    )

    backend.cancel(execution)
    observation = backend.observe(
        BackendExecution.model_validate(execution.model_dump())
    )

    assert api.deleted == [tuple(execution.native_id.split("/", 1))]
    assert observation.status is ExecutionStatus.CANCELLED
    assert observation.terminated


def _codex_backend(
    tmp_path: Path,
    api: FakeKubernetesApi,
    **config_overrides: Any,
) -> tuple[
    CodexKubernetesWorkerBackend,
    ExecutionRequest,
    ArtifactManifest,
    str,
]:
    image_digest = "sha256:" + "a" * 64
    image = f"docker.io/library/python@{image_digest}"
    release_root = tmp_path / "release"
    for relative in (
        "wheelhouse/requirements.lock",
        "worker/prepare-runtime.sh",
        "worker/prepare-source.py",
        "worker/run.sh",
        "worker/execute-task.py",
    ):
        path = release_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    release_manifest = {
        "schema_version": "project-hermes-release.v1",
        "runtime_image": image,
        "codex": {
            "sdk_version": "0.144.4",
            "cli_version": "0.144.4",
        },
    }
    encoded = (
        json.dumps(
            release_manifest,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    release_digest = hashlib.sha256(encoded).hexdigest()
    (release_root / "release-manifest.json").write_bytes(encoded)
    (release_root / "RELEASE_DIGEST").write_text(
        release_digest + "\n",
        encoding="ascii",
    )
    repository_skill = (
        release_root
        / "source"
        / "project_hermes"
        / "repository_skills"
        / "solve-acme-kernel"
    )
    (repository_skill / "agents").mkdir(parents=True)
    (repository_skill / "SKILL.md").write_text(
        "---\n"
        "name: solve-acme-kernel\n"
        "description: Resolve Acme kernel Issues.\n"
        "---\n\n"
        "# Acme Kernel Skill\n\nValidate the focused kernel test.\n",
        encoding="utf-8",
    )
    (repository_skill / "agents" / "openai.yaml").write_text(
        "interface:\n"
        '  display_name: "Acme Kernel Solver"\n'
        '  short_description: "Resolve Acme kernel Issues safely"\n'
        '  default_prompt: "Use $solve-acme-kernel for this Issue."\n',
        encoding="utf-8",
    )

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "kernel.py").write_text("VALUE = 1\n", encoding="utf-8")
    source_root = tmp_path / "source-bundles"
    source_metadata = TaskSourceBundleMetadata(
        task_id="task-issue-1",
        repository="acme/kernel",
        workspace_lease_id="workspace-issue-1",
        base_sha="c" * 40,
        candidate_digest="b" * 64,
    )
    artifact = create_task_source_bundle(
        worktree,
        source_root,
        request_id="source-issue-1",
        metadata=source_metadata,
    )
    worker = CodexWorkerMetadata(
        release_digest=release_digest,
        source_bundle_request_id=artifact.request_id,
        source_bundle_digest=artifact.resolved_digest,
        base_sha=source_metadata.base_sha,
        model_profile="codex-primary",
        model="deepseek-v4-pro",
        model_provider="deepseek",
        provider_endpoint="https://api.deepseek.com/v1",
        provider_api_key_env="DEEPSEEK_API_KEY",
        provider_wire_api="responses",
        context_window=1_000_000,
        handoff_model_profile="hermes",
        handoff_model="deepseek-v4-pro",
        handoff_model_provider="deepseek",
        handoff_provider_endpoint="https://api.deepseek.com/v1",
        handoff_provider_api_key_env="DEEPSEEK_API_KEY",
        handoff_provider_wire_api="chat",
        repository_skill_name="solve-acme-kernel",
        repository_skill_digest=repository_skill_digest(repository_skill.resolve()),
    )
    request = ExecutionRequest(
        request_id="request-issue-1",
        task_id="task-issue-1",
        run_id="run-issue-1",
        node_id="implement",
        repository="acme/kernel",
        workspace_lease_id="workspace-issue-1",
        candidate_digest="b" * 64,
        command=["codex", "exec", "--json", "implement task"],
        environment=ExecutionEnvironment.KUBERNETES,
        image_repository="python",
        image_digest=image_digest,
        artifact_request_ids=[artifact.request_id],
        network_access=False,
        idempotency_key="task-issue-1-codex",
        metadata={"codex_worker": worker.model_dump(mode="json")},
    )
    config_values: dict[str, Any] = {
        "enabled": True,
        "node_name": "wx-k8s-test-s-002",
        "release_root": release_root,
        "source_bundle_root": source_root,
        "artifact_archive_root": tmp_path / "worker-artifacts",
    }
    config_values.update(config_overrides)
    config = KubernetesJobConfig(**config_values)
    return (
        CodexKubernetesWorkerBackend(
            config,
            release_digest=release_digest,
            api=api,
        ),
        request,
        artifact,
        release_digest,
    )


def _start_codex_worker(
    tmp_path: Path,
    api: FakeKubernetesApi,
    *,
    execution_id: str = "execution-codex-1",
) -> tuple[
    CodexKubernetesWorkerBackend,
    BackendExecution,
    ExecutionRequest,
    ArtifactManifest,
]:
    backend, request, artifact, _ = _codex_backend(tmp_path, api)
    execution = backend.start(
        execution_id,
        request,
        gpu_ids=(),
        artifacts=(artifact,),
    )
    return backend, execution, request, artifact


def _upgraded_codex_backend(
    tmp_path: Path,
    api: FakeKubernetesApi,
    previous: CodexKubernetesWorkerBackend,
) -> tuple[CodexKubernetesWorkerBackend, str]:
    release_root = tmp_path / "release-upgraded"
    shutil.copytree(previous.config.release_root, release_root)
    manifest_path = release_root / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["fixture_generation"] = "upgraded-controller"
    encoded = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    release_digest = hashlib.sha256(encoded).hexdigest()
    manifest_path.write_bytes(encoded)
    (release_root / "RELEASE_DIGEST").write_text(
        release_digest + "\n",
        encoding="ascii",
    )
    config = KubernetesJobConfig.model_validate({
        **previous.config.model_dump(),
        "release_root": release_root,
    })
    return (
        CodexKubernetesWorkerBackend(
            config,
            release_digest=release_digest,
            api=api,
        ),
        release_digest,
    )


def _finish_job(
    api: FakeKubernetesApi,
    execution: BackendExecution,
    *,
    failed: bool = False,
    exit_code: int = 0,
) -> None:
    namespace, name = execution.native_id.split("/", 1)
    condition = "Failed" if failed else "Complete"
    api.jobs[(namespace, name)]["status"] = {
        "failed" if failed else "succeeded": 1,
        "startTime": "2026-08-13T00:00:00Z",
        "completionTime": "2026-08-13T00:00:12Z",
        "conditions": [
            {
                "type": condition,
                "status": "True",
                "reason": "FixtureComplete",
                "lastTransitionTime": "2026-08-13T00:00:12Z",
            }
        ],
    }
    api.pods = [
        {
            "status": {
                "containerStatuses": [
                    {
                        "name": "codex",
                        "state": {
                            "terminated": {
                                "exitCode": exit_code,
                            }
                        },
                    }
                ]
            }
        }
    ]


def _write_worker_archive(
    tmp_path: Path,
    backend: CodexKubernetesWorkerBackend,
    execution: BackendExecution,
    request: ExecutionRequest,
    artifact: ArtifactManifest,
    *,
    exit_code: int = 0,
    worker_event: dict[str, object] | None = None,
    worker_events: tuple[dict[str, object], ...] = (),
) -> Path:
    source_root = tmp_path / f"prepared-{execution.execution_id}"
    (source_root / "repo").mkdir(parents=True)
    output_root = tmp_path / f"outputs-{execution.execution_id}"
    archive_root = (
        backend.config.artifact_archive_root
        / "execution-scopes"
        / execution.execution_id
    )
    script = (
        Path(__file__).resolve().parents[2] / "deploy/release-worker/execute-task.py"
    )
    if worker_event is not None and worker_events:
        raise ValueError("provide worker_event or worker_events, not both")
    events = (worker_event,) if worker_event is not None else worker_events
    event_output = "".join(f"print({json.dumps(event)!r}); " for event in events)
    child = (
        "import json, os, sys; from pathlib import Path; "
        "assert 'DEEPSEEK_API_KEY' not in os.environ; "
        "print(os.environ['MODEL_ACCESS_KEY']); "
        "print('fixture-handoff-not-a-live-key'); "
        + event_output
        + "Path(os.environ['PROJECT_HERMES_OUTPUT_ROOT'], "
        "'result.json').write_text(json.dumps({'fixture': True})); "
        f"raise SystemExit({exit_code})"
    )
    worker = CodexWorkerMetadata.model_validate(request.metadata["codex_worker"])
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PROJECT_HERMES_EXECUTION_ID": execution.execution_id,
        "PROJECT_HERMES_TASK_ID": request.task_id,
        "PROJECT_HERMES_REPOSITORY": request.repository,
        "PROJECT_HERMES_RELEASE_DIGEST": backend.release_digest,
        "PROJECT_HERMES_RELEASE_ROOT": str(backend.config.release_root),
        "PROJECT_HERMES_IMAGE_DIGEST": request.image_digest,
        "PROJECT_HERMES_SOURCE_BUNDLE_DIGEST": (artifact.resolved_digest),
        "PROJECT_HERMES_PROVIDER_ENDPOINT": "https://inference.do-ai.run/v1",
        "PROJECT_HERMES_MODEL": "deepseek-v4-pro",
        "PROJECT_HERMES_MODEL_PROVIDER": "digitalocean",
        "PROJECT_HERMES_PROVIDER_API_KEY_ENV": "MODEL_ACCESS_KEY",
        "PROJECT_HERMES_PROVIDER_WIRE_API": "responses",
        "PROJECT_HERMES_MODEL_CONTEXT_WINDOW": "1000000",
        "PROJECT_HERMES_CODEX_REQUEST_MAX_RETRIES": "12",
        "PROJECT_HERMES_CODEX_STREAM_MAX_RETRIES": "8",
        "PROJECT_HERMES_CODEX_CAPACITY_RESUME_ATTEMPTS": "8",
        "PROJECT_HERMES_CODEX_CAPACITY_HANDOFF_AFTER_ATTEMPTS": "2",
        "PROJECT_HERMES_CODEX_CAPACITY_BACKOFF_SECONDS": "30",
        "PROJECT_HERMES_HANDOFF_MODEL": "deepseek-v4-pro",
        "PROJECT_HERMES_HANDOFF_MODEL_PROVIDER": "deepseek",
        "PROJECT_HERMES_HANDOFF_PROVIDER_ENDPOINT": "https://api.deepseek.com/v1",
        "PROJECT_HERMES_HANDOFF_PROVIDER_API_KEY_ENV": "DEEPSEEK_API_KEY",
        "PROJECT_HERMES_HANDOFF_PROVIDER_WIRE_API": "chat",
        "PROJECT_HERMES_SOURCE_ROOT": str(source_root),
        "PROJECT_HERMES_OUTPUT_ROOT": str(output_root),
        "PROJECT_HERMES_RESULT_PATH": str(output_root / "result.json"),
        "PROJECT_HERMES_ARTIFACT_ARCHIVE_ROOT": str(archive_root),
        "CODEX_HOME": str(tmp_path / f"codex-{execution.execution_id}"),
        "MODEL_ACCESS_KEY": "fixture-not-a-live-key",
        "DEEPSEEK_API_KEY": "fixture-handoff-not-a-live-key",
        "HTTPS_PROXY": "http://project-hermes-model-proxy:3128",
    }
    if worker.repository_skill_name is not None:
        env["PROJECT_HERMES_REPOSITORY_SKILL"] = worker.repository_skill_name
        env["PROJECT_HERMES_REPOSITORY_SKILL_DIGEST"] = (
            worker.repository_skill_digest or ""
        )
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--",
            sys.executable,
            "-c",
            child,
            "--output-last-message",
            str(output_root / "result.json"),
            "fixture worker prompt",
        ],
        env=env,
        check=False,
    )
    assert completed.returncode == exit_code
    return archive_root


def test_codex_job_uses_offline_runtime_secret_and_task_scoped_mounts(
    tmp_path: Path,
) -> None:
    api = FakeKubernetesApi()
    backend, execution, request, artifact = _start_codex_worker(
        tmp_path,
        api,
    )
    namespace, name = execution.native_id.split("/", 1)
    job = api.jobs[(namespace, name)]
    pod_spec = job["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]

    assert "serviceAccountName" not in pod_spec
    assert pod_spec["automountServiceAccountToken"] is False
    assert job["spec"]["ttlSecondsAfterFinished"] == (
        backend.config.ttl_seconds_after_finished
    )
    assert [item["name"] for item in pod_spec["initContainers"]] == [
        "prepare-runtime",
        "prepare-source",
    ]
    assert all(
        item["image"] == f"docker.io/library/python@{request.image_digest}"
        for item in [*pod_spec["initContainers"], container]
    )
    prepare_runtime = pod_spec["initContainers"][0]
    assert prepare_runtime["command"] == [
        "/bin/bash",
        "/release/worker/prepare-runtime.sh",
    ]
    assert "/release/worker/execute-task.py" in container["command"]
    environment = {item["name"]: item for item in container["env"]}
    assert "MODEL_ACCESS_KEY" not in environment
    assert environment["DEEPSEEK_API_KEY"]["valueFrom"] == {
        "secretKeyRef": {
            "name": "project-hermes-credentials",
            "key": "deepseek-api-key",
        }
    }
    assert environment["PROJECT_HERMES_PROVIDER_ENDPOINT"]["value"] == (
        "https://api.deepseek.com/v1"
    )
    assert environment["PROJECT_HERMES_MODEL_PROVIDER"]["value"] == "deepseek"
    assert environment["PROJECT_HERMES_PROVIDER_API_KEY_ENV"]["value"] == (
        "DEEPSEEK_API_KEY"
    )
    assert environment["PROJECT_HERMES_PROVIDER_WIRE_API"]["value"] == "responses"
    assert "PROJECT_HERMES_MODEL_REASONING_EFFORT" not in environment
    assert environment["PROJECT_HERMES_MODEL_CONTEXT_WINDOW"]["value"] == "1000000"
    assert environment["PROJECT_HERMES_CODEX_REQUEST_MAX_RETRIES"]["value"] == str(
        backend.config.codex_request_max_retries
    )
    assert environment["PROJECT_HERMES_CODEX_STREAM_MAX_RETRIES"]["value"] == str(
        backend.config.codex_stream_max_retries
    )
    assert environment["PROJECT_HERMES_CODEX_CAPACITY_RESUME_ATTEMPTS"]["value"] == str(
        backend.config.codex_capacity_resume_attempts
    )
    assert environment["PROJECT_HERMES_CODEX_CAPACITY_HANDOFF_AFTER_ATTEMPTS"][
        "value"
    ] == str(backend.config.codex_capacity_handoff_after_attempts)
    assert environment["PROJECT_HERMES_CODEX_CAPACITY_BACKOFF_SECONDS"]["value"] == str(
        backend.config.codex_capacity_backoff_seconds
    )
    assert environment["PROJECT_HERMES_HANDOFF_MODEL"]["value"] == ("deepseek-v4-pro")
    assert environment["PROJECT_HERMES_HANDOFF_MODEL_PROVIDER"]["value"] == (
        "deepseek"
    )
    assert environment["PROJECT_HERMES_HANDOFF_PROVIDER_ENDPOINT"]["value"] == (
        "https://api.deepseek.com/v1"
    )
    assert environment["PROJECT_HERMES_HANDOFF_PROVIDER_API_KEY_ENV"]["value"] == (
        "DEEPSEEK_API_KEY"
    )
    assert environment["PROJECT_HERMES_HANDOFF_PROVIDER_WIRE_API"]["value"] == "chat"
    assert environment["PROJECT_HERMES_REPOSITORY_SKILL"]["value"] == (
        "solve-acme-kernel"
    )
    assert len(environment["PROJECT_HERMES_REPOSITORY_SKILL_DIGEST"]["value"]) == 64
    assert environment["HTTPS_PROXY"]["value"].endswith("project-hermes-jobs.svc:3128")
    assert not any(name.startswith(("GITHUB_", "GH_")) for name in environment)
    source_mount = next(
        mount
        for mount in pod_spec["initContainers"][1]["volumeMounts"]
        if mount["mountPath"] == "/bundle/source.tar.gz"
    )
    assert source_mount["subPath"].endswith(artifact.local_path)
    archive_mount = next(
        mount
        for mount in container["volumeMounts"]
        if mount["mountPath"] == "/artifacts"
    )
    assert archive_mount["subPath"].endswith(
        f"execution-scopes/{execution.execution_id}"
    )
    assert source_mount["name"] == "release"
    assert archive_mount["name"] == "release"
    assert [
        volume for volume in pod_spec["volumes"] if "persistentVolumeClaim" in volume
    ] == [
        {
            "name": "release",
            "persistentVolumeClaim": {
                "claimName": "github-agent-workspace",
            },
        }
    ]
    tmp_volume = next(
        volume for volume in pod_spec["volumes"] if volume["name"] == "tmp"
    )
    assert tmp_volume["emptyDir"]["sizeLimit"] == (backend.config.worker_tmp_size_limit)
    assert any(
        mount["name"] == "tmp" and mount["mountPath"] == "/tmp"
        for mount in container["volumeMounts"]
    )

    policy = api.network_policies[(namespace, f"{name}-egress")]
    egress = policy["spec"]["egress"]
    assert len(egress) == 2
    assert not any("ipBlock" in target for rule in egress for target in rule["to"])
    assert egress[1]["to"][0]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "project-hermes-model-proxy"
    }


def test_codex_worker_rejects_provider_secret_key_mismatch(
    tmp_path: Path,
) -> None:
    api = FakeKubernetesApi()
    backend, request, artifact, _ = _codex_backend(
        tmp_path,
        api,
        model_secret_key="model-access-key",
    )

    with pytest.raises(
        ValueError,
        match="Secret key does not match provider_api_key_env",
    ):
        backend.start(
            "execution-secret-mismatch",
            request,
            gpu_ids=(),
            artifacts=(artifact,),
        )

    assert not api.jobs


def test_codex_worker_installs_only_its_locked_skill_in_fresh_home(
    tmp_path: Path,
) -> None:
    api = FakeKubernetesApi()
    backend, execution, request, artifact = _start_codex_worker(tmp_path, api)

    _write_worker_archive(
        tmp_path,
        backend,
        execution,
        request,
        artifact,
    )

    codex_home = tmp_path / f"codex-{execution.execution_id}"
    skill_names = sorted(path.name for path in (codex_home / "skills").iterdir())
    assert skill_names == ["solve-acme-kernel"]
    assert (codex_home / "skills" / skill_names[0] / "SKILL.md").is_file()
    assert (codex_home / "config.toml").is_file()


def test_codex_worker_metadata_requires_complete_skill_identity(
    tmp_path: Path,
) -> None:
    api = FakeKubernetesApi()
    _backend_value, request, _artifact, _digest = _codex_backend(tmp_path, api)
    raw = request.metadata["codex_worker"]
    raw.pop("repository_skill_digest")

    with pytest.raises(ValueError, match="supplied together"):
        CodexWorkerMetadata.model_validate(raw)


def test_codex_worker_metadata_rejects_partial_handoff_route(tmp_path: Path) -> None:
    api = FakeKubernetesApi()
    _backend_value, request, _artifact, _digest = _codex_backend(tmp_path, api)
    raw = request.metadata["codex_worker"]
    raw.pop("handoff_provider_endpoint")

    with pytest.raises(ValueError, match="supplied together"):
        CodexWorkerMetadata.model_validate(raw)


def test_codex_worker_metadata_rejects_unapproved_handoff_route(
    tmp_path: Path,
) -> None:
    api = FakeKubernetesApi()
    _backend_value, request, _artifact, _digest = _codex_backend(tmp_path, api)
    raw = request.metadata["codex_worker"]
    raw["handoff_provider_endpoint"] = "https://models.example.test/v1"

    with pytest.raises(ValueError, match="handoff model route is not allowlisted"):
        CodexWorkerMetadata.model_validate(raw)


def test_codex_job_keeps_distinct_pvc_volumes(tmp_path: Path) -> None:
    api = FakeKubernetesApi()
    backend, request, artifact, _ = _codex_backend(
        tmp_path,
        api,
        release_pvc_name="release-pvc",
        source_bundle_pvc_name="source-pvc",
        artifact_archive_pvc_name="artifacts-pvc",
    )
    execution = backend.start(
        "execution-distinct-pvcs",
        request,
        gpu_ids=(),
        artifacts=(artifact,),
    )
    namespace, name = execution.native_id.split("/", 1)
    pod_spec = api.jobs[(namespace, name)]["spec"]["template"]["spec"]

    pvc_volumes = {
        volume["name"]: volume["persistentVolumeClaim"]["claimName"]
        for volume in pod_spec["volumes"]
        if "persistentVolumeClaim" in volume
    }
    assert pvc_volumes == {
        "release": "release-pvc",
        "source-bundle": "source-pvc",
        "artifacts": "artifacts-pvc",
    }
    source_mount = next(
        mount
        for mount in pod_spec["initContainers"][1]["volumeMounts"]
        if mount["mountPath"] == "/bundle/source.tar.gz"
    )
    archive_mount = next(
        mount
        for mount in pod_spec["containers"][0]["volumeMounts"]
        if mount["mountPath"] == "/artifacts"
    )
    assert source_mount["name"] == "source-bundle"
    assert archive_mount["name"] == "artifacts"


def test_codex_start_failure_reclaims_network_policy(tmp_path: Path) -> None:
    api = FakeKubernetesApi()
    backend, request, artifact, _release_digest = _codex_backend(tmp_path, api)
    api.create_job_error = RuntimeError("Job admission failed")

    with pytest.raises(RuntimeError, match="Job admission failed"):
        backend.start(
            "execution-failed-start",
            request,
            gpu_ids=(),
            artifacts=(artifact,),
        )

    assert not api.jobs
    assert not api.network_policies
    assert len(api.deleted_network_policies) == 1


def test_source_init_verifies_bundle_and_creates_clean_worktree(
    tmp_path: Path,
) -> None:
    api = FakeKubernetesApi()
    backend, request, artifact, _ = _codex_backend(tmp_path, api)
    destination = tmp_path / "prepared-source"
    script = (
        Path(__file__).resolve().parents[2] / "deploy/release-worker/prepare-source.py"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PROJECT_HERMES_SOURCE_BUNDLE": str(
            backend.config.source_bundle_root / artifact.local_path
        ),
        "PROJECT_HERMES_SOURCE_BUNDLE_DIGEST": artifact.resolved_digest,
        "PROJECT_HERMES_SOURCE_ROOT": str(destination),
        "PROJECT_HERMES_TASK_ID": request.task_id,
        "PROJECT_HERMES_REPOSITORY": request.repository,
        "PROJECT_HERMES_WORKSPACE_LEASE_ID": request.workspace_lease_id,
        "PROJECT_HERMES_BASE_SHA": "c" * 40,
        "PROJECT_HERMES_CANDIDATE_DIGEST": request.candidate_digest,
    }

    subprocess.run([sys.executable, str(script)], env=env, check=True)

    repository = destination / "repo"
    assert (repository / ".git").is_dir()
    assert (repository / "kernel.py").read_text(encoding="utf-8") == ("VALUE = 1\n")
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    assert status.stdout == ""


@pytest.mark.parametrize(
    ("exit_code", "expected_status"), [(0, "SUCCEEDED"), (3, "FAILED")]
)
def test_codex_worker_archive_is_verified_before_job_deletion(
    tmp_path: Path,
    exit_code: int,
    expected_status: str,
) -> None:
    api = FakeKubernetesApi()
    backend, execution, request, artifact = _start_codex_worker(
        tmp_path,
        api,
        execution_id=f"execution-codex-{exit_code}",
    )
    archive_root = _write_worker_archive(
        tmp_path,
        backend,
        execution,
        request,
        artifact,
        exit_code=exit_code,
    )
    _finish_job(
        api,
        execution,
        failed=exit_code != 0,
        exit_code=exit_code,
    )
    archived_manifest = json.loads(
        (
            archive_root / "executions" / execution.execution_id / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    for secret in (
        b"fixture-not-a-live-key",
        b"fixture-handoff-not-a-live-key",
    ):
        assert all(
            secret not in (archive_root / entry["object_path"]).read_bytes()
            for entry in archived_manifest["entries"]
        )
    codex_config = (
        tmp_path / f"codex-{execution.execution_id}" / "config.toml"
    ).read_text(encoding="utf-8")
    assert 'sandbox_mode = "danger-full-access"' in codex_config
    assert "https://inference.do-ai.run/v1" in codex_config
    assert 'env_key = "MODEL_ACCESS_KEY"' in codex_config
    assert "model_reasoning_effort" not in codex_config
    assert "model_context_window = 1000000" in codex_config
    assert "request_max_retries = 12" in codex_config
    assert "stream_max_retries = 8" in codex_config
    assert "fixture-not-a-live-key" not in codex_config

    observation = backend.observe(execution)

    assert observation.status.value == expected_status
    assert observation.result["archive_verified"] is True
    assert not api.deleted
    archive = backend.delete_verified(execution)
    assert archive.exit_code == exit_code
    assert api.deleted == [tuple(execution.native_id.split("/", 1))]
    assert api.deleted_network_policies
    assert (archive_root / "executions" / execution.execution_id).is_dir()


def test_old_release_worker_is_reconciled_and_deleted_after_controller_upgrade(
    tmp_path: Path,
) -> None:
    api = FakeKubernetesApi()
    old_backend, execution, request, artifact = _start_codex_worker(
        tmp_path,
        api,
        execution_id="execution-before-controller-upgrade",
    )
    _write_worker_archive(
        tmp_path,
        old_backend,
        execution,
        request,
        artifact,
    )
    _finish_job(api, execution)
    new_backend, new_release_digest = _upgraded_codex_backend(
        tmp_path,
        api,
        old_backend,
    )

    observation = new_backend.observe(execution)

    assert old_backend.release_digest != new_release_digest
    assert observation.status is ExecutionStatus.SUCCEEDED
    assert observation.result["archive_verified"] is True
    assert observation.environment["release_digest"] == (old_backend.release_digest)
    assert observation.environment["release_digest"] != (new_backend.release_digest)
    archive = new_backend.delete_verified(execution)
    assert archive.release_digest == old_backend.release_digest
    assert api.deleted == [tuple(execution.native_id.split("/", 1))]


@pytest.mark.parametrize(
    "tamper",
    [
        "job-execution",
        "job-release-format",
        "job-release-identity",
        "archive-identity",
    ],
)
def test_cross_release_archive_identity_tampering_fails_closed(
    tmp_path: Path,
    tamper: str,
) -> None:
    api = FakeKubernetesApi()
    old_backend, execution, request, artifact = _start_codex_worker(
        tmp_path,
        api,
        execution_id=f"execution-upgrade-tamper-{tamper}",
    )
    archive_root = _write_worker_archive(
        tmp_path,
        old_backend,
        execution,
        request,
        artifact,
    )
    _finish_job(api, execution)
    namespace, name = execution.native_id.split("/", 1)
    annotations = api.jobs[(namespace, name)]["metadata"]["annotations"]
    other_release_digest = (
        "0" * 64 if old_backend.release_digest != "0" * 64 else "1" * 64
    )
    if tamper == "job-execution":
        annotations["project-hermes.io/execution-id"] = "execution-other"
    elif tamper == "job-release-format":
        annotations["project-hermes.io/release-digest"] = "release-a"
    elif tamper == "job-release-identity":
        annotations["project-hermes.io/release-digest"] = other_release_digest
    else:
        manifest_path = (
            archive_root / "executions" / execution.execution_id / "manifest.json"
        )
        archive_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        archive_manifest["release_digest"] = other_release_digest
        encoded = (
            json.dumps(
                archive_manifest,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        manifest_path.chmod(0o644)
        manifest_path.write_bytes(encoded)
        completion_path = manifest_path.parent / "complete"
        completion_path.chmod(0o644)
        completion_path.write_text(
            hashlib.sha256(encoded).hexdigest() + "\n",
            encoding="ascii",
        )
    new_backend, _new_release_digest = _upgraded_codex_backend(
        tmp_path,
        api,
        old_backend,
    )

    observation = new_backend.observe(execution)

    assert observation.status is ExecutionStatus.FAILED
    assert observation.result["archive_verified"] is False
    with pytest.raises(ValueError):
        new_backend.delete_verified(execution)
    assert not api.deleted


def test_codex_worker_capacity_failure_is_classified_from_final_turn(
    tmp_path: Path,
) -> None:
    api = FakeKubernetesApi()
    backend, execution, request, artifact = _start_codex_worker(
        tmp_path,
        api,
        execution_id="execution-codex-capacity",
    )
    _write_worker_archive(
        tmp_path,
        backend,
        execution,
        request,
        artifact,
        exit_code=1,
        worker_event={
            "type": "turn.failed",
            "error": {
                "message": "exceeded retry limit: 429 Too Many Requests",
            },
        },
    )
    _finish_job(api, execution, failed=True, exit_code=1)

    observation = backend.observe(execution)

    assert observation.status is ExecutionStatus.FAILED
    assert observation.result["archive_verified"] is True
    assert observation.result["failure_kind"] == "provider_capacity_exhausted"


@pytest.mark.parametrize(
    "final_event",
    [
        {"type": "turn.completed"},
        {
            "type": "turn.failed",
            "error": {"message": "sandbox command failed"},
        },
    ],
)
def test_recovered_capacity_failure_does_not_override_final_turn(
    tmp_path: Path,
    final_event: dict[str, object],
) -> None:
    api = FakeKubernetesApi()
    backend, execution, request, artifact = _start_codex_worker(
        tmp_path,
        api,
        execution_id="execution-codex-recovered-capacity",
    )
    _write_worker_archive(
        tmp_path,
        backend,
        execution,
        request,
        artifact,
        exit_code=1,
        worker_events=(
            {
                "type": "turn.failed",
                "error": {"message": "429 Too Many Requests"},
            },
            final_event,
        ),
    )
    _finish_job(api, execution, failed=True, exit_code=1)

    observation = backend.observe(execution)

    assert observation.status is ExecutionStatus.FAILED
    assert observation.result["archive_verified"] is True
    assert "failure_kind" not in observation.result


def test_worker_capacity_result_marker_survives_completed_turn(
    tmp_path: Path,
) -> None:
    api = FakeKubernetesApi()
    backend, execution, request, artifact = _start_codex_worker(
        tmp_path,
        api,
        execution_id="execution-codex-capacity-result",
    )
    _write_worker_archive(
        tmp_path,
        backend,
        execution,
        request,
        artifact,
        exit_code=1,
        worker_events=(
            {"type": "turn.completed"},
            {
                "type": "project_hermes.capacity_result_completed",
                "candidate_completed": True,
            },
        ),
    )
    _finish_job(api, execution, failed=True, exit_code=1)

    observation = backend.observe(execution)

    assert observation.status is ExecutionStatus.FAILED
    assert observation.result["archive_verified"] is True
    assert observation.result["failure_kind"] == "provider_capacity_exhausted"


def test_legacy_worker_invalid_result_after_capacity_is_retryable(
    tmp_path: Path,
) -> None:
    api = FakeKubernetesApi()
    backend, execution, request, artifact = _start_codex_worker(
        tmp_path,
        api,
        execution_id="execution-codex-legacy-capacity",
    )
    _write_worker_archive(
        tmp_path,
        backend,
        execution,
        request,
        artifact,
        exit_code=65,
        worker_events=(
            {
                "type": "turn.failed",
                "error": {"message": "429 Too Many Requests"},
            },
            {"type": "turn.completed"},
        ),
    )
    _finish_job(api, execution, failed=True, exit_code=65)

    observation = backend.observe(execution)

    assert observation.status is ExecutionStatus.FAILED
    assert observation.result["archive_verified"] is True
    assert observation.result["failure_kind"] == "provider_capacity_exhausted"


def test_tampered_worker_archive_preserves_terminal_job(
    tmp_path: Path,
) -> None:
    api = FakeKubernetesApi()
    backend, execution, request, artifact = _start_codex_worker(
        tmp_path,
        api,
    )
    archive_root = _write_worker_archive(
        tmp_path,
        backend,
        execution,
        request,
        artifact,
    )
    manifest = json.loads(
        (
            archive_root / "executions" / execution.execution_id / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    object_path = archive_root / manifest["entries"][0]["object_path"]
    object_path.chmod(0o644)
    object_path.write_bytes(b"tampered")
    _finish_job(api, execution)

    observation = backend.observe(execution)

    assert observation.status is ExecutionStatus.FAILED
    assert observation.result["archive_verified"] is False
    with pytest.raises(ValueError, match="mismatch"):
        backend.delete_verified(execution)
    assert not api.deleted


def test_execution_reconciliation_deletes_only_verified_worker(
    tmp_path: Path,
) -> None:
    api = FakeKubernetesApi()
    backend, request, artifact, _ = _codex_backend(tmp_path, api)
    coordinator = ExecutionCoordinator(
        SqliteExecutionStore(tmp_path / "executions.db"),
        backend,
        artifact_lookup=lambda request_id: (
            artifact if request_id == artifact.request_id else None
        ),
        max_jobs=8,
        max_gpus=6,
    )
    record = coordinator.submit(request)
    assert record.backend_execution is not None
    _write_worker_archive(
        tmp_path,
        backend,
        record.backend_execution,
        request,
        artifact,
    )
    _finish_job(api, record.backend_execution)

    terminal = coordinator.reconcile(record.execution_id)

    assert terminal.status is ExecutionStatus.SUCCEEDED
    assert terminal.observation is not None
    assert terminal.observation.result["archive_verified"] is True
    assert api.deleted == [tuple(record.backend_execution.native_id.split("/", 1))]


def test_proxy_manifest_is_connect_only_and_immutable() -> None:
    root = Path(__file__).resolve().parents[2]
    documents = list(
        yaml.safe_load_all(
            (root / "deploy/kubernetes/github-agent-job-runner.yaml").read_text(
                encoding="utf-8"
            )
        )
    )
    deployment = next(
        item
        for item in documents
        if item["kind"] == "Deployment"
        and item["metadata"]["name"] == "project-hermes-model-proxy"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "@PROJECT_HERMES_RUNTIME_IMAGE@"
    assert container["command"][-1] == "/release/worker/connect-proxy.py"
    assert (
        deployment["metadata"]["annotations"]["project-hermes.io/release-digest"]
        == "@PROJECT_HERMES_RELEASE_DIGEST@"
    )
    proxy_source = (root / "deploy/release-worker/connect-proxy.py").read_text(
        encoding="utf-8"
    )
    assert '"api.deepseek.com"' in proxy_source
    assert '"inference.do-ai.run"' in proxy_source
    assert container["env"][1] == {
        "name": "PROJECT_HERMES_ALLOWED_MODEL_HOSTS",
        "value": "api.deepseek.com",
    }
    assert 'method != "CONNECT"' in proxy_source
    assert 'line.lower().startswith(b"proxy-authorization:")' in proxy_source
    assert "line.casefold()" not in proxy_source
    assert "socket.getaddrinfo(" in proxy_source

    quota = next(item for item in documents if item["kind"] == "ResourceQuota")
    job_quota = int(quota["spec"]["hard"]["count/jobs.batch"])
    gpu_quota = int(quota["spec"]["hard"]["requests.amd.com/gpu"])
    assert job_quota >= (gpu_quota * 2) + 2
    assert quota["spec"]["hard"]["requests.amd.com/gpu"] == "6"
    assert quota["spec"]["hard"]["limits.amd.com/gpu"] == "6"

    runtime_installer = (root / "deploy/release-worker/prepare-runtime.sh").read_text(
        encoding="utf-8"
    )
    for locked_option in (
        "PIP_NO_INDEX=1",
        "--no-index",
        "--no-deps",
        "--only-binary=:all:",
        "--require-hashes",
    ):
        assert locked_option in runtime_installer
    assert 'expected = "0.144.4"' in runtime_installer
    assert '("openai-codex", "openai-codex-cli-bin")' in runtime_installer
    assert "from codex_cli_bin import bundled_codex_path" in runtime_installer
    assert "launcher.symlink_to(binary)" in runtime_installer
    assert '!= "codex-cli 0.144.4"' in runtime_installer


def test_worker_tmp_volume_has_bounded_pytorch_headroom() -> None:
    root = Path(__file__).resolve().parents[2]
    cluster = yaml.safe_load(
        (root / "deploy/kubernetes/project-hermes.cluster.yaml").read_text(
            encoding="utf-8"
        )
    )
    example = yaml.safe_load(
        (root / "project-hermes.example.yaml").read_text(encoding="utf-8")
    )

    assert KubernetesJobConfig().worker_tmp_size_limit == "32Gi"
    assert cluster["kubernetes_jobs"]["worker_tmp_size_limit"] == "32Gi"
    assert example["kubernetes_jobs"]["worker_tmp_size_limit"] == "32Gi"


def test_installer_secret_projection_covers_every_model_route() -> None:
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load(
        (root / "deploy/kubernetes/project-hermes.cluster.yaml").read_text(
            encoding="utf-8"
        )
    )
    hermes_routes = [
        config["hermes"]["model_profile"],
        config["hermes"]["reviewer_profile"],
        config["hermes"]["worker_handoff_profile"],
    ]
    codex_routes = list(config["codex"]["model_profiles"].values())
    required_environment_names = {
        route["provider_api_key_env"]
        for route in [*hermes_routes, *codex_routes]
    }
    secret_files_by_environment = dict(CONTROLLER_CREDENTIAL_SECRET_FILES)
    assert required_environment_names <= set(secret_files_by_environment)

    installer = yaml.safe_load(
        (root / "deploy/kubernetes/install-job.yaml").read_text(
            encoding="utf-8"
        )
    )
    secret_volume = next(
        volume
        for volume in installer["spec"]["template"]["spec"]["volumes"]
        if volume["name"] == "model-credentials"
    )
    projected_files = {
        item["path"]: item["key"]
        for item in secret_volume["secret"]["items"]
    }
    expected_files = {
        secret_file: secret_file
        for secret_file in secret_files_by_environment.values()
    }
    assert projected_files == expected_files


def test_connect_proxy_parses_byte_headers_and_rejects_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PROJECT_HERMES_ALLOWED_MODEL_HOSTS",
        "api.deepseek.com,inference.do-ai.run",
    )
    script = (
        Path(__file__).resolve().parents[2] / "deploy/release-worker/connect-proxy.py"
    )
    spec = importlib.util.spec_from_file_location(
        "project_hermes_connect_proxy_test",
        script,
    )
    assert spec is not None and spec.loader is not None
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)

    class FakeSocket:
        def __init__(self, request: bytes = b"") -> None:
            self.request = request
            self.responses: list[bytes] = []

        def recv(self, _size: int) -> bytes:
            request, self.request = self.request, b""
            return request

        def sendall(self, data: bytes) -> None:
            self.responses.append(data)

        def __enter__(self) -> "FakeSocket":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    connected_hosts: list[str] = []

    def fake_connect_upstream(host: str) -> FakeSocket:
        connected_hosts.append(host)
        return FakeSocket()

    monkeypatch.setattr(proxy, "_connect_upstream", fake_connect_upstream)
    observed_timeouts: list[int] = []
    observed_events: list[tuple[str, dict[str, object]]] = []

    def fake_select(
        _readable: object,
        _writable: object,
        _exceptional: object,
        timeout: int,
    ) -> tuple[list[object], list[object], list[object]]:
        observed_timeouts.append(timeout)
        return [], [], []

    monkeypatch.setattr(proxy.select, "select", fake_select)
    monkeypatch.setattr(
        proxy,
        "_log_proxy_event",
        lambda event, **fields: observed_events.append((event, fields)),
    )
    allowed = FakeSocket(
        b"CONNECT inference.do-ai.run:443 HTTP/1.1\r\nHost: inference.do-ai.run:443\r\n\r\n"
    )
    proxy.ConnectHandler(allowed, ("127.0.0.1", 10000), object())
    assert allowed.responses[0].startswith(b"HTTP/1.1 200 Connection Established")
    deepseek = FakeSocket(
        b"CONNECT api.deepseek.com:443 HTTP/1.1\r\nHost: api.deepseek.com:443\r\n\r\n"
    )
    proxy.ConnectHandler(deepseek, ("127.0.0.1", 10001), object())
    assert deepseek.responses[0].startswith(b"HTTP/1.1 200 Connection Established")
    assert connected_hosts == ["inference.do-ai.run", "api.deepseek.com"]
    assert observed_timeouts == [proxy.MODEL_RESPONSE_IDLE_TIMEOUT_SECONDS] * 2
    assert proxy.MODEL_RESPONSE_IDLE_TIMEOUT_SECONDS >= 600
    assert [event for event, _fields in observed_events] == [
        "tunnel_opened",
        "tunnel_idle_timeout",
        "tunnel_opened",
        "tunnel_idle_timeout",
    ]

    forbidden = FakeSocket(
        b"CONNECT inference.do-ai.run:443 HTTP/1.1\r\npRoXy-AuThOrIzAtIoN: secret\r\n\r\n"
    )
    proxy.ConnectHandler(forbidden, ("127.0.0.1", 10002), object())
    assert forbidden.responses == [b"HTTP/1.1 403 Forbidden\r\n\r\n"]
    unknown = FakeSocket(
        b"CONNECT models.example.test:443 HTTP/1.1\r\nHost: models.example.test:443\r\n\r\n"
    )
    proxy.ConnectHandler(unknown, ("127.0.0.1", 10003), object())
    assert unknown.responses == [b"HTTP/1.1 403 Forbidden\r\n\r\n"]
    assert connected_hosts == ["inference.do-ai.run", "api.deepseek.com"]
