# ProjectHermes

ProjectHermes is an additive control-plane extension for Hermes Agent. It keeps
the upstream Hermes conversation loop, provider setup, plugins, skills, memory,
and delegation behavior intact. It adds strict task contracts, an event-driven
work graph, framework-neutral runtime adapters, task-private Codex daemons,
controller-owned resources, evidence gates, independent review, and a durable
knowledge cleanup protocol. A continuous, bounded GitHub Issue poller replaces
the former Campaign scheduler; Main Hermes owns triage, planning, dispatch, and
the worker lifecycle.

The upstream foundation was cloned from
`https://github.com/NousResearch/hermes-agent.git`. ProjectHermes-owned code is
isolated in `project_hermes/`; existing upstream entry points continue to work.

## Non-negotiable invariants

- Hermes and Codex decide the next technical action from current evidence. The
  control plane never turns investigation into a fixed technical stage list.
- The outer loop owns permissions, worktrees, artifacts, GPU leases, execution,
  publication, cleanup, and audit records.
- Every task starts from an independently triaged, locked `issue-task.v2`
  contract.
- Scope expansion requires an approved goal revision that names the exact
  repository being added.
- A Codex task has one private runtime directory, root thread, Unix socket,
  `CODEX_HOME`, and `CODEX_SQLITE_HOME`.
- Every polled Issue worker starts a fresh conversation and `CODEX_HOME`, and
  receives exactly one digest-locked Skill for its configured repository.
- Evidence is bound to an exact candidate digest and goal revision.
- Completion requires all configured completion layers plus both mandatory,
  independent review roles.
- Knowledge is validated and durably probed before task-private state is
  deleted.
- API keys are never stored in the main configuration. A task daemon receives
  only credential-shaped environment variables from a private, untracked file.
- Model identity and requested reasoning mode are immutable for a live Codex
  daemon. Escalation always closes the primary daemon and provisions a new
  generation on the same worktree.
- Completion-auditor reviews record progress against the frozen goal paths.
  Two consecutive completed, non-approved cycles with `no_progress` or
  `regressed` request the single automatic escalation for that goal revision;
  an unsuccessful escalated attempt creates an operator-visible blocker.
- Accounting is append-only and stores numeric usage and timing, never prompts
  or private model reasoning. Total elapsed is task wall-clock time; total LLM
  cost excludes execution hardware cost and reports unknown pricing explicitly.
- ProjectHermes-owned code, documentation, comments, configuration, and
  generated schemas are English only.

## Quick start

Install the ProjectHermes optional dependencies:

```bash
uv sync --extra project-hermes
```

Create a local configuration:

```bash
uv run project-hermes init --config project-hermes.yaml
uv run project-hermes validate-config project-hermes.yaml
uv run project-hermes doctor project-hermes.yaml
```

The generated configuration keeps Codex disabled. To enable it, create a
private credentials file under the project root:

```yaml
environment:
  OPENAI_API_KEY: replace-with-an-operator-provided-key
```

Set its permissions to `0600`, keep it untracked, and configure:

```yaml
codex:
  enabled: true
  credential_delivery: file_mount
  credentials_file: .project-hermes/credentials.yaml
  network_access: true
```

For a custom Codex-compatible provider, also set `model_provider`,
`provider_endpoint`, and `provider_api_key_env`. The named environment variable
must exist in the credentials file.

Create and inspect a run:

```bash
uv run project-hermes create-run \
  --config project-hermes.yaml \
  --task issue-task.yaml

uv run project-hermes show-run \
  --config project-hermes.yaml \
  RUN_ID
```

Export interoperability schemas:

```bash
uv run project-hermes export-schemas schemas/project-hermes
```

## Unified web frontend

The Hermes web build now uses the ProjectHermes operations shell as its primary
frontend. Start it with:

```bash
uv run hermes dashboard
```

The primary navigation contains:

- **Overview** for scan totals, repository metrics, queue depth, and lane state;
- **Issues** for the live filtered Issue feed and original GitHub links;
- **Work** for Main Hermes plans and the six isolated worker lanes;
- **Changes** for GitHub-style changed-file totals, split or unified diffs, and
  Draft PR descriptions;
- **Chat** for the original persistent Hermes TUI over PTY and WebSocket;
- **Hermes Tools** for the original configuration, files, analytics, models,
  logs, skills, plugins, and system pages.

