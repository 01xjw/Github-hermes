---
name: solve-pytorch-executorch
description: Resolve ExecuTorch Issues across export and runtimes.
---

# ExecuTorch Issue Skill

Solve locked `pytorch/executorch` Issues across export, lowering, kernels, and
runtime integration. Preserve the boundary between portable core behavior and
backend-specific delegates.

## When to Use

Use only for a controller-locked ExecuTorch Issue and plan.

## Prerequisites

- Read repository instructions and the owning backend/runtime documentation.
- Work offline from the immutable baseline and avoid dependency downloads.
- Identify export-time, compile-time, load-time, and execution-time stages.

## How to Run

Reduce the Issue to the smallest exported program or runtime fixture and trace
which stage first violates its contract.

## Quick Reference

- Inspect `exir/`, `runtime/`, `kernels/`, `backends/`, examples, and tests.
- Preserve schema/version compatibility for serialized programs.
- Use existing backend capability checks rather than device-name shortcuts.

## Procedure

1. Pin the failing stage, operator set, shapes, and delegate configuration.
2. Compare the portable path with the selected backend path.
3. Fix the owning stage without leaking backend policy into core abstractions.
4. Add a minimal local regression for the serialized or runtime boundary.
5. Run the smallest Python/native target and related compatibility check.
6. Report any unavailable toolchain or device validation explicitly.

## Pitfalls

- Export success does not prove runtime loading or operator availability.
- Generated schemas and bindings must be changed at their canonical source.
- Backend fallback behavior must remain deterministic and observable.

## Verification

- Confirm the minimal program crosses all affected stages successfully.
- Confirm serialization and fallback behavior remain compatible.
- Confirm focused Python/native tests pass offline.
