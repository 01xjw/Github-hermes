---
name: solve-pytorch-pytorch
description: Resolve PyTorch Issues with focused regression tests.
---

# PyTorch Issue Skill

Solve locked `pytorch/pytorch` Issues using the owning subsystem's established
patterns. Accept hardware-neutral work and explicit AMD/ROCm work; never turn a
CUDA-only report into an assumed ROCm task.

## When to Use

Use only for a controller-locked PyTorch Issue and plan.

## Prerequisites

- Read root and nested `AGENTS.md` plus subsystem test instructions.
- Work offline from the immutable baseline with no publication.
- Identify whether the path is eager, ATen, Inductor, Dynamo, distributed, or HIP.

## How to Run

Start with the smallest existing test file and trace the failure to the owning
dispatcher, transformation, or kernel before editing.

## Quick Reference

- Use focused `python test/<file>.py -k <case>`-style tests when available.
- For Inductor, inspect layout, stride, aliasing, guards, and fusion boundaries.
- Keep CUDA/HIP shared code portable; isolate vendor APIs behind existing gates.

## Procedure

1. Reduce the reproducer to the exact operator graph and tensor metadata.
2. Identify the first incorrect invariant, not the final generated-code symptom.
3. Reuse the subsystem's dispatch and feature-gating conventions.
4. Add a focused regression beside the owning tests, including a boundary case.
5. Run the focused test and the nearest related test group.
6. Record skipped hardware coverage precisely and avoid unsupported claims.

## Temporary Storage Discipline

PyTorch installation trees are large, and the Worker's `/tmp` volume is bounded.

- Inspect `/tmp` usage with `du` before and after creating a large install,
  build, or copied runtime tree.
- Keep at most one complete patched installation or copied `site-packages` tree.
  Reuse that tree for every focused validation that needs the patched runtime.
- Prefer a targeted import overlay, `PYTHONPATH`, or the subsystem's supported
  in-place build path when the production diff does not require a full copy.
- Before creating a replacement large tree, remove the explicitly identified,
  superseded tree and confirm that its space was reclaimed. Do not accumulate
  numbered copies such as `installedpatch2`, `installedpatch3`, and later.
- Run the required tests against the checkout or runtime that actually contains
  the production diff. Storage cleanup never substitutes for Reviewer evidence.

## Checkout-Backed Runtime Evidence

When a locked acceptance criterion requires tests against the patched checkout,
the executed Python modules and test file must come from that checkout. A copied
or ported patch under `agent_space`, `/tmp`, or installed `site-packages` is
useful diagnostic evidence, but it is not checkout-backed verification.

- Prefer making the checkout importable with the image's already-installed,
  offline binary artifacts. Materialize or link only missing ignored/generated
  binary assets into the checkout; never replace tracked Python sources with
  files from the prebuilt installation.
- A targeted import bootstrap is acceptable only when every changed production
  module is loaded under its canonical module name directly from the checkout
  and the repository's own focused test file is executed from the checkout.
- Before the focused test, record `torch.__file__` plus every changed module's
  `__file__`. Resolve the paths and assert that `torch` and all changed Python
  modules are under the current repository root. Also assert that each loaded
  source byte-for-byte matches the corresponding working-tree file.
- Execute the exact focused test in a fresh process so the CUDA initialization
  assertion starts from a clean state. Do not substitute a generated standalone
  test or a test file from the image's separate PyTorch source tree.
- Use a full editable/in-place build only when its build dependencies are
  already available offline. Never fetch a missing build backend. If checkout
  import is genuinely incompatible with the provided binary runtime, report the
  exact import or ABI failure and do not label overlay results as checkout tests.

## Pitfalls

- Layout-sensitive Inductor fusion can change semantics without shape changes.
- Device strings and CUDA environment dumps do not prove vendor exclusivity.
- Generated files and operator schemas often have canonical source generators.
- Multiple full PyTorch runtime copies can exhaust bounded local storage before
  the Worker can archive its candidate and test evidence.

## Verification

- Confirm the minimal reproducer and focused regression pass.
- Confirm CPU/shared behavior or HIP gating is unchanged where applicable.
- Confirm the diff follows the subsystem's formatting and codegen rules.
- For checkout-required evidence, confirm the recorded resolved module and test
  paths are inside the current checkout and not an overlay or image source tree.