The Changes page defaults to `/app/Github_Hermes`. Operators can select another
Git worktree in the page. Existing pull-request title and body data are loaded
through the authenticated GitHub CLI when available. Draft descriptions for
branches without a pull request are stored only in browser local storage until
publication is explicitly authorized.

## Continuous Issue pipeline

Polling is a long-lived bounded pipeline rather than a bulk import. During the
training phase, each run selects the least-recently scanned configured
repository and scans newly opened Issues from the last 30 days, with a hard
30-page/3,000-candidate ceiling. It mechanically filters and deduplicates the
Issues, then queues eligible Work. NVIDIA/H20/XPU-only Issues without an
AMD/ROCm signal are excluded, while hardware-neutral Issues remain eligible;
ROCm-owned repositories use an explicit AMD-native policy. When that one
repository scan finishes, the next repository starts immediately. The
configured interval is a per-repository refresh cadence, so a completed pass
sleeps until the oldest repository is due instead of repeatedly calling GitHub.

The pending buffer counts `queued` and `planning` Work and defaults to six.
When it is full, discovery pauses without dropping eligible candidates. As
Main Hermes dispatches Work into one of the six running lanes, stored eligible
candidates are promoted first and repository polling resumes. GitHub retry and
rate-limit handling remains authoritative.

Main Hermes reads `project_hermes/AGENT.md` and performs exactly one auditable
project action per turn: assess queued Work, verify the cluster environment,
commit a plan, start an isolated Kubernetes worker, or wait. Each decision uses
a fresh manager conversation and reloads the stable contract plus the bounded
durable projection. Planned Work starts without another model call. The
controller owns every state transition, repository Skill identity, and resource
allocation.

## Package map

- `project_hermes.models` defines issue-task, responsibility, capability,
  permission, and goal-revision contracts. A legacy campaign identifier is
  decoded only for old persisted task records.
- `project_hermes.polling`, `project_hermes.polling_store`, and
  `project_hermes.polling_supervisor` implement continuous repository rotation,
  durable candidates, queue backpressure, and service orchestration.
- `project_hermes.project_manager` gives Main Hermes the project-manager role
  and dispatches approved Work into the controlled execution path.
- `project_hermes.work_graph` defines lifecycle states, dynamic DAG nodes,
  action requests, sessions, and append-only events.
- `project_hermes.store` provides atomic SQLite run persistence and leases for
  local development.
- `project_hermes.policy` authorizes every untrusted action request and confines
  repository and worktree paths.
- `project_hermes.runtime` defines the `AgentRuntime` SPI and adapters for
  upstream Hermes and task-private Codex.
- `project_hermes.candidate_review` loads the immutable per-role `SOUL.md` and
  `AGENT.md` files and runs independent completion and minimal-diff reviews.
- `project_hermes.workspaces` owns Git mirrors, exclusive worktrees, and
  deterministic candidate digests.
- `project_hermes.resource_managers` owns atomic GPU allocation and
  content-addressed artifact verification.
- `project_hermes.assurance` and `project_hermes.assurance_store` define and
  persist evidence, the completion matrix, findings, and dual review.
- `project_hermes.knowledge` enforces validate, commit, probe, delete, and
  tombstone ordering.
- `project_hermes.controller` combines policy decisions with durable
  projections.
- `project_hermes.english` enforces the English-only policy for owned files.

## Compatibility policy

ProjectHermes does not rename or replace upstream Hermes modules. Upstream
changes should be merged into the foundation first; ProjectHermes changes
remain additive except for packaging metadata, the optional dependency group,
the `project-hermes` script, and local-state ignore rules.

Existing fixed-stage run records remain readable migration inputs. New work is
written as `pipeline-run.v2`, `work-graph.v1`, and append-only
`agent-event.v1` records. See `docs/project-hermes/migration.md`.

## Verification

Run the focused extension checks:

```bash
scripts/run_tests.sh tests/project_hermes/ -q
uv run ruff check project_hermes tests/project_hermes
uv run project-hermes check-english
```

The default test suite uses fake runtime drivers and local Git repositories. It
does not send model requests or require an API key.

## Further documentation

- `docs/project-hermes/architecture.md`
- `docs/project-hermes/contracts.md`
- `docs/project-hermes/operations.md`
- `docs/project-hermes/security.md`
- `docs/project-hermes/migration.md`
- `docs/project-hermes/adr/`
