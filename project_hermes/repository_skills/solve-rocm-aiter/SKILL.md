---
name: solve-rocm-aiter
description: Resolve AITER Issues on ROCm and RDNA3 paths.
---

# AITER Issue Skill

Solve locked `ROCm/aiter` Issues across Python dispatch, generated operators,
and ROCm kernels. Give special attention to RDNA3 `gfx1100` configurations.

## When to Use

Use only for a controller-locked AITER Issue and committed plan.

## Prerequisites

- Read local contributor instructions and operator-specific test guidance.
- Work offline from the immutable baseline and never publish changes.
- Treat the available GPU as `gfx1100`; do not infer MI/CDNA coverage from it.

## How to Run

Trace the operator from Python entry point through tuning/config lookup and
kernel dispatch, then reproduce the exact dtype, shape, and layout.

## Quick Reference

- Inspect operator wrappers, `csrc` kernels, config tables, and `op_tests`.
- For A8W8 GEMM, verify scale layout, dtype, padding, and tuned config lookup.
- Compare correctness against a stable reference before timing performance.

## Procedure

1. Lock the failing model shape, dtype, strides, and architecture.
2. Distinguish missing dispatch/config coverage from kernel correctness.
3. Extend the narrow architecture/config predicate with a safe fallback.
4. Add a focused operator regression for the reported boundary.
5. Run correctness first, then the smallest relevant performance smoke check.
6. Report whether validation executed on `gfx1100` or was compile-only.

## Pitfalls

- A tuned RDNA3 config may be wrong for CDNA even when the kernel name matches.
- W8A8 scale broadcasting and non-contiguous inputs are common hidden edges.
- Never turn a missing optimized config into a crash instead of a fallback.

## Verification

- Confirm reference numerics and tolerances pass for the Issue shape.
- Confirm dispatch selects the intended `gfx1100` path and fallback remains.
- Confirm the regression test fails on the old behavior and passes now.
