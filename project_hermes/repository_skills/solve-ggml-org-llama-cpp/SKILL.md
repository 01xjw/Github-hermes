---
name: solve-ggml-org-llama-cpp
description: Resolve llama.cpp Issues across HIP and portable code.
---

# llama.cpp Issue Skill

Solve locked `ggml-org/llama.cpp` Issues across model loading, graph execution,
GGML operators, and HIP backends. Keep portable code separate from backend
optimizations.

## When to Use

Use only for a controller-locked llama.cpp Issue and plan.

## Prerequisites

- Read repository build/test instructions and backend ownership notes.
- Work offline from the immutable baseline with generated/tiny fixtures.
- Use `gfx1100` for available HIP validation and preserve non-HIP builds.

## How to Run

Reduce the Issue to the smallest model metadata, graph, or GGML operator and
trace CPU reference versus backend execution.

## Quick Reference

- Inspect `ggml/`, backend directories, `src/`, `tests/`, and CMake options.
- For HIP builds use existing `GGML_HIP` and `AMDGPU_TARGETS` conventions.
- Validate quantization block sizes, strides, alignment, and tail handling.

## Procedure

1. Lock file format/quantization, tensor shapes, backend, and expected result.
2. Establish a CPU or portable reference when the operation supports one.
3. Fix the owning backend or shared layer without cross-backend leakage.
4. Add a focused local test or deterministic operator fixture.
5. Build the smallest target and run focused tests plus an HIP smoke check.
6. Record compile-only versus executed `gfx1100` evidence.

## Pitfalls

- Quantized kernels often fail only on tails or non-default strides.
- CMake feature gates and backend registration must agree.
- HIP success must not break CPU, Metal, Vulkan, or CUDA compilation paths.

## Verification

- Confirm reference numerics and boundary shapes pass.
- Confirm both portable and HIP registration/build invariants remain valid.
- Confirm focused tests and available `gfx1100` execution pass.
