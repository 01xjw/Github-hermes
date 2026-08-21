# Migrating to ProjectHermes

## Compatibility objective

Migration must not interrupt existing Hermes conversations or invalidate
active legacy runs. ProjectHermes is introduced as an additive package and a
new record family. Upstream Hermes remains the default reasoning engine unless
a task explicitly selects another registered runtime.

The migration separates technical capability labels from lifecycle state.
Legacy stages remain readable historical values; they are not copied into the
new lifecycle enum.

## Legacy stage interpretation

Legacy labels such as `LOCATE`, `REPRODUCE`, `PLAN`, `IMPLEMENT`, `VALIDATE`,
`LOCK`, `COMPARE`, `LEARN`, and `REPORT` become capability or event names.
They do not imply that every new task must execute them or preserve that order.

For example:

- a successful historical `LOCATE` step can become a completed
  `agent_invocation` node with capability `locate`;
- a historical `VALIDATE` step can become an `execution` node plus one or more
  evidence records;
- `LEARN` can become knowledge extraction and validation events;
- `REPORT` can become a publication or final-response event.

The new coarse lifecycle is derived from actual scheduling state:

- pending legacy work becomes `DISCOVERED` or `QUEUED`;
- an owned live step becomes `CLAIMED` or `RUNNING`;
- external waits become the matching `WAITING_*` state;
- terminal legacy outcomes map to `COMPLETED`, `BLOCKED`, `FAILED`, or
  `CANCELLED`.

## Migration sequence

### 1. Inventory and freeze contract changes

Record every legacy run schema, stage enum, status value, event format, and
terminal outcome. Do not change legacy writers during this inventory.

### 2. Install ProjectHermes additively

Install the optional group and verify the new package without enabling new run
creation:

```bash
uv sync --extra project-hermes
scripts/run_tests.sh tests/project_hermes/ -q
uv run project-hermes check-english
```

Existing `hermes`, `hermes-agent`, and `hermes-acp` entry points remain
unchanged.

### 3. Add schema-aware readers

At every read boundary, dispatch by `schema_version`. Keep the legacy decoder
and add readers for:

- `issue-task.v2`;
- `pipeline-run.v2`;
- `work-graph.v1`;
- `work-node.v1`;
- `agent-event.v1`;
- `evidence-record.v3`;
- `completion-matrix.v3`;
- `review-packet.v1` and `review-packet.v2`;
- `review-record.v3`.

Unknown versions fail closed. Readers may create a new projection, but they
must not rewrite append-only legacy history.

### 4. Backfill locked task contracts

For each eligible legacy run:

1. identify the named baseline;
2. reconstruct issue URLs and independent triage evidence;
3. split acceptance into independently verifiable goal paths;
4. record repository responsibilities and dependency order;
5. derive the least-privilege permission budget;
6. preserve non-goals and hardware requirements;
7. validate as `issue-task.v2`.

A run without independent triage evidence cannot be silently promoted. Keep it
legacy or send it back through triage.

### 5. Project legacy history

Create a `pipeline-run.v2` and a WorkGraph projection. Use deterministic
idempotency keys derived from the legacy run and step IDs so the backfill can
be retried.

Store the legacy record identifier in node payload metadata. Emit one
`migration.projected` event describing the source schema and migration tool
version.

Historical validation output may be imported as evidence only when it contains
the exact candidate, command, environment, and result required by
`evidence-record.v3`. Otherwise retain it as an external reference and leave
the completion layer pending.

### 6. Shadow new scheduling

For a bounded period, let the legacy scheduler remain authoritative while
ProjectHermes computes ready nodes and policy decisions without side effects.
Compare:

- selected runnable work;
- dependency readiness;
- lease recovery;
- terminal status;
- repository and path authorization;
- resource demand.

Investigate every difference. Do not automatically make the new scheduler
authoritative from aggregate success rates alone.

### 7. Enable new writes by cohort

Create new tasks directly as `issue-task.v2` and `pipeline-run.v2` for a small
cohort. Keep legacy readers available. Do not dual-write two authoritative
event streams; choose one source of truth and derive compatibility projections
from it.

### 8. Move resource authority

Transfer worktree, artifact, GPU, and execution allocation to the outer control
plane before allowing autonomous Codex writes. A runtime may request resources
but cannot own allocation records.

### 9. Enforce closure gates

Enable candidate-digest invalidation, the completion matrix, and both mandatory
reviews. Legacy terminal status alone must no longer close a migrated v2 task.

### 10. Enable knowledge-first cleanup

Route terminal outcomes through `KnowledgeLifecycle`. Retain legacy cleanup
jobs only for legacy runs. Confirm tombstones and durable knowledge probes
before removing the old cleanup path.

## Codex migration

Do not attach multiple tasks to one Codex app-server or state directory.
Provision a new private daemon per task.

If a legacy task has a known Codex thread ID, import it only when:

- the task and worktree identity are proven;
- the thread belongs to the same provider and account boundary;
- its state is copied into the task-private Codex home;
- resume succeeds before new work is accepted.

Otherwise start a new thread and preserve the legacy transcript as evidence
context. Never guess a thread mapping.

## Data migration properties

Migration code should be:

- idempotent;
- append-only for history;
- deterministic for node and event identities;
- restartable after partial failure;
- explicit about records it cannot promote;
- reversible by switching readers, not by deleting v2 data.

Backfill completion should be recorded per legacy run. A global migration flag
is insufficient because partial cohorts and retries are expected.

## Rollback

Before authority moves, rollback means disabling new task creation and
returning scheduling to the legacy path.

After a v2 task begins, do not convert its event history back into a mutable
legacy sequence. Pause the task, preserve its graph and resources, and use a
compatibility view if the legacy operator interface must inspect it.

Never roll back by:

- deleting v2 events;
- reusing a released worktree lease;
- attaching a task to another task's Codex daemon;
- marking completion from a legacy terminal flag;
- releasing a GPU without termination confirmation;
- deleting task state before knowledge commit and probe.

## Completion criteria

Migration is complete when:

- all new tasks use locked v2 contracts;
- new scheduling is event-driven and lease-safe;
- legacy history remains readable;
- resource allocation is controller-owned;
- closure requires fresh completion and dual review;
- terminal cleanup leaves a knowledge commit and tombstone;
- upstream Hermes entry points and agent behavior still pass their compatibility
  tests.
