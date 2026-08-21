# ADR 0002: Use a Dynamic WorkGraph

Status: Accepted

## Context

A fixed technical sequence such as locate, reproduce, plan, implement, and
validate is useful as a playbook but incorrect as a universal state machine.
Real tasks may begin with an existing reproducer, require parallel repository
changes, revisit hypotheses, wait for artifacts, or skip irrelevant
capabilities.

The control plane still needs durable scheduling, concurrency limits,
idempotency, lease recovery, and coarse operator-visible status.

## Decision

Represent technical work as a persistent DAG of generic `WorkNode` records.
Agents choose capability names and dependencies from evidence. The controller
validates graph invariants and policy but does not impose technical ordering.

Use a small lifecycle concerned only with scheduling:

- discovered;
- queued;
- claimed;
- running;
- waiting for resource, artifact, review, or approval;
- completed, blocked, failed, or cancelled.

Every claim has an owner token and expiry. Ready nodes are queued nodes whose
dependencies are complete. Node creation and claims support idempotency and
compare-and-set persistence.

## Consequences

Independent work can run concurrently. Repeated investigation and validation
are natural graph shapes instead of state-machine exceptions.

Operator status remains stable even as capability vocabularies evolve.

Graph validation and lease reconciliation become controller responsibilities.
The controller must preserve append-only events so projections can be rebuilt.

Legacy stage values require a compatibility projection during migration.

## Rejected alternatives

A larger enum containing every technical activity was rejected because every
new workflow would require controller deployment.

A free-form event stream without a DAG projection was rejected because
dependency readiness and atomic claims would be difficult to query safely.

Letting each agent maintain its own private task list was rejected because
resource allocation and recovery require controller-visible ownership.
