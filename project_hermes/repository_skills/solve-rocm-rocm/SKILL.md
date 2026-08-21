---
name: solve-rocm-rocm
description: Resolve ROCm core Issues with focused validation.
---

# ROCm Core Issue Skill

Solve locked Issues in `ROCm/ROCm` while preserving component boundaries and
the repository's role as the ROCm integration tree. Do not broaden an Issue
into an unrelated component upgrade.

## When to Use

Use only for a controller-locked `ROCm/ROCm` Issue and plan.

## Prerequisites

- Read repository `AGENTS.md`, contribution rules, and component-owned docs.
- Work from the immutable baseline without network access or publication.
- Treat `gfx1100` as the available validation target when GPU work is needed.

## How to Run

Inspect the Issue path, identify the owning ROCm component, and validate the
smallest affected integration surface before editing.

## Quick Reference

- Check manifests, component pins, CMake, packaging, and install scripts first.
- Prefer the owning component's focused test over a full ROCm build.
- Verify path, version, and platform logic on Linux and ROCm explicitly.

## Procedure

1. Map every acceptance criterion to one owning file and one check.
2. Reproduce the failure or prove the broken invariant from local evidence.
3. Follow existing component/version conventions; avoid speculative wrappers.
4. Implement the narrowest fix and add a focused regression check when viable.
5. Run formatting or schema checks before the smallest relevant build/test.
6. Record exact commands, outcomes, and any untestable hardware boundary.

## Pitfalls

- Do not patch generated manifests when their source generator owns the value.
- Do not silently change component pins or supported architecture policy.
- Do not claim a full stack result from a component-only check.

## Verification

- Confirm the diff contains only Issue-scoped source changes.
- Confirm the owning component or manifest check passes.
- Confirm no NVIDIA-only assumption entered an AMD-native path.
