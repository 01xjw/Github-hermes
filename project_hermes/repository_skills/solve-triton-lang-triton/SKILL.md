---
name: solve-triton-lang-triton
description: Resolve Triton Issues across AMD backend and compiler.
---

# Triton Issue Skill

Solve locked `triton-lang/triton` Issues across language semantics, compiler
passes, MLIR lowering, and the AMD backend. Preserve target-independent IR
contracts and backend-specific legality.

## When to Use

Use only for a controller-locked Triton Issue and plan.

## Prerequisites

- Read repository and compiler/backend test instructions.
- Work offline from the immutable baseline with the available toolchain.
- Use `gfx1100` for available AMD execution; do not infer CUDA/CDNA coverage.

## How to Run

Reduce the Issue to the smallest Triton kernel and identify the first incorrect
IR stage, target property, lowering, or runtime behavior.

## Quick Reference

- Inspect Python frontend, dialects/passes, AMD backend, runtime, and tests.
- Capture shapes, constexprs, layouts, strides, warps, stages, and target.
- Use IR/compiler tests for transformations and execution tests for semantics.

## Procedure

1. Minimize the kernel while preserving the failing compiler stage.
2. Dump or inspect the nearest stable IR boundary to locate the defect.
3. Fix target-independent logic or AMD lowering at its true owner.
4. Add the smallest regression at the transformation or execution layer.
5. Run focused compiler tests, then an available `gfx1100` execution check.
6. Record compile-only and runtime evidence separately.

## Pitfalls

- A frontend symptom may originate in layout conversion several passes later.
- AMD wave size and resource limits must not be encoded as CUDA assumptions.
- Overly broad canonicalization changes can affect unrelated backends.

## Verification

- Confirm the reduced kernel compiles and produces reference-correct output.
- Confirm the relevant IR invariant and target legality are preserved.
- Confirm nearby AMD and target-independent tests pass.
