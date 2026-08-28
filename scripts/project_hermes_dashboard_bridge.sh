#!/usr/bin/env bash
set -euo pipefail

# Temporary management-host connector for clusters whose nodes cannot keep a
# stable Cloudflare Tunnel connection. The tunnel credential is fetched into a
# mode-0600 temporary directory and removed when the bridge exits.

bridge_session="project-hermes-dashboard-bridge"
bridge_namespace="${PROJECT_HERMES_NAMESPACE:-project-hermes-jobs}"
bridge_deployment="${PROJECT_HERMES_DASHBOARD_DEPLOYMENT:-github-agent}"
bridge_configmap="${PROJECT_HERMES_CLOUDFLARED_CONFIGMAP:-github-agent-cloudflared}"
bridge_secret="${PROJECT_HERMES_CLOUDFLARED_SECRET:-github-agent-cloudflared}"
bridge_local_port="${PROJECT_HERMES_BRIDGE_LOCAL_PORT:-18770}"
bridge_metrics_port="${PROJECT_HERMES_BRIDGE_METRICS_PORT:-22000}"
bridge_log="${PROJECT_HERMES_BRIDGE_LOG:-/tmp/project-hermes-dashboard-bridge.log}"
bridge_public_url="${PROJECT_HERMES_DASHBOARD_URL:-https://hermes.oneclickamd.ai/}"
bridge_script="$(realpath "${BASH_SOURCE[0]}")"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "required command not found: $1" >&2
    exit 127
  fi
}

bridge_status() {
  if tmux has-session -t "${bridge_session}" 2>/dev/null; then
    echo "bridge process: running"
  elif curl -fsS --max-time 2 "http://127.0.0.1:${bridge_metrics_port}/ready" >/dev/null 2>&1; then
    echo "bridge process: running under an external supervisor"
  else
    echo "bridge process: stopped"
  fi

  if curl -fsS --max-time 5 "http://127.0.0.1:${bridge_metrics_port}/ready" >/dev/null 2>&1; then
    echo "cloudflared connector: ready"
  else
    echo "cloudflared connector: unavailable"
  fi

  status_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "${bridge_public_url}" || true)"
  echo "public dashboard: HTTP ${status_code:-000}"
  echo "log: ${bridge_log}"
}

