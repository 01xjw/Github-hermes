# ProjectHermes Security Model

## Security objective

ProjectHermes assumes that model output, tool arguments, repository content,
issue content, artifact sources, and reviewer prose can be malicious or
incorrect. Autonomous reasoning is useful only when external authority remains
bounded and independently auditable.

The security objective is not to prove that an agent always makes a correct
technical decision. It is to prevent an agent from silently expanding scope,
escaping its worktree, acquiring unapproved resources, fabricating completion,
publishing without approval, leaking credentials through configuration, or
deleting state before durable knowledge extraction.

## Trust boundaries

The control plane and its durable policy configuration are trusted to enforce
authority. The following inputs are untrusted:

- `ActionRequest` payloads from every agent runtime;
- issue and pull-request text;
- repository files and Git history;
- commands proposed by an agent;
- paths returned by an artifact provider;
- runtime events and final responses;
- evidence claims before controller validation;
- review findings and verdicts before gate evaluation;
- goal-revision proposals;
- cleanup paths.

The official Codex SDK and pinned CLI binary are supply-chain dependencies.
Their versions are exact and independently checked at daemon startup.

## Controls

### Role and action authorization

The policy engine maps each role to an explicit action set. Unknown or
unauthorized combinations fail closed. The request task ID must match the
locked task. Execution, review, publication, and network actions also consult
the task permission budget.

An approved goal revision is not a wildcard. Repository expansion is allowed
only when that revision explicitly names the requested repository.

### Path confinement

Repository reads with paths require a controller-owned repository root.
Worktree writes require an exclusive lease and an absolute path resolving
inside its root. Git worktree creation uses controller-derived path components.
Artifact providers can return only regular files inside their assigned staging
directory. Cleanup paths must remain under the allowed private root.

Resolved paths are checked to prevent `..` and symbolic-link escapes.

### Credential isolation

Secrets are separated from durable configuration. Both the configuration
loader and the task daemon validate the credential file independently.
Credential environment names must have credential-only suffixes. This prevents
the credential channel from changing process execution with values such as
`PATH`, `PYTHONPATH`, `LD_PRELOAD`, or `CODEX_HOME`.

The daemon subprocess receives an allowlisted environment. Proxy environment
variables are intentionally excluded because proxy URLs can contain
credentials and can redirect traffic. Custom provider endpoints require
explicit network permission.

Credential values must never appear in:

- `project-hermes.yaml`;
- configuration fingerprints;
- daemon specs;
- native Codex `config.toml`;
- action events;
- evidence records;
- review records;
- command-line arguments.

### Resource ownership

Workspaces and GPUs have explicit owners. A workspace cannot be released by a
different session. A dirty worktree is not discarded implicitly. GPUs remain
allocated until an external execution object is confirmed terminated.

The Hermes runtime adapter requires a deployment-supplied attestor before it
accepts an agent. The attestor must verify task sandboxing and
controller-capability-only tool exposure. Codex daemon environments hide local
ROCm, HIP, and CUDA devices; GPU work is admitted only through the execution
coordinator.

Artifact identity is content-addressed. A source URI alone is never sufficient
to authorize execution input.

### Evidence integrity

Evidence is immutable by ID and has a stable content hash. It binds the exact
candidate, goal revision, command, environment, result, and artifact
references. A changed candidate invalidates stale completion entries.

The completion projection alone cannot close work. Two independent roles must
approve the exact candidate and goal revision. The controller rejects reused
review sessions and implementer self-approval. The frozen packet includes the
base and reviewed diff for every repository in the locked task.

### Cleanup ordering

Knowledge validation, commit, and independent probe precede every deletion.
Cleanup boundaries are validated both when the plan is created and immediately
before deletion. A minimal tombstone remains after private state is removed.

## Threat scenarios

### Prompt injection requests an out-of-scope write

The model emits a worktree write for another repository or an outside path.
The role may have write capability in principle, but repository and path
checks deny this request. The denial is recorded.

### An unrelated approved revision is reused

The model supplies a valid approved revision ID while requesting a secret
repository. The controller extracts exact repository additions from the
revision. Because the requested repository is absent, authorization fails.

### A credential file attempts process injection

The file includes `LD_PRELOAD`, `PATH`, or `PYTHONPATH`. These names do not
match the credential-only rule, so daemon startup fails before the official SDK
is created.

### An artifact provider returns an external path

The coordinator resolves the returned path and verifies that it remains inside
the private staging directory. It marks the request failed and publishes no
manifest.

### A timed-out GPU job continues running

Timeout handling may request interruption, but it does not release the GPU
lease. The controller waits for job or pod termination confirmation before
release.

### The candidate changes after validation

The deterministic candidate digest changes. Completion entries tied to the old
digest become invalidated, and old reviews are stale. Closure fails until new
evidence and reviews exist.

### Knowledge storage returns success without durable bytes

The lifecycle probes the stored object and verifies its digest before cleanup.
Probe failure preserves task-private state.

## Residual risks

The local SQLite adapter is intended for development and single-controller
operation. It does not provide cross-host consensus, database-level row
security, or production backup orchestration.

The Python process cannot by itself prove that a custom cluster backend
enforces network, filesystem, or GPU isolation. Production evidence must record
the backend policy and immutable environment identity.

Unix file modes do not defend against a compromised account with the same user
identity. Production should use separate service identities, mount namespaces,
container or pod security controls, and a dedicated secret manager.

Content digests establish byte identity, not publisher trust. Artifact source
authorization and malware policy remain controller responsibilities.

Review independence is enforced by session identity. Deployments should also
use separate model invocations, prompts, or human reviewers according to risk.

## Operator checklist

- Confirm the locked task and named baseline before work begins.
- Keep publication and network permissions false unless explicitly needed.
- Verify credentials are private, owned, untracked, and narrowly scoped.
- Use immutable image and artifact digests.
- Confirm worktree ownership before execution.
- Bind evidence to the candidate digest after the command completes.
- Assign review roles to separate, non-implementer sessions.
- Confirm external execution termination before releasing GPUs.
- Probe committed knowledge before approving cleanup.
- Review daemon logs before sharing them outside the control plane.
