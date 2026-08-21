---
name: solve-deepseek-ai-deepep
description: Resolve DeepEP Issues in portable or AMD-relevant scope.
---

# DeepEP Issue Skill

Solve locked `deepseek-ai/DeepEP` Issues only within portable control logic or
an explicit AMD/ROCm path. Do not infer that CUDA/NVLink transport code is
portable merely because its Python interface is generic.

## When to Use

Use only for a controller-locked DeepEP Issue and plan.

## Prerequisites

- Read repository build, communication, and test instructions.
- Work offline from the immutable baseline with no cluster/network actions.
- Identify transport, buffer layout, dispatch, synchronization, and API scope.

## How to Run

Reduce the Issue to the smallest local state/metadata transition and trace it
through Python bindings into native communication code.

## Quick Reference

- Inspect bindings, buffer/dispatch code, build gates, and local tests.
- Lock world size, rank mapping, token layout, dtype, and synchronization phase.
- Preserve backend capability failures when no AMD implementation exists.

## Procedure

1. Prove the Issue is portable or names an implemented AMD-relevant boundary.
2. Separate API/state defects from transport-kernel defects.
3. Implement only in the owning layer with explicit capability gates.
4. Add a deterministic local regression that needs no distributed network.
5. Run focused binding/unit checks and supported compilation.
6. Report unavailable multi-node or accelerator validation explicitly.

## Pitfalls

- Collective semantics cannot be validated by a single-rank success alone.
- Buffer alignment and token routing errors may surface after synchronization.
- Never replace unsupported hardware detection with a false success path.

## Verification

- Confirm state, shape, and error-handling invariants on the local fixture.
- Confirm unsupported backends remain explicit and safe.
- Confirm no NVIDIA-only primitive leaked into portable code.
