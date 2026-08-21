---
name: solve-vllm-project-vllm
description: Resolve vLLM Issues across ROCm serving paths.
---

# vLLM Issue Skill

Solve locked `vllm-project/vllm` Issues across model execution, attention,
quantization, sampling, and serving. Keep ROCm dispatch explicit and preserve
portable behavior.

## When to Use

Use only for a controller-locked vLLM Issue and plan.

## Prerequisites

- Read repository and subsystem contributor instructions.
- Work offline from the immutable baseline with synthetic/tiny fixtures.
- Use `gfx1100` as the available GPU target; do not infer CUDA or CDNA results.

## How to Run

Trace the request from configuration through platform detection, worker/model
runner, operator dispatch, and output sampling before editing.

## Quick Reference

- Inspect `vllm/`, ROCm platform code, attention/quantization ops, and `tests/`.
- For AITER W8A8, validate scale layout and fallback dispatch on RDNA3.
- For GDN decode or sampling, validate state/cache shape and deterministic output.

## Procedure

1. Lock model architecture, phase, batch/sequence shape, dtype, and backend.
2. Distinguish configuration gating from operator and scheduler correctness.
3. Follow platform/quantization registries instead of device-name conditionals.
4. Add a focused regression that avoids remote model downloads.
5. Run CPU/unit coverage first, then the smallest `gfx1100` execution check.
6. Report correctness and performance evidence separately.

## Pitfalls

- Prefill and decode frequently use different kernels and cache layouts.
- AITER W8A8, GDN decode, and sampling each have independent dispatch gates.
- Never make missing ROCm optimization support a fatal error when fallback exists.

## Verification

- Confirm platform dispatch selects the intended path for the Issue shape.
- Confirm deterministic output/reference numerics and fallback behavior.
- Confirm focused tests and available `gfx1100` smoke validation pass.
