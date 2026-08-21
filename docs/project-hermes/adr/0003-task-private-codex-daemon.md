# ADR 0003: Use One Task-Private Codex Daemon

Status: Accepted

## Context

Codex benefits from a persistent root thread, native app-server state, and
restart recovery. Sharing one global app-server across tasks would mix
credentials, SQLite state, worktree access, cancellation, and thread
ownership. Starting a new process for every turn would lose continuity and
make cancellation unreliable.

The official Python SDK already owns Codex app-server protocol details over
stdio.

## Decision

Provision exactly one long-lived daemon per ProjectHermes task.

Each daemon owns:

- one official `openai-codex==0.144.4` client;
- one pinned `openai-codex-cli-bin==0.144.4` app-server;
- one root Codex thread;
- one exclusive worktree;
- one private Unix socket;
- one `CODEX_HOME`;
- one `CODEX_SQLITE_HOME`;
- durable task-to-thread metadata, event log, and latest result.

The controller uses short-lived newline-delimited JSON requests over the
private socket. It does not implement native Codex JSON-RPC.

Task runtime configuration is immutable while the daemon is live. Restart
resumes the stored root thread. A turn lock permits only one root turn at a
time.

## Consequences

Task state and cancellation are isolated. The official SDK remains the single
owner of app-server protocol compatibility.

The deployment must support Unix sockets and keep runtime paths short enough
for platform limits. A supervisor is required to detect stale sockets and
recover daemons.

One daemon per task consumes more processes than a global client, but task
concurrency is explicitly bounded.

SDK and CLI upgrades require a reviewed version bump, lockfile update, schema
compatibility check, and restart policy.

## Rejected alternatives

A global Codex app-server was rejected because task identity and credentials
would share a failure and authorization boundary.

One process per turn was rejected because it discards native continuity and
durable cancellation.

Reimplementing app-server JSON-RPC in the controller was rejected because it
would duplicate the official SDK and increase protocol drift.

TCP transport was rejected for the local task boundary because a private Unix
socket provides filesystem ownership and avoids an exposed listening port.
