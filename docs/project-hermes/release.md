# ProjectHermes Immutable Release Operations

`Github_Hermes` is the only release source of truth. Release creation, web
compilation, wheel download, and manifest rendering happen on the local build
host. The Kubernetes installer accepts only the resulting checksummed bundle;
it never installs from a mutable checkout and never resolves a dependency from
the network.

## Release contents

One release directory contains:

- `source.tar.gz`: Git-tracked and non-ignored untracked `Github_Hermes`
  source, with normalized ownership, modes, ordering, and timestamps;
- `web.tar.gz`: the prebuilt dashboard bundle;
- `wheelhouse.tar.gz`: the application wheel and complete Python 3.12 Linux
  wheel set, plus a hash-locked offline install file;
- `worker.tar.gz`: the worker runtime preparation and launch scripts;
- `release-manifest.json`: source identity, target platform, immutable runtime
  image, exact Codex versions, artifact sizes, and artifact digests;
- `SHA256SUMS` and `RELEASE_DIGEST`.

The release digest is the SHA-256 digest of the canonical release manifest.
The source archive includes the current working tree, so review all uncommitted
and untracked files before packaging. Build output must be outside the
`Github_Hermes` tree.

The packager verifies byte parity for the shared `project_hermes/` and
`tests/project_hermes/` trees against the sibling `ProjectHermes` checkout.
When parity fails, reconcile the downstream copy from `Github_Hermes`; do not
replace release-source files from the sibling checkout.

## Safe local checks

Resolve the existing cluster runtime image to an immutable OCI digest before
packaging. Use the complete `registry/repository@sha256:...` value:

```bash
cd /home/jixiong/Radeon/github-agent/Github_Hermes
RUNTIME_IMAGE='registry.example/base@sha256:<64-lowercase-hex>'
python scripts/package_project_hermes_release.py build \
  --runtime-image "${RUNTIME_IMAGE}" \
  --dry-run
```

The dry run checks the exact `0.144.4` Codex pins, shared-tree parity, source
inventory, runtime image shape, and deterministic epoch. It prints the build
commands but creates no directories, installs nothing, and performs no network
request.

Static checks that do not build the release are:

```bash
python -m py_compile \
  scripts/package_project_hermes_release.py \
  scripts/render_project_hermes_manifests.py \
  deploy/release-worker/connect-proxy.py \
  deploy/release-worker/execute-task.py \
  deploy/release-worker/prepare-source.py
bash -n deploy/release-worker/prepare-runtime.sh
bash -n deploy/release-worker/run.sh
python - <<'PY'
from pathlib import Path
import yaml

for path in sorted(Path("deploy/kubernetes").glob("*.yaml")):
    list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    print(path)
PY
```

These checks deliberately do not run the application test suite or compile the
web UI.

## Deferred local build

Run this only in the later release-execution session:

```bash
python scripts/package_project_hermes_release.py build \
  --runtime-image "${RUNTIME_IMAGE}" \
  --output-dir ../project-hermes-releases
BUNDLE='../project-hermes-releases/project-hermes-REPLACE_WITH_PRINTED_DIGEST'
python scripts/package_project_hermes_release.py validate "${BUNDLE}"
```

Set `BUNDLE` to the exact path printed by the packager. To check
reproducibility, build twice from the same bytes, runtime image, platform, and
`SOURCE_DATE_EPOCH` into two empty output roots, then compare `RELEASE_DIGEST`
and every line of `SHA256SUMS`.

Render apply-ready manifests from the verified bundle:

```bash
python scripts/render_project_hermes_manifests.py "${BUNDLE}" \
  --output-dir ../project-hermes-rendered
RELEASE_DIGEST="$(cat "${BUNDLE}/RELEASE_DIGEST")"
RENDERED="../project-hermes-rendered/project-hermes-${RELEASE_DIGEST}"
```

Templates in `deploy/kubernetes/` intentionally contain `@...@` tokens and
must not be applied directly. The renderer gets both the release digest and
digest-pinned runtime image from the verified release manifest, including the
model-proxy Deployment in `github-agent-job-runner.yaml`.

Client-only manifest parsing is safe before handoff:

```bash
kubectl apply --dry-run=client --validate=false \
  -f "${RENDERED}/github-agent-job-runner.yaml"
kubectl apply --dry-run=client --validate=false \
  -f "${RENDERED}/install-job.yaml"
kubectl apply --dry-run=client --validate=false \
  -f "${RENDERED}/github-agent-pod.yaml"
```

Do not use these commands without `--dry-run=client` in the implementation
session.

## Cluster handoff

The following is an execution-session checklist, not an instruction to run
during packaging:

1. Apply `github-agent-job-runner.yaml` to create the shared ProjectHermes
   namespace, accounts, eight-Job/six-GPU quota, policies, proxy, and PVC.
2. Create `project-hermes-credentials` in `project-hermes-jobs` from
   operator-owned `model-access-key`, `minimax-api-key`, and
   `deepseek-api-key` files. Never place a key in a manifest or release. The
   installer writes the DigitalOcean, MiniMax, and DeepSeek keys into the
   controller's private credentials file. The active Codex Responses and
   continuity-handoff routes both select the official DeepSeek credential.
   Worker Jobs still resolve the two independently locked routes, and the Codex
   subprocess does not inherit a distinct handoff key if an operator later
   configures one.
3. Delete any completed `project-hermes-install` Job and apply the rendered
   `install-job.yaml`.
