"""Controller-owned GPU allocation and artifact supply implementations."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from threading import RLock
from typing import Protocol
from uuid import uuid4

from project_hermes.config import assert_private_directory
from project_hermes.models import utc_now
from project_hermes.resources import (
    ArtifactManifest,
    ArtifactRequest,
    ArtifactStatus,
    GpuAllocationStatus,
    GpuDevice,
    GpuLease,
    GpuRequest,
)


class GpuPool:
    """Atomic fixed-size GPU pool.

    GPUs are never released by a duration timer. The caller must confirm that
    the execution pod or job terminated.
    """

    def __init__(self, devices: list[str | GpuDevice]) -> None:
        if not devices:
            raise ValueError("GPU pool cannot be empty")
        if len(devices) > 8:
            raise ValueError("GPU pool cannot exceed eight devices")
        inventory = [
            device
            if isinstance(device, GpuDevice)
            else GpuDevice(gpu_id=device)
            for device in devices
        ]
        gpu_ids = [device.gpu_id for device in inventory]
        if len(gpu_ids) != len(set(gpu_ids)):
            raise ValueError("GPU ids must be unique")
        self._devices = {
            device.gpu_id: device for device in inventory
        }
        self._leases: dict[str, GpuLease] = {}
        self._lock = RLock()

    def allocate(self, request: GpuRequest, *, execution_id: str) -> GpuLease | None:
        """Allocate all requested devices or none."""

        with self._lock:
            in_use = {
                gpu_id
                for lease in self._leases.values()
                if lease.status is not GpuAllocationStatus.RELEASED
                for gpu_id in lease.gpu_ids
            }
            available = [
                device
                for device in self._devices.values()
                if device.gpu_id not in in_use
                and self._matches(device, request)
            ]
            selected = self._select_topology(available, request)
            if selected is None:
                return None
            lease = GpuLease(
                lease_id=f"gpu-lease-{uuid4().hex}",
                request_id=request.request_id,
                task_id=request.task_id,
                gpu_ids=[device.gpu_id for device in selected],
                execution_id=execution_id,
            )
            self._leases[lease.lease_id] = lease
            return lease

    def restore(self, lease: GpuLease) -> GpuLease:
        """Restore one durable non-released lease after controller restart."""

        with self._lock:
            existing = self._leases.get(lease.lease_id)
            if existing is not None:
                if existing != lease:
                    raise ValueError(
                        "durable GPU lease differs from the restored lease"
                    )
                return existing
            if lease.status is GpuAllocationStatus.RELEASED:
                self._leases[lease.lease_id] = lease
                return lease
            unknown = set(lease.gpu_ids) - set(self._devices)
            if unknown:
                raise ValueError(
                    "durable GPU lease names unknown devices: "
                    + ", ".join(sorted(unknown))
                )
            in_use = {
                gpu_id
                for current in self._leases.values()
                if current.status is not GpuAllocationStatus.RELEASED
                for gpu_id in current.gpu_ids
            }
            conflicts = set(lease.gpu_ids) & in_use
            if conflicts:
                raise ValueError(
                    "durable GPU lease conflicts with active devices: "
                    + ", ".join(sorted(conflicts))
                )
            self._leases[lease.lease_id] = lease
            return lease

    def mark_terminating(self, lease_id: str) -> GpuLease:
        with self._lock:
            lease = self._get(lease_id)
            if lease.status is GpuAllocationStatus.RELEASED:
                raise ValueError("released GPU leases cannot re-enter termination")
            updated = GpuLease.model_validate(
                lease.model_copy(
                    update={"status": GpuAllocationStatus.TERMINATING}
                ).model_dump()
            )
            self._leases[lease_id] = updated
            return updated

    def release(
        self,
        lease_id: str,
        *,
        execution_terminated: bool,
    ) -> GpuLease:
        """Release a lease only after termination is independently confirmed."""

        if not execution_terminated:
            raise PermissionError(
                "GPU release requires confirmed pod or job termination"
            )
        with self._lock:
            lease = self._get(lease_id)
            if lease.status is GpuAllocationStatus.RELEASED:
                return lease
            updated = GpuLease.model_validate(
                lease.model_copy(
                    update={
                        "status": GpuAllocationStatus.RELEASED,
                        "released_at": utc_now(),
                    }
                ).model_dump()
            )
            self._leases[lease_id] = updated
            return updated

    def available_ids(self) -> tuple[str, ...]:
        with self._lock:
            in_use = {
                gpu_id
                for lease in self._leases.values()
                if lease.status is not GpuAllocationStatus.RELEASED
                for gpu_id in lease.gpu_ids
            }
            return tuple(
                device.gpu_id
                for device in self._devices.values()
                if device.gpu_id not in in_use and device.healthy
            )

    def set_health(self, gpu_id: str, *, healthy: bool) -> GpuDevice:
        """Update controller-observed device health."""

        with self._lock:
            try:
                device = self._devices[gpu_id]
            except KeyError as exc:
                raise KeyError(f"unknown GPU: {gpu_id}") from exc
            updated = device.model_copy(update={"healthy": healthy})
            self._devices[gpu_id] = updated
            return updated

    def get(self, lease_id: str) -> GpuLease:
        with self._lock:
            return self._get(lease_id)

    def _get(self, lease_id: str) -> GpuLease:
        try:
            return self._leases[lease_id]
        except KeyError as exc:
            raise KeyError(f"unknown GPU lease: {lease_id}") from exc

    @staticmethod
    def _matches(device: GpuDevice, request: GpuRequest) -> bool:
        if not device.healthy:
            return False
        if request.architecture is not None and (
            device.architecture is None
            or device.architecture != request.architecture.casefold()
        ):
            return False
        if request.minimum_memory_mb is not None and (
            device.memory_mb is None
            or device.memory_mb < request.minimum_memory_mb
        ):
            return False
        topology = (request.topology or "").casefold()
        if topology and topology not in {
            "any",
            "single-node",
            "single_node",
        }:
            labels = {
                value.casefold() for value in device.topology_labels.values()
            }
            if topology not in labels:
                return False
        return True

    @staticmethod
    def _select_topology(
        devices: list[GpuDevice],
        request: GpuRequest,
    ) -> list[GpuDevice] | None:
        topology = (request.topology or "").casefold()
        if topology in {"single-node", "single_node"}:
            by_node: dict[str, list[GpuDevice]] = {}
            for device in devices:
                if device.node is not None:
                    by_node.setdefault(device.node, []).append(device)
            for node in sorted(by_node):
                candidates = by_node[node]
                if len(candidates) >= request.count:
                    return candidates[: request.count]
            return None
        if len(devices) < request.count:
            return None
        return devices[: request.count]


class ArtifactProvider(Protocol):
    """Supply-chain adapter controlled by the outer control plane."""

    def materialize(self, request: ArtifactRequest, staging_path: Path) -> Path:
        """Download or copy the requested artifact into ``staging_path``."""


class ArtifactCoordinator:
    """Content-verify artifacts before they become execution inputs."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        assert_private_directory(self.root)
        self._status: dict[str, ArtifactStatus] = {}
        self._manifests: dict[str, ArtifactManifest] = {}
        self._lock = RLock()

    def request(self, request: ArtifactRequest) -> None:
        with self._lock:
            if request.request_id in self._status:
                raise ValueError(
                    f"artifact request already exists: {request.request_id}"
                )
            self._status[request.request_id] = ArtifactStatus.REQUESTED

    def supply(
        self,
        request: ArtifactRequest,
        provider: ArtifactProvider,
    ) -> ArtifactManifest:
        """Materialize, hash, and atomically publish one artifact."""

        with self._lock:
            status = self._status.get(request.request_id)
            if status is None:
                raise KeyError(f"unknown artifact request: {request.request_id}")
            if status is ArtifactStatus.READY:
                return self._manifests[request.request_id]
            if status is not ArtifactStatus.REQUESTED:
                raise ValueError(
                    f"artifact request is not supplyable from {status}"
                )
            self._status[request.request_id] = ArtifactStatus.DOWNLOADING

        staging_dir = Path(
            tempfile.mkdtemp(
                prefix="artifact-",
                dir=self.root,
            )
        )
        try:
            staged = provider.materialize(request, staging_dir / "payload")
            staged = staged.resolve()
            if not staged.is_relative_to(staging_dir.resolve()):
                raise ValueError("artifact provider returned a path outside staging")
            if not staged.is_file():
                raise ValueError("artifact provider must materialize one file")
            digest, byte_size = self._digest(staged)
            if digest != request.expected_digest:
                raise ValueError(
                    f"artifact digest mismatch: expected "
                    f"{request.expected_digest}, got {digest}"
                )

            digest_hex = digest.split(":", 1)[1]
            content_dir = self.root / "sha256" / digest_hex[:2]
            assert_private_directory(content_dir)
            destination = content_dir / digest_hex
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_file():
                    raise ValueError(
                        "content-addressed artifact path is not a regular file"
                    )
                existing_digest, existing_size = self._digest(destination)
                if (
                    existing_digest != digest
                    or existing_size != byte_size
                ):
                    raise ValueError(
                        "content-addressed artifact bytes do not match "
                        "their digest path"
                    )
                staged.unlink()
            else:
                os.replace(staged, destination)
                os.chmod(destination, 0o600)
            manifest = ArtifactManifest(
                request_id=request.request_id,
                task_id=request.task_id,
                kind=request.kind,
                source_uri=request.source_uri,
                resolved_digest=digest,
                local_path=str(destination),
                byte_size=byte_size,
            )
        except Exception:
            with self._lock:
                self._status[request.request_id] = ArtifactStatus.FAILED
            raise
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

        with self._lock:
            self._status[request.request_id] = ArtifactStatus.VERIFIED
            self._manifests[request.request_id] = manifest
            self._status[request.request_id] = ArtifactStatus.READY
        return manifest

    def status(self, request_id: str) -> ArtifactStatus:
        with self._lock:
            try:
                return self._status[request_id]
            except KeyError as exc:
                raise KeyError(f"unknown artifact request: {request_id}") from exc

    def manifest(self, request_id: str) -> ArtifactManifest | None:
        with self._lock:
            return self._manifests.get(request_id)

    @staticmethod
    def _digest(path: Path) -> tuple[str, int]:
        hasher = hashlib.sha256()
        byte_size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
                byte_size += len(chunk)
        return "sha256:" + hasher.hexdigest(), byte_size
