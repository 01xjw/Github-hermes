---
name: solve-microsoft-onnxruntime
description: Resolve ONNX Runtime Issues with provider-aware tests.
---

# ONNX Runtime Issue Skill

Solve locked `microsoft/onnxruntime` Issues across graph transforms, kernels,
and execution providers. Keep provider-specific behavior behind established
capability and registration boundaries.

## When to Use

Use only for a controller-locked ONNX Runtime Issue and plan.

## Prerequisites

- Read repository build, test, and provider-specific instructions.
- Work offline from the immutable baseline; do not update submodules.
- Identify graph, session, kernel registry, or execution-provider ownership.

## How to Run

Reduce the model to the smallest ONNX graph and determine the first provider
assignment or execution step that differs from expected behavior.

## Quick Reference

- Inspect `onnxruntime/core`, provider directories, `test/`, and build scripts.
- Check opset, dtype, shape inference, provider assignment, and fallback.
- Build and run only the smallest target supported by the prepared tree.

## Procedure

1. Lock opset, graph nodes, tensor metadata, providers, and expected output.
2. Separate graph-rewrite failures from kernel-selection failures.
3. Follow existing registration and feature-gating conventions.
4. Add a focused graph or provider regression with deterministic inputs.
5. Run the owning test plus a provider/fallback neighbor.
6. State clearly when ROCm provider execution was unavailable.

## Pitfalls

- A graph can pass CPU tests while failing provider assignment or fusion.
- Kernel registration changes can affect many opsets and type constraints.
- Never replace a valid fallback with a provider-specific hard failure.

## Verification

- Confirm expected graph assignment and numeric output.
- Confirm fallback and unsupported-type behavior remain correct.
- Confirm the smallest build/test target passes offline.
