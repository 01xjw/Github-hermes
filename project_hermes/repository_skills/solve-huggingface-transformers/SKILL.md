---
name: solve-huggingface-transformers
description: Resolve Transformers Issues with model-focused tests.
---

# Transformers Issue Skill

Solve locked `huggingface/transformers` Issues across model implementations,
generation, tokenization, and loading. Preserve shared abstractions and model
configuration compatibility.

## When to Use

Use only for a controller-locked Transformers Issue and plan.

## Prerequisites

- Read repository instructions and the model family's test conventions.
- Work offline from the immutable baseline using tiny random configurations.
- Identify shared generation/loading code versus one model implementation.

## How to Run

Reduce the Issue to a tiny config and deterministic tensors, then trace masks,
cache layout, dtype, device, and output contract.

## Quick Reference

- Inspect `src/transformers/` and the corresponding `tests/models/` subtree.
- Prefer tiny random models; never download weights or tokenizers.
- Check eager and supported attention/cache paths when shared code changes.

## Procedure

1. Lock model class, config, input shapes, cache mode, dtype, and expected output.
2. Determine whether the defect belongs to shared utilities or model code.
3. Reuse established configuration and output dataclass patterns.
4. Add a tiny deterministic regression at the narrowest owning layer.
5. Run the model test and the nearest shared generation/loading test.
6. State which accelerator-specific path was actually exercised.

## Pitfalls

- Cache position, attention masks, and left padding interact subtly.
- Optional backends and lazy imports must remain safe when unavailable.
- Do not copy a CUDA-only optimization into a portable model path.

## Verification

- Confirm outputs, cache shapes, and config round trips meet the contract.
- Confirm another representative model still passes if shared code changed.
- Confirm focused tests pass fully offline.
