# ADR 0004: Separate Completion Layers and Independent Review

Status: Accepted

## Context

Implementation, unit verification, operator correctness, performance,
serving, end-to-end validation, packaging, and CI are different claims. A
single success status hides missing evidence and becomes stale when code or
goals change.

An implementing agent also cannot serve as the only authority that its work is
complete or minimal.

## Decision

Track nine completion layers in `completion-matrix.v3`. Each goal path has its
own repository-bound projection, and each satisfied entry
requires evidence bound to the exact candidate digest and goal revision.
Candidate changes invalidate stale satisfied entries.

Require two independent review roles:

- completion auditor;
- minimal-diff reviewer.

Each role submits exactly one fresh verdict for the candidate. Approval
requires independent evidence and no unresolved medium-or-higher finding.
Review sessions must be distinct and cannot equal the implementer session.

Closure is the conjunction of the completion matrix and review gate. Missing,
duplicate, stale, revision, or rejection results fail closed.

## Consequences

Operators can see which claim is incomplete instead of interpreting one broad
status. Code changes trigger targeted revalidation.

Tasks may configure a subset of completion layers, but cannot waive required
layers after work begins without an approved goal revision.

Review costs increase, especially for small changes. This is accepted because
publication and cleanup are high-impact terminal operations.

Session identity provides a minimum independence check. Higher-risk
deployments should also use separate model invocations, prompts, credentials,
or human reviewers.

## Rejected alternatives

A single `validated` flag was rejected because it cannot represent independent
environment and serving claims.

Allowing the implementer to approve completion was rejected because it removes
the principal independent control.

Keeping reviews as unstructured comments was rejected because stale and
duplicate verdicts could not be evaluated deterministically.
