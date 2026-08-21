# Environment Issue Screener Contract

Screen every frozen Issue in the supplied batch for whether ProjectHermes can
perform useful local repository work in the supplied machine envelope. This
contract is intentionally repository-agnostic. Do not create repository-name,
framework-name, vendor-name, or Issue-number exceptions.

## Decision procedure

For every candidate, in the supplied order:

1. Apply an actionability gate before environment reasoning. Identify the
   concrete requested deliverable and whether it is real open work. When the
   Issue text explicitly says it is tracking-only, informational-only, not
   open work, or requests no implementation, return `REJECT` with task kind
   `NON_ACTIONABLE_REPORT`; do not invent work merely because a change is
   technically imaginable. A tracking or RFC label alone is not enough to
   reject an Issue when its text contains a concrete open deliverable.
2. Separate four sets of facts: reporter metadata, minimum reproduction
   requirements, implementation requirements, and acceptance requirements.
   Reporter hardware and software are evidence, not requirements, unless the
   failure or acceptance path actually depends on them.
3. Look first for an executable CPU or general path: a CPU reproducer, a
   backend-independent operator/compiler path, a failure on CPU and GPU, or a
   test whose logic can be validated without the named accelerator. Such a
   path takes precedence over incidental non-AMD environment words.
4. Bound substitute evidence honestly. A CPU mock, fake tensor, or synthetic
   fixture can validate pure routing, parsing, validation, bookkeeping, or
   backend-neutral code. It does not validate hardware behavior when final
   acceptance still depends on unavailable hardware.
5. Close every mandatory dependency before `SELECT`. A dependency qualifies
   only when the packet proves it available, the repository/release supplies
   it offline, or a concrete offline fixture replaces it. Record that closure
   in the reason or evidence, and leave `external_dependencies` empty on every
   `SELECT`; that field is reserved for unresolved dependencies. Optional
   dependencies do not gate selection. An unproven dependency may produce
   `DEFER` only when one bounded probe and a real probe consumer are identified;
   an unavailable mandatory dependency makes local work `INCOMPATIBLE`. Do not
   infer a hardware vendor from an API namespace when the machine packet
   identifies that namespace as a compatibility alias.
6. Compare the real minimum with the complete `machine_envelope`: operating
   system, CPU architecture and cores, memory, execution-time limit, AMD GPU
   architecture and count, software/runtime capabilities, known unavailable
   capabilities, worker network boundary, source availability, build limits,
   and prohibited external writes. Never assume a capability not listed there,
   and never defer a fact the packet already answers.
7. Treat the mechanical filter result as advisory evidence. It may expose a
   duplicate/invalid label or a hardware concern, but it may not veto an
   explicit compatible CPU/general path. Explain any disagreement.
8. Do not rank by difficulty, expected patch size, age, popularity, whether an
   Issue remains open, or a Top-N quota. Environment fit and locally useful
   work are the decision criteria.
9. State concrete evidence and every material uncertainty. Every `DEFER` must
   name the single bounded probe, the fact or output that resolves it, and the
   configured consumer that will use the result. If no probe consumer exists,
   do not use `DEFER` as a placeholder for ordinary missing diligence.

## Decisions

- `SELECT`: the machine envelope can execute the necessary reproduction or
  useful local code/test work now. Set `machine_compatibility` to `COMPATIBLE`.
  Use task kind `LOCAL_CODE_OR_TEST` or `LOCAL_REPRODUCTION`. Worker network
  access and external-system writes must not be required.
- `DEFER`: one or more named facts require a bounded probe before compatibility
  can be known, such as whether a generic kernel reproduces on the available
  AMD architecture or whether an unproven dependency exists in the worker
  image. Set compatibility to `NEEDS_PROBE`; keep a local task kind; and list
  the exact probe, resolving evidence, and probe consumer in `uncertainties`.
- `REJECT`: useful completion requires an unavailable OS, CPU architecture,
  accelerator vendor/model, more GPUs/CPU/memory than supplied, worker network
  access, an external-system write, release/index/CI administration, or the
  report provides no bounded local work. Set compatibility to `INCOMPATIBLE`
  for an environment mismatch while preserving `LOCAL_CODE_OR_TEST` or
  `LOCAL_REPRODUCTION`. Use `EXTERNAL_OPERATION` only when the required useful
  outcome itself is outside local repository work. A locally compatible report
  may use `NON_ACTIONABLE_REPORT` when it contains no real open work.

## Required environment

Fill every field with the minimum inferred requirement, not the whole machine:

- `operating_systems`: accepted OS names; use an empty list only when OS is
  genuinely irrelevant.
- `cpu_architectures`: accepted CPU architectures; use `amd64` for ordinary
  x86-64 requirements.
- `minimum_cpu_cores`: an integer from 1 through 1024. It must never be `0`,
  negative, `null`, or omitted. Use `1` when no higher CPU minimum is implied.
- `minimum_memory_gib`: an integer from 1 through 4096. It must never be `0`,
  negative, `null`, or omitted. Use `1` when no higher memory minimum is
  implied.
- `gpu_count`: zero for a CPU/general path; otherwise the minimum simultaneous
  GPU count.
- `gpu_architectures`: accepted required architectures or vendor/model names;
  it must be empty when `gpu_count` is zero. Use `gfx1100` when that exact
  supplied architecture is sufficient, and `amd` only when any AMD GPU is
  sufficient.
- `network_access_required`: true only when execution itself must reach an
  external network service. The controller supplying the frozen repository is
  not worker network access.
- `external_system_write_required`: true for GitHub comments/pushes/PRs,
  package-index changes, release publication, CI administration, or any other
  required external mutation.
- `external_dependencies`: name only unresolved material runtimes, services,
  datasets, or packages not already proven by the packet and not supplied by
  the repository. This list must be empty on `SELECT`. When the repository or
  a concrete offline fixture closes a dependency, explain that closure in the
  reason/evidence instead of listing it here.

## Output

Return exactly one JSON object and no Markdown. `decisions` must contain
exactly one object for every supplied candidate ID, with no omissions,
duplicates, or extra IDs:

```json
{"decisions":[{"candidate_id":"poll-candidate-...","decision":"SELECT|DEFER|REJECT","machine_compatibility":"COMPATIBLE|NEEDS_PROBE|INCOMPATIBLE","task_kind":"LOCAL_CODE_OR_TEST|LOCAL_REPRODUCTION|EXTERNAL_OPERATION|NON_ACTIONABLE_REPORT","reason":"specific environment- and work-based reason","required_environment":{"operating_systems":["linux"],"cpu_architectures":["amd64"],"minimum_cpu_cores":1,"minimum_memory_gib":4,"gpu_count":0,"gpu_architectures":[],"network_access_required":false,"external_system_write_required":false,"external_dependencies":[]},"evidence":["specific Issue fact"],"uncertainties":[]}]}
```

Reasons and evidence must be Issue-specific. Use an empty `uncertainties`
array for `SELECT` and for fully established `REJECT`; every `DEFER` must name
at least one bounded uncertainty.

Before returning, validate the complete object: every supplied candidate ID
appears exactly once; both minimum CPU and memory values are positive
integers; a zero-GPU path has an empty `gpu_architectures` list; `SELECT` is
paired with `COMPATIBLE` local work and requires neither worker network access
nor an external write and has an empty `external_dependencies` list; `DEFER`
is paired with `NEEDS_PROBE` and has at least one uncertainty. Correct the JSON
yourself before emitting it; never use an invalid sentinel such as `0` for an
unknown CPU or memory requirement.
