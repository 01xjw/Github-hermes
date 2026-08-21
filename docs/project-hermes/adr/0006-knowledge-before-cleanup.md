# ADR 0006: Commit Knowledge Before Cleanup

Status: Accepted

## Context

Autonomous tasks accumulate useful positive and negative knowledge in private
runtime state. Deleting that state immediately after merge, rejection, or
failure can lose reproducibility details and repeat past mistakes.

A storage API returning success does not prove that bytes are durably readable.
Cleanup paths proposed from task context may also escape the intended boundary.

## Decision

Use a two-phase knowledge and cleanup lifecycle:

1. extract evidence-bound candidates;
2. validate each candidate independently;
3. commit one content-addressed batch;
4. probe the stored bytes and digest;
5. validate every deletion path;
6. delete task-private state;
7. write a minimal tombstone.

Merged outcomes produce positive knowledge. Closed, rejected, and failed
outcomes produce negative knowledge. Candidates cannot mix tasks or outcomes
in one commit.

Cleanup cannot delete its allowed root, escape that root, or place the
tombstone inside a path scheduled for deletion.

## Consequences

Failed attempts become reusable operational knowledge instead of disappearing.
Cleanup is delayed until durable storage proves the batch can be read.

The knowledge repository becomes a terminal-path dependency. If it is
unavailable, private state remains and operators must retry safely.

Tombstones provide a small audit trail without retaining complete task-private
content.

## Rejected alternatives

Deleting immediately after a pull-request outcome was rejected because it can
lose the only explanation of failures and environment constraints.

Writing knowledge after deletion was rejected because extraction may depend on
the deleted state.

Trusting a successful write response without a read probe was rejected because
it does not prove durable retrievability.
