#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
namespace="project-hermes-jobs"
config_map="digitalocean-429-concurrent-probe"

command -v kubectl >/dev/null

kubectl -n "${namespace}" create configmap "${config_map}" \
  --from-file=digitalocean_429_concurrent_probe.py="${script_dir}/digitalocean_429_concurrent_probe.py" \
  --dry-run=client \
  -o yaml | kubectl apply -f -

kubectl apply -f "${repo_root}/deploy/kubernetes/digitalocean-429-concurrent-probe.yaml"
kubectl -n "${namespace}" rollout restart deployment/"${config_map}"
kubectl -n "${namespace}" rollout status deployment/"${config_map}" --timeout=180s