run_bridge() {
  umask 077
  bridge_runtime_dir="$(mktemp -d /tmp/project-hermes-dashboard-bridge.XXXXXX)"
  bridge_credentials_file="${bridge_runtime_dir}/credentials.json"
  bridge_config_file="${bridge_runtime_dir}/config.yml"
  forward_pid=""
  tunnel_pid=""

  cleanup() {
    if [[ -n "${tunnel_pid}" ]]; then
      kill "${tunnel_pid}" 2>/dev/null || true
      wait "${tunnel_pid}" 2>/dev/null || true
    fi
    if [[ -n "${forward_pid}" ]]; then
      kill "${forward_pid}" 2>/dev/null || true
      wait "${forward_pid}" 2>/dev/null || true
    fi
    if [[ -f "${bridge_credentials_file}" ]]; then
      unlink -- "${bridge_credentials_file}"
    fi
    if [[ -f "${bridge_config_file}" ]]; then
      unlink -- "${bridge_config_file}"
    fi
    rmdir -- "${bridge_runtime_dir}" 2>/dev/null || true
  }
  trap cleanup EXIT HUP INT TERM

  kubectl -n "${bridge_namespace}" get secret "${bridge_secret}" \
    -o jsonpath='{.data.credentials\.json}' \
    | base64 --decode > "${bridge_credentials_file}"
  chmod 600 "${bridge_credentials_file}"

  tunnel_id="$(
    kubectl -n "${bridge_namespace}" get configmap "${bridge_configmap}" \
      -o jsonpath='{.data.config\.yml}' \
      | awk '$1 == "tunnel:" { print $2; exit }'
  )"
  if [[ -z "${tunnel_id}" ]]; then
    echo "could not resolve the named Tunnel ID" >&2
    exit 65
  fi

  bridge_hostname="${bridge_public_url#*://}"
  bridge_hostname="${bridge_hostname%%/*}"
  bridge_hostname="${bridge_hostname%%:*}"
  if [[ -z "${bridge_hostname}" ]]; then
    echo "could not resolve the dashboard hostname" >&2
    exit 65
  fi

  # Use an explicit config so an unrelated ~/.cloudflared/config.yml cannot
  # override --url and send this hostname to a catch-all HTTP 404 ingress.
  {
    printf 'tunnel: %s\n' "${tunnel_id}"
    printf 'credentials-file: %s\n' "${bridge_credentials_file}"
    printf 'protocol: http2\n'
    printf 'metrics: 127.0.0.1:%s\n' "${bridge_metrics_port}"
    printf 'ingress:\n'
    printf '  - hostname: %s\n' "${bridge_hostname}"
    printf '    service: http://127.0.0.1:%s\n' "${bridge_local_port}"
    printf '    originRequest:\n'
    printf '      connectTimeout: 30s\n'
    printf '      httpHostHeader: 127.0.0.1\n'
    printf '  - service: http_status:404\n'
  } > "${bridge_config_file}"
  chmod 600 "${bridge_config_file}"

  while true; do
    kubectl -n "${bridge_namespace}" port-forward \
      "deployment/${bridge_deployment}" \
      "${bridge_local_port}:8770" \
      --address 127.0.0.1 >> "${bridge_log}" 2>&1 &
    forward_pid="$!"

    origin_ready=false
    for _attempt in $(seq 1 30); do
      if curl -fsS --max-time 2 "http://127.0.0.1:${bridge_local_port}/" >/dev/null 2>&1; then
        origin_ready=true
        break
      fi
      if ! kill -0 "${forward_pid}" 2>/dev/null; then
        break
      fi
      sleep 1
    done

    if [[ "${origin_ready}" != "true" ]]; then
      kill "${forward_pid}" 2>/dev/null || true
      wait "${forward_pid}" 2>/dev/null || true
      forward_pid=""
      sleep 3
      continue
    fi

    cloudflared tunnel \
      --config "${bridge_config_file}" \
      --no-autoupdate \
      --loglevel info \
      run \
      "${tunnel_id}" >> "${bridge_log}" 2>&1 &
    tunnel_pid="$!"

    while kill -0 "${forward_pid}" 2>/dev/null && kill -0 "${tunnel_pid}" 2>/dev/null; do
      sleep 5
    done

    kill "${tunnel_pid}" "${forward_pid}" 2>/dev/null || true
    wait "${tunnel_pid}" 2>/dev/null || true
    wait "${forward_pid}" 2>/dev/null || true
    tunnel_pid=""
    forward_pid=""
    sleep 3
  done
}

require_command kubectl
require_command cloudflared
require_command curl
require_command tmux

case "${1:-status}" in
  start)
    if curl -fsS --max-time 2 "http://127.0.0.1:${bridge_metrics_port}/ready" >/dev/null 2>&1; then
      echo "dashboard bridge is already running under an external supervisor"
    elif tmux has-session -t "${bridge_session}" 2>/dev/null; then
      echo "dashboard bridge is already running"
    else
      tmux new-session -d -s "${bridge_session}" "${bridge_script} run"
      echo "dashboard bridge started"
    fi
    bridge_status
    ;;
  stop)
    if tmux has-session -t "${bridge_session}" 2>/dev/null; then
      tmux kill-session -t "${bridge_session}"
      echo "dashboard bridge stopped"
    else
      echo "dashboard bridge is not running"
    fi
    ;;
  restart)
    if tmux has-session -t "${bridge_session}" 2>/dev/null; then
      tmux kill-session -t "${bridge_session}"
    fi
    tmux new-session -d -s "${bridge_session}" "${bridge_script} run"
    echo "dashboard bridge restarted"
    bridge_status
    ;;
  status)
    bridge_status
    ;;
  run)
    run_bridge
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status}" >&2
    exit 64
    ;;
esac
