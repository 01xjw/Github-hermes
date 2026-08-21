---
name: solve-huggingface-diffusers
description: Resolve Diffusers Issues with pipeline-focused tests.
---

# Diffusers Issue Skill

Solve locked `huggingface/diffusers` Issues across pipelines, schedulers,
models, and loading utilities. Preserve configuration and serialization
compatibility while keeping tests independent of remote model downloads.

## When to Use

Use only for a controller-locked Diffusers Issue and plan.

## Prerequisites

- Read repository and component test instructions.
- Work offline from the immutable baseline with tiny local fixtures.
- Identify the owning pipeline, scheduler, model, loader, or callback path.

## How to Run

Reproduce with a tiny deterministic component configuration and trace tensor
shape, dtype, device, and scheduler state through the failing step.

## Quick Reference

- Inspect `src/diffusers/` and the matching `tests/` subtree.
- Use dummy components and seeded tensors instead of hub downloads.
- Check config round trips, optional components, and CPU/device offload gates.

## Procedure

1. Lock pipeline class, configuration, step, dtype, device, and expected result.
2. Find the nearest tiny-model test and match its fixture style.
3. Fix the owning component without special-casing a public pipeline name.
4. Add a deterministic regression for the reported boundary.
5. Run the focused test file and one configuration/serialization neighbor.
6. Report accelerator coverage separately from CPU logic coverage.

## Pitfalls

- Scheduler state and timestep dtype can make failures order-dependent.
- Optional dependencies must stay import-safe when absent.
- A CUDA environment report alone does not justify CUDA-only code.

## Verification

- Confirm seeded output invariants or exact shapes pass.
- Confirm config save/load and optional-component behavior remain stable.
- Confirm tests run without network access.
