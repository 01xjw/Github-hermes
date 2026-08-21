# ProjectHermes Contracts

ProjectHermes treats every agent, controller, runner, reviewer, and curator
boundary as untrusted input. External contracts inherit from `StrictModel`,
which rejects unknown fields, validates assignment, and strips surrounding
string whitespace.

Generated JSON Schemas live in `schemas/project-hermes/` and can be refreshed
with:

```bash
uv run project-hermes export-schemas schemas/project-hermes
```

## Versioning rules

Schema versions are explicit string constants, not inferred from package
versions. Additive optional fields may remain within a schema version when the
meaning of existing records does not change. Renamed fields, changed
invariants, or changed interpretation require a new schema version.

Consumers must:

- reject unknown major schema versions;
- validate before persistence or side effects;
- retain the original validated record for audit;
- write new records in the latest schema;
- migrate projections without rewriting append-only events.

## Locked task example

```yaml
schema_version: issue-task.v2
task_id: issue-17
campaign_id: kernel-correctness
issue_urls:
  - https://github.com/acme/kernel/issues/17
named_baseline: main@0123456789abcdef
goals:
  - path_id: correctness
    description: Repair the incorrect kernel result.
    repository: acme/kernel
    acceptance_criteria:
      - The regression test passes on the named hardware.
      - Existing supported inputs retain their behavior.
    required_completion_layers:
      - implemented
      - gate_verified
      - operator_correct
      - final_e2e
      - ci_reviewed
repositories:
  - schema_version: repo-responsibility.v1
    repository: acme/kernel
    responsibilities:
      - Own the implementation and focused tests.
    depends_on: []
    pull_request_order: 0
non_goals:
  - Rewrite unrelated kernels.
must_preserve:
  - Existing public Python APIs.
target_hardware:
  gpu_architectures:
    - gfx942
  minimum_gpu_count: 1
  topology: single-node
  environment_name: controlled-gpu-runner
permissions:
  readable_repositories:
    - acme/kernel
  writable_repositories:
    - acme/kernel
  may_request_execution: true
  may_request_review: true
  may_publish: false
  may_access_network: false
resource_limits:
  max_parallel_nodes: 4
  max_gpu_count: 1
  max_model_concurrency: 3
  max_artifact_bytes: 1073741824
triage_decision: APPROVE
triage_evidence_refs:
  - evidence://triage/issue-17
revision: 0
metadata: {}
```

An issue task is invalid unless triage approved it and supplied evidence. Goal
repositories must have matching responsibility records. Writable repositories
must also be readable.

## Goal revisions

`goal-revision.v1` proposes changes; it does not mutate a task by itself.
Pending revisions cannot claim decision provenance. Approved or rejected
revisions require both `decided_by` and `decided_at`.

Repository expansion is exact. An approved revision authorizes a new
repository only when `proposed_changes` names it under
`readable_repositories`, `repository_allowlist`, or `repositories`. Approval of
an unrelated revision cannot be reused to read an arbitrary repository.

## Work graph records

`pipeline-run.v2` is the top-level projection. `work-graph.v1` contains
`work-node.v1` nodes. `agent-event.v1` is the append-only source of operational
history.

Work nodes require:

- a globally unique node ID;
- their owning run ID;
- a generic node kind;
- a capability name;
- the role required to claim the node;
- dependencies that already exist in the graph;
- an optional idempotency key;
- an owner token and expiry together when claimed or running.

The graph validates keys, run ownership, dependency existence, and acyclicity.

`action-request.v1` is deliberately untrusted. Authorization considers the
requesting role, task ID, action, repository, path, goal revision, permission
budget, resource ownership, and terminal-operation approvals.

## Runtime records

The generic runtime surface uses:

- `runtime-request.v1`;
- `runtime-handle.v1`;
- `runtime-event.v1`;
- `runtime-result.v1`.

The task-private Codex transport uses:

- `codex-task-spec.v1`;
- `codex-command.v1`;
- `codex-reply.v1`;
- `codex-task-metadata.v1`.

The task spec is immutable for a running daemon. Changing model, provider,
endpoint, credential file, network policy, SDK version, CLI version, or timeout
requires stopping and reprovisioning the daemon.

## Resource records

Workspace, GPU, and artifact contracts are separate because they have
different ownership and release semantics.

`workspace-lease.v1` binds one task, repository, mirror, worktree, branch,
baseline commit, and owner session. The mirror and worktree must be distinct.

`gpu-request.v1` requests one to eight devices. `gpu-lease.v1` binds allocated
device IDs to one execution object. Released leases require a release
timestamp.

`artifact-request.v1` names a source and expected lowercase SHA-256 digest.
`artifact-manifest.v1` is created only after byte-level verification.
`resource-bundle.v1` rejects resources belonging to different tasks.

## Evidence and review records

`evidence-record.v3` binds an exact task, goal path, repository, base commit,
candidate diff, environment, command, and result. Performance evidence also
requires a named baseline, reference, and tolerance. It computes a stable
content hash over all source fields, and evidence IDs are immutable in the
assurance store.

`completion-matrix.v3` contains a separate layer projection for every locked
goal path and repository. A satisfied entry requires evidence IDs, candidate
provenance, and goal provenance. Blocked, failed, and not-applicable entries
require a rationale.

`review-packet.v2` freezes the absolute path, base, branch, and complete diff
including untracked files for every repository in the locked task. It also
freezes non-goals, hardware, the completion matrix, and evidence. A v1 packet
remains readable as a single-repository packet. `review-record.v3` binds a
reviewer role and session to that packet, candidate, and goal revision. An
approval requires independent evidence and cannot contain an unresolved
medium, high, or critical finding.

`review-gate.v3` requires exactly one fresh record for each mandatory role and
the same packet digest for both.
`candidate-gate-result.v3` combines that decision with repository base commits,
reviewed per-repository diff digests, the completion matrix, and
session-independence checks.

## Knowledge records

`knowledge-candidate.v1` binds extracted knowledge to source evidence and a
candidate. `knowledge-commit.v1` points to a durably stored,
content-addressed batch. `cleanup-tombstone.v2` is written in `PREPARED` state
before deletion and replaced with `COMPLETED` after deletion, while v1 remains
readable.
