# ADR 0001: Extend Hermes Additively

Status: Accepted

## Context

Hermes already provides the conversation loop, provider setup, tool calling,
plugins, skills, memory, delegation, and user-facing entry points. Replacing
those surfaces would fork behavior, increase upstream merge conflicts, and
duplicate security-sensitive provider logic.

ProjectHermes needs stronger task governance and production resource control
without weakening upstream compatibility.

## Decision

ProjectHermes is an additive package named `project_hermes`.

Upstream modules remain authoritative for Hermes reasoning behavior.
ProjectHermes receives an upstream agent through an injected factory and wraps
it with the framework-neutral `AgentRuntime` SPI.

Changes outside the package are limited to:

- a dedicated optional dependency group;
- a `project-hermes` CLI entry point;
- package discovery metadata;
- ignored local control-plane state;
- ProjectHermes documentation, tests, configuration, and schemas.

No upstream `hermes`, `hermes-agent`, or `hermes-acp` entry point is replaced.

## Consequences

Upstream changes can be merged with a small conflict surface. Existing Hermes
users do not opt into ProjectHermes unless they install and invoke its optional
surface.

ProjectHermes cannot assume private upstream implementation details. Adapters
must target stable public behavior or inject factories at the integration
boundary.

Some capability data is normalized, so framework-specific details may remain
in typed metadata rather than the generic interface.

## Rejected alternatives

Forking the upstream agent loop was rejected because it would create two
reasoning implementations and make upstream synchronization expensive.

Replacing Hermes with a fixed orchestrator was rejected because technical
workflow choice belongs to the autonomous inner loop.

Embedding control-plane side effects directly in upstream tools was rejected
because it would blur authority and complicate independent audit.
