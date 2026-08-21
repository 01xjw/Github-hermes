---
name: solve-paddlepaddle-paddle
description: Resolve Paddle Issues with backend-focused validation.
---

# Paddle Issue Skill

Solve locked `PaddlePaddle/Paddle` Issues across Python APIs, operators,
autograd, graph compilation, and device backends. Preserve shared semantics and
explicit backend registration.

## When to Use

Use only for a controller-locked Paddle Issue and plan.

## Prerequisites

- Read repository and owning subsystem build/test instructions.
- Work offline from the immutable baseline.
- Identify static/eager/PIR mode and CPU, AMD-relevant, or portable ownership.

## How to Run

Reduce the Issue to a deterministic operator/program and trace API, shape/type
inference, kernel selection, and gradient behavior.

## Quick Reference

- Inspect `python/`, `paddle/`, operator/kernel registration, and tests.
- Lock execution mode, dtype, shape, place, layout, and gradient requirements.
- Use existing backend macros and capability checks.

## Procedure

1. Establish the smallest failing program and expected output/gradient.
2. Separate API/inference defects from backend kernel defects.
3. Implement at the owning layer without vendor leakage into shared code.
4. Add a focused operator or graph regression using local data.
5. Run the owning test and a nearby mode/backend compatibility test.
6. Report unavailable accelerator compilation or execution honestly.

## Pitfalls

- Eager, static, and PIR paths may have separate registration and inference.
- Forward correctness does not prove gradient correctness.
- Do not convert a CUDA-only report into an unimplemented AMD kernel request.

## Verification

- Confirm outputs, gradients, shape/type inference, and errors as applicable.
- Confirm shared CPU behavior or backend gates remain stable.
- Confirm focused tests pass offline.
