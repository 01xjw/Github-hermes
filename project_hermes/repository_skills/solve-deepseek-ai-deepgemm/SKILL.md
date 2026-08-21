---
name: solve-deepseek-ai-deepgemm
description: Resolve DeepGEMM Issues in portable or AMD-relevant scope.
---

# DeepGEMM Issue Skill

Solve locked `deepseek-ai/DeepGEMM` Issues only when the affected layer is
portable or explicitly AMD/ROCm-aware. Keep architecture-specific code
generation and tuning assumptions visible.

## When to Use

Use only for a controller-locked DeepGEMM Issue and plan.

## Prerequisites

- Read repository code-generation, tuning, build, and test instructions.
- Work offline from the immutable baseline.
- Lock matrix shapes, dtypes, layouts, scaling, architecture, and expected error.

## How to Run

Trace API configuration through code generation, compilation, dispatch, and
reference comparison; stop if the requested backend has no implementation.

## Quick Reference

- Inspect generators, templates, tuning tables, JIT/build paths, and tests.
- Treat alignment, tile divisibility, scaling layout, and tails as first-class.
- Change generator inputs/templates rather than emitted kernels.

## Procedure

1. Prove the Issue belongs to portable logic or an existing AMD path.
2. Reproduce configuration/shape selection independently of performance.
3. Fix capability or generation logic with architecture-explicit predicates.
4. Add a boundary regression for supported and rejected shapes.
5. Run generation/compile checks and reference numerics where supported.
6. Report performance only when measured on the named architecture.

## Pitfalls

- A generated kernel name does not prove it supports the requested architecture.
- Tile and scale assumptions commonly fail at non-divisible dimensions.
- Never claim AMD kernel support from a parser or API-only correction.

## Verification

- Confirm generation/selection is deterministic for the Issue shape.
- Confirm reference numerics and unsupported-shape errors are correct.
- Confirm architecture gates did not broaden unintentionally.
