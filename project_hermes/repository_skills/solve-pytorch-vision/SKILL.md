---
name: solve-pytorch-vision
description: Resolve TorchVision Issues across Python and native ops.
---

# TorchVision Issue Skill

Solve locked `pytorch/vision` Issues across datasets, transforms, models, and
native operators while preserving public API and serialization compatibility.

## When to Use

Use only for a controller-locked TorchVision Issue and plan.

## Prerequisites

- Read repository and directory-level contributor instructions.
- Work offline from the immutable baseline; use only local test assets.
- Determine whether the behavior is Python-only, C++/HIP, or packaging-related.

## How to Run

Locate the closest test and public API contract, then reproduce with the
smallest tensor/image fixture before changing implementation.

## Quick Reference

- Inspect `torchvision/`, `test/`, native `csrc/`, and packaging boundaries.
- Preserve dtype, device, memory format, antialiasing, and batch semantics.
- Prefer tiny synthetic fixtures over downloaded datasets or weights.

## Procedure

1. Lock input format, dtype, shape, device, and expected public behavior.
2. Trace Python dispatch into native ops only when the call actually crosses it.
3. Implement the smallest backward-compatible correction.
4. Add a focused regression using local deterministic data.
5. Run the owning test file and a nearby API/dispatch test.
6. Report native compilation and GPU execution as separate evidence.

## Pitfalls

- PIL, tensor, scripted, and v2 transform paths may differ intentionally.
- CPU success does not validate a HIP native-op branch.
- Avoid network-dependent weights or datasets in regression tests.

## Verification

- Confirm all relevant input representations preserve expected results.
- Confirm public signatures and serialization remain compatible.
- Confirm focused tests pass without network access.