4. Locate its Pod, copy all bundle files into `/staging`, then copy
   `RELEASE_DIGEST` to `/staging/upload.complete` last. The last copy is the
   installer commit marker.
5. Wait for the Job to succeed. A failed checksum, unsafe archive path,
   unexpected artifact, Codex version mismatch, or release/image mismatch
   fails closed before activation.
6. Delete the old `github-agent` Pod and apply the rendered
   `github-agent-pod.yaml`.

The installer extracts to
`releases/releases/<release-digest>`, performs a fully offline
`pip --no-index --no-deps --require-hashes` install into that versioned
directory, makes it non-writable, and atomically replaces the `current`
symlink. The former `current` target becomes `previous`; older release
directories are not garbage-collected automatically.

Runtime mounts are separated:

- `releases` is mounted read-only and contains source, web assets, wheels,
  worker artifact, and installed Python packages;
- `state` is writable for controller and dashboard state;
- `worktrees` is a separate writable mount at the controller workspace root;
- `logs` is a separate writable mount under `HERMES_HOME`.

Worker Jobs mount the exact
`releases/releases/<release-digest>` subpath read-only, mount an `emptyDir` at
`PROJECT_HERMES_WORKER_RUNTIME`, and run
`worker/prepare-runtime.sh` as their init step. The script verifies the mounted
manifest digest, installs only from the packaged hash-locked wheelhouse, and
checks both Codex distributions are exactly `0.144.4`. The main container
launches through `worker/run.sh`; package installation from a registry is not
permitted in either container.

Each Issue worker also mounts exactly one content-addressed source archive as
a read-only file. A second init container verifies the archive digest and its
embedded task, repository, lease, base, and candidate identities before safely
extracting it into an isolated `emptyDir` worktree. The main container has no
service-account token or GitHub credential. The independently allowlisted Codex
Responses and continuity Chat routes currently use `deepseek-v4-pro` at the
official DeepSeek endpoint and receive `DEEPSEEK_API_KEY` directly from the
Kubernetes Secret. If those routes use distinct credentials in a future
operator-reviewed profile, the handoff key remains in the worker wrapper and is
not inherited by the Codex subprocess. Before archiving, the wrapper redacts
every selected credential value from every core artifact; all remaining route
metadata is credential-free.

The worker NetworkPolicy permits only cluster DNS and the internal
`project-hermes-model-proxy` Service. The production proxy accepts only
`CONNECT` for `api.deepseek.com:443`, rejects proxy credentials and every other
authority, resolves the upstream itself, and refuses non-public addresses. The
proxy implementation retains an explicit operator-reviewed DigitalOcean
authority for future profiles, but the production manifest does not enable it.
Its own
NetworkPolicy permits DNS and public TCP 443. Direct worker access to GitHub or
any Internet IP is therefore unavailable.

The worker wrapper always captures stdout, stderr, `result.json`, and
`usage.json`. It stores each object by SHA-256 and writes a completion marker
containing the archive-manifest digest. Successful and failed Jobs have no
automatic TTL. The controller re-verifies the marker, task/release/image/source
identity, byte sizes, and every object digest before deleting the Job and its
per-execution NetworkPolicy. Missing or damaged archives leave the terminal
Job and its available logs in place for recovery.

The Pod mounts the rendered versioned release subpath directly and refuses to
start when that release's digest differs from the manifest. The digest is also
stored as the
`project-hermes.io/release-digest` Pod annotation and in
`$HERMES_HOME/release.json`, which is visible in the dashboard Files view.

## Dashboard access and verification

The deployment intentionally has no login configuration. The dashboard binds
only `127.0.0.1` inside the Pod, all probes are exec probes against loopback,
and there is no Service. Access it only through:

```bash
kubectl -n project-hermes-jobs port-forward pod/github-agent 8770:8770
```

After activation, verify the Pod annotation, `release.json`, and read-only
source mount agree with the expected digest. Do not expose port 8770 through a
Service, ingress, host port, or non-loopback dashboard bind.

## Rollback

Read `previous_release_digest` from the active release record and locate that
release's original local bundle. Render the rollback Job and Pod from the
previous bundle so the rolled-back Pod also uses that release's recorded
runtime image:

```bash
PREVIOUS_BUNDLE=/operator/archive/project-hermes-<previous-digest>
PREVIOUS_DIGEST="$(cat "${PREVIOUS_BUNDLE}/RELEASE_DIGEST")"
python scripts/render_project_hermes_manifests.py "${PREVIOUS_BUNDLE}" \
  --rollback-digest "${PREVIOUS_DIGEST}" \
  --output-dir ../project-hermes-rollback-rendered
ROLLBACK_RENDERED="../project-hermes-rollback-rendered/project-hermes-${PREVIOUS_DIGEST}"
kubectl apply --dry-run=client --validate=false \
  -f "${ROLLBACK_RENDERED}/rollback-job.yaml"
kubectl apply --dry-run=client --validate=false \
  -f "${ROLLBACK_RENDERED}/github-agent-pod.yaml"
```

In the later execution session, delete any old `project-hermes-rollback` Job,
apply `rollback-job.yaml`, and wait for success. The Job accepts only the
retained `previous` target, verifies its manifest digest, atomically activates
it, and retains the formerly active release as the new `previous`. Then delete
and recreate `github-agent` from the rendered previous-release Pod manifest.
Recheck the annotation and `$HERMES_HOME/release.json`.
