# ProjectHermes Operations

## Installation

ProjectHermes extends the existing Hermes distribution. Install the dedicated
optional group:

```bash
uv sync --extra project-hermes
```

This group pins both `openai-codex` and `openai-codex-cli-bin` to `0.144.4`.
The daemon checks both installed versions before it starts a native Codex
client.

For the Kubernetes deployment, do not run this mutable source-install path.
Use the checksum-verified, versioned release and rollback procedure in
[`release.md`](release.md); cluster dependency installation is offline from
the packaged wheelhouse.

## Configuration

Start from `project-hermes.example.yaml` or generate defaults:

```bash
uv run project-hermes init --config project-hermes.yaml
uv run project-hermes validate-config project-hermes.yaml
```

Relative paths resolve against the configuration file directory. The
configuration fingerprint is stable and redacts the credential-file path to a
configured marker.

SQLite is the executable local persistence adapter. PostgreSQL and HTTP are
reserved control-plane modes; deployments must register a compatible
`RunStore` adapter before selecting them.

## Credential delivery

The main configuration contains only a path to a credential file. It never
contains an API key.

The credential file must:

- be inside `project_root`;
- be a regular file, not a symbolic link;
- be owned by the current user;
- have mode `0600` or stricter;
- be no larger than 64 KiB;
- remain untracked by Git;
- contain only an `environment` mapping.

Only credential-shaped uppercase names are accepted, such as
`OPENAI_API_KEY`, `CODEX_ACCESS_TOKEN`, or `PRIVATE_PROVIDER_API_KEY`.
Process-control names such as `PATH`, `HOME`, `PYTHONPATH`, `LD_PRELOAD`, and
`CODEX_HOME` are rejected.

Example:

```yaml
environment:
  OPENAI_API_KEY: replace-with-an-operator-provided-key
```

The daemon receives a sanitized process environment. It does not inherit
provider keys or proxy URLs from the controller process. The official Codex
app-server receives the task-specific `CODEX_HOME`,
`CODEX_SQLITE_HOME`, and the validated credential mapping.

## Task-private Codex lifecycle

Provisioning requires:

1. Codex enabled with reviewed file-mount credentials;
2. an existing exclusive worktree;
3. a task-safe identifier;
4. a runtime root short enough for a Unix socket;
5. exact SDK and CLI versions.

The supervisor creates a private task directory, writes `daemon-spec.json`,
and launches:

```bash
python -m project_hermes.runtime.codex_daemon --spec PATH
```

The daemon binds a mode `0600` Unix socket and initializes one official Codex
client and one root thread. The supervisor waits for a successful status
request before returning.

Subsequent controller calls are short-lived socket requests. `events` and
`result` are reads; `run_turn` is serialized by a non-blocking task lock;
`cancel` interrupts the active native turn; `shutdown` closes the SDK client
and removes the socket.

### Restart recovery

The task-to-thread mapping is stored in `session.json`. When a daemon restarts,
it validates the task identity, worktree, and immutable runtime policy, then
calls the SDK's thread-resume operation with the recorded root thread ID.

A stale socket is removed only after a status request proves it unreachable.
A changed model, endpoint, credential path, network setting, or version is not
silently applied to a live task. Stop and reprovision the daemon.

### Diagnosing startup

Run:

```bash
uv run project-hermes doctor project-hermes.yaml
```

If a daemon exits during startup, inspect its private `daemon.log`. Do not copy
the log into an issue without reviewing it for task content. Credential values
are not intentionally logged, but model output and provider errors can still
be sensitive.

Common failures:

- a socket path at or above the platform limit: shorten `codex.runtime_root`;
- missing or mismatched pinned packages: synchronize the
  `project-hermes` extra;
- credential permission failure: restore mode `0600` and correct ownership;
- custom endpoint without explicit network access: review and enable network
  access for that task;
- custom endpoint key mismatch: make `provider_api_key_env` match a key in the
  private credential file.

## Git workspace lifecycle

`GitWorkspaceManager` stores private mirrors, worktrees, and lease metadata
under one controller root.

Acquisition:

1. validate the `owner/name` repository identity;
2. clone or fetch the controller mirror with terminal prompts disabled;
3. resolve `base_ref` to an immutable commit;
4. create a unique branch and exclusive worktree;
5. persist a mode `0600` lease record.

The manager never updates global or system Git configuration. The candidate
digest covers the baseline, binary tracked diff, untracked path names, file
bytes, and safe symbolic-link targets without staging changes.

Release checks the owner session. A dirty worktree is preserved unless the
caller explicitly requests discard. Publication and cleanup policy should
retain the branch or an immutable patch artifact before discard.

## Artifact supply

The controller creates an artifact request with a trusted expected digest.
The provider writes one file into a private staging path. The coordinator
rejects paths outside staging, directories, and digest mismatches. Verified
content is atomically moved to:

```text
ARTIFACT_ROOT/sha256/PREFIX/FULL_DIGEST
```

Consumers receive only the immutable artifact manifest. Failed requests remain
failed and require a new request ID.

## GPU lifecycle

GPU allocation is all-or-nothing. A request that cannot be satisfied returns
no lease. Allocated and terminating leases keep their devices unavailable.

To release:

1. request execution termination;
2. confirm the external job or pod has terminated;
3. mark the lease terminating when useful for observation;
4. call release with independent termination confirmation.

Do not release from a wall-clock timeout alone. A timed-out command may still
own a live GPU process.

## Evidence and review operations

Persist evidence immediately after a controlled action. Store logs and large
artifacts externally; put immutable references and digests in the evidence
record.

After any candidate change:

```python
invalidated = assurance_store.invalidate_stale_completion(
    task_id,
    code_diff_sha=new_digest,
)
```

Re-run only the layers whose evidence is no longer valid. Then assign the two
review roles to distinct sessions that did not implement the candidate.
Closure uses `ProjectHermesController.candidate_gate`.

## Cleanup operations

Never delete a task merely because its pull request reached a terminal state.
First extract knowledge from both successful and unsuccessful outcomes. Run an
independent validator, commit the accepted batch, and probe it from durable
storage. Only then execute a `CleanupPlan`.

The cleanup plan rejects the allowed root itself, paths outside the root, and
a tombstone path inside a directory scheduled for deletion.

## Backup and retention

For local SQLite:

- back up the database using SQLite's online backup mechanism;
- retain the WAL and shared-memory files only as part of a consistent backup;
- retain external evidence and knowledge objects by digest;
- retain cleanup tombstones according to audit policy;
- treat task runtime directories as private and temporary.

Production deployments should put runs, graph nodes, events, completion
matrices, reviews, resource leases, and tombstones in controller-owned durable
storage. Task-private Codex state remains local to the task runtime but must be
restartable on its assigned host.
