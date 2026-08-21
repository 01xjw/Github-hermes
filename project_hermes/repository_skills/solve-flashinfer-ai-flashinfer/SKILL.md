---
name: solve-flashinfer-ai-flashinfer
description: Resolve FlashInfer Issues without CUDA-only assumptions.
---

# FlashInfer Issue Skill

Solve locked `flashinfer-ai/flashinfer` Issues only when they are portable or
explicitly AMD-relevant. Preserve the distinction between frontend contracts,
code generation, and accelerator-specific kernels.

## When to Use

Use only for a controller-locked FlashInfer Issue and plan.

## Prerequisites

- Read repository build, code-generation, and test instructions.
- Work offline from the immutable baseline and avoid package downloads.
- Confirm the Issue is not an NVIDIA/CUDA-only kernel request before editing.

## How to Run

Trace the failing public API through planning/dispatch and generated/native
implementation, identifying which layer is actually portable.

## Quick Reference

- Inspect Python APIs, JIT/codegen sources, native kernels, and focused tests.
- Lock attention mode, page/cache layout, dtype, head dimensions, and backend.
- Change generators rather than generated artifacts when a generator exists.

## Procedure

1. Prove the accepted Issue has a portable or AMD-relevant implementation path.
2. Reduce it to deterministic tensor metadata and expected output.
3. Fix the narrow owning layer using existing backend gates.
4. Add a regression that can run without remote models or data.
5. Run frontend/unit checks and only supported backend compilation/execution.
6. Report unsupported backend validation honestly.

## Pitfalls

- A portable Python API may still dispatch exclusively to CUDA kernels.
- Generated code edits disappear unless the canonical generator changes.
- Never advertise AMD support from a frontend-only change.

## Verification

- Confirm dispatch and capability checks match the implemented backend scope.
- Confirm reference numerics or API invariants pass.
- Confirm the diff introduces no unguarded NVIDIA-specific dependency.
