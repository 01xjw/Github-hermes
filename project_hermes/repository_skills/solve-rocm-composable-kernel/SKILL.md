---
name: solve-rocm-composable-kernel
description: Resolve Composable Kernel Issues and validate dispatch.
---

# Composable Kernel Issue Skill

Solve locked `ROCm/composable_kernel` Issues across kernel instances,
selection logic, and host integration. Preserve compile-time coverage and
avoid adding an instance that dispatch cannot reach.

## When to Use

Use only for a controller-locked Composable Kernel Issue and plan.

## Prerequisites

- Read repository instructions and the closest example/test conventions.
- Work offline from the immutable baseline; never publish or fetch code.
- Use `gfx1100` for available RDNA3 validation and guard other architectures.

## How to Run

Trace the full path from public operation to instance factory, support check,
dispatch, and device kernel before changing code.

## Quick Reference

- Inspect `include/`, `library/`, `example/`, `profiler/`, and `test/` ownership.
- Check type, layout, padding, vectorization, and architecture predicates.
- Build the smallest target that instantiates the changed templates.

## Procedure

1. Reduce the Issue shape to exact types, layouts, dimensions, and gfx target.
2. Find the nearest working instance and compare every dispatch predicate.
3. Change source generators or instance lists rather than generated output.
4. Add a boundary case covering both supported and rejected configurations.
5. Compile the focused target, then run its unit/profiler validation.
6. Report compile coverage separately from executed GPU coverage.

## Pitfalls

- Template code can compile without ever being registered or selected.
- Alignment and vector-width changes can invalidate odd or tail dimensions.
- A `gfx1100` optimization must not become an unconditional CDNA behavior.

## Verification

- Confirm the new path is registered and selected for the Issue shape.
- Confirm unsupported shapes still fail their support predicate safely.
- Confirm focused compilation and available `gfx1100` execution pass.
