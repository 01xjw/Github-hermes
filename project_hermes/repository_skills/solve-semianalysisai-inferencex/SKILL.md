---
name: solve-semianalysisai-inferencex
description: Resolve InferenceX Issues in AMD or portable scope.
---

# InferenceX Issue Skill

Solve locked `SemiAnalysisAI/InferenceX` Issues only in an explicit AMD/ROCm or
hardware-neutral path. Discover local architecture before assuming a framework,
backend, or test command.

## When to Use

Use only for a controller-locked InferenceX Issue and plan.

## Prerequisites

- Read all repository-local agent, build, and contribution instructions.
- Work offline from the immutable baseline and use only checked-in assets.
- Confirm the Issue's hardware relevance and identify the owning component.

## How to Run

Inventory manifests, source roots, tests, and generated files first; then map
the Issue acceptance criteria to the repository's actual execution path.

## Quick Reference

- Derive build/test commands from checked-in configuration, never guess them.
- Lock model/workload, dtype, shape, backend, and resource assumptions.
- Preserve explicit unsupported-hardware behavior and safe fallbacks.

## Procedure

1. Identify language, build system, component boundaries, and test harness.
2. Reduce the Issue to a local deterministic fixture or invariant.
3. Trace from public configuration/API to the first broken owner.
4. Implement the smallest AMD-relevant or portable correction.
5. Add and run the closest focused regression supported by the tree.
6. Record unavailable model assets, GPU paths, or integration checks precisely.

## Pitfalls

- Repository layout may evolve; stale assumed paths create incorrect fixes.
- A generic API can hide a vendor-exclusive implementation.
- Never claim end-to-end acceleration from configuration-only validation.

## Verification

- Confirm the actual checked-in build/test contract was followed.
- Confirm portable or AMD scope is explicit in code and evidence.
- Confirm no NVIDIA-only requirement entered the candidate.
