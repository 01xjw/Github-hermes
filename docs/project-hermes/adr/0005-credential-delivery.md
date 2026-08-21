# ADR 0005: Deliver Credentials Through a Private File

Status: Accepted

## Context

Task-private Codex daemons need provider authentication, including support for
operator-configured endpoints. Putting API keys in the main YAML,
command-line arguments, daemon specs, or native Codex configuration would make
them durable, easy to commit, or visible to other processes.

Inheriting the controller environment would also pass unrelated credentials
and process-control values into autonomous runtimes.

## Decision

Keep Codex disabled by default. Enabling it requires `file_mount` credential
delivery and a private credential file referenced by path from the main
configuration.

The file must be owned by the current user, mode `0600` or stricter, untracked,
inside the project root, non-symlink, and at most 64 KiB. It may contain only
an `environment` mapping.

Accept only credential-shaped names. Reject process-control and location names.
Launch the daemon with an allowlisted environment and do not inherit proxy
variables. Pass validated credentials to the official SDK through its child
environment, never through argv or generated `config.toml`.

Custom endpoints require an explicit model provider, key environment name, and
network permission.

## Consequences

Operators can supply keys later without changing tracked configuration.
Credential delivery is narrow and independently checked by both configuration
loading and daemon startup.

Local file modes assume a trustworthy operating-system user boundary.
Production can replace the file with a secret-manager-backed mount while
retaining the same daemon contract.

Interactive shared ChatGPT login is not implicitly reused because every task
has a private `CODEX_HOME`.

## Rejected alternatives

Keys in the main configuration were rejected because the file is expected to
be tracked and reviewed.

Keys in command-line arguments were rejected because process listings and
logs can expose them.

Inheriting the complete controller environment was rejected because unrelated
provider keys and injection-sensitive variables would cross the task boundary.

A shared Codex authentication home was rejected because it would couple task
identity and cleanup.
