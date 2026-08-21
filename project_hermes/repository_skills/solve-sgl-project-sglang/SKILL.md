---
name: solve-sgl-project-sglang
description: Resolve SGLang Issues across ROCm serving paths.
---

# SGLang Issue Skill

Solve locked `sgl-project/sglang` Issues across runtime scheduling, model
workers, attention, kernels, and serving APIs. Preserve backend boundaries and
request lifecycle invariants.

## When to Use

Use only for a controller-locked SGLang Issue and plan.

## Prerequisites

- Read repository and runtime/backend instructions.
- Work offline from the immutable baseline using local fixtures.
- Use `gfx1100` only for tests the repository explicitly supports on ROCm.

## How to Run

Trace one request through server/runtime scheduling, model execution, cache
management, and response assembly to locate the first broken invariant.

## Quick Reference

- Inspect Python runtime/server code, backend ops, kernels, and focused tests.
- Lock prefill/decode phase, batch shape, cache state, and parallel settings.
- Use existing backend capability checks and deterministic tiny configurations.

## Procedure

1. Reduce the Issue to one local request or operator fixture.
2. Separate scheduler/state defects from backend kernel defects.
3. Implement within the owning layer and preserve cancellation/error handling.
4. Add a regression for the exact lifecycle or tensor boundary.
5. Run the focused unit test, then an available local runtime smoke test.
6. Report any unavailable model asset or GPU path without inventing success.

## Pitfalls

- Prefill/decode and eager/captured execution paths can diverge.
- Cache ownership bugs may appear as later sampling or timeout failures.
- Do not introduce NVIDIA-only assumptions into shared runtime code.

## Verification

- Confirm the reduced request completes with correct state transitions.
- Confirm backend fallback and error propagation remain intact.
- Confirm focused tests pass offline and `gfx1100` evidence is explicit.
