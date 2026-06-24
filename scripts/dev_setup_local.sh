#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Run the local editable toolkit against a locally running Rebase Workflows API.

Default API:
  http://127.0.0.1:18082

Useful overrides:
  REBASE_DEV_API_URL=http://127.0.0.1:18082
  REBASE_DEV_PROFILE=local
  REBASE_DEV_PROVIDER=github
  REBASE_DEV_WORKSPACE=default
  REBASE_DEV_REPO=owner/name
  REBASE_DEV_REPO_SCOPE=workspace
  REBASE_DEV_NO_BROWSER=1
  REBASE_DEV_WAIT_TIMEOUT=30

Commands:
  ./scripts/dev_setup_local.sh --print-command   Print the commands without running them

Any extra arguments are appended to `rebase setup`.
USAGE
}

print_command=0
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi
if [[ "${1:-}" == "--print-command" ]]; then
  print_command=1
  shift
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
toolkit_dir="$(cd "${script_dir}/.." && pwd)"

bool_enabled() {
  case "${1:-}" in
    1 | true | TRUE | yes | YES | on | ON) return 0 ;;
    *) return 1 ;;
  esac
}

api_host="${REBASE_DEV_API_HOST:-127.0.0.1}"
api_port="${REBASE_DEV_API_PORT:-18082}"
api_url="${REBASE_DEV_API_URL:-http://${api_host}:${api_port}}"
health_url="${api_url%/}/health"
wait_timeout="${REBASE_DEV_WAIT_TIMEOUT:-30}"

if ! bool_enabled "${print_command}" && ! bool_enabled "${REBASE_DEV_SKIP_WAIT:-0}"; then
  echo "Waiting for local Rebase API at ${health_url}"
  start_seconds="${SECONDS}"
  while true; do
    if curl -fsS "${health_url}" >/dev/null 2>&1; then
      break
    fi
    if ((SECONDS - start_seconds >= wait_timeout)); then
      cat >&2 <<EOF
Timed out waiting for ${health_url}.

Start the local API in another terminal:
  cd ../workflows
  ./scripts/dev_api.sh
EOF
      exit 2
    fi
    sleep 1
  done
fi

args=(setup)
connect_args=(connect github)
if bool_enabled "${REBASE_DEV_FORCE_AUTH:-1}"; then
  args+=(--force-auth)
fi
args+=(--api-url "${api_url}")
connect_args+=(--api-url "${api_url}")

if [[ -n "${REBASE_DEV_PROFILE:-}" ]]; then
  args+=(--profile "${REBASE_DEV_PROFILE}")
  connect_args+=(--profile "${REBASE_DEV_PROFILE}")
fi
if [[ -n "${REBASE_DEV_PROVIDER:-}" ]]; then
  args+=(--provider "${REBASE_DEV_PROVIDER}")
fi
if [[ -n "${REBASE_DEV_WORKSPACE:-}" ]]; then
  args+=(--workspace "${REBASE_DEV_WORKSPACE}")
fi
if [[ -n "${REBASE_DEV_HANDLE:-}" ]]; then
  args+=(--handle "${REBASE_DEV_HANDLE}")
fi
if [[ -n "${REBASE_DEV_REPO:-}" ]]; then
  connect_args+=(--repo "${REBASE_DEV_REPO}")
fi
if bool_enabled "${REBASE_DEV_CREATE_REPO:-0}"; then
  connect_args+=(--create-repo)
fi
if bool_enabled "${REBASE_DEV_NO_BROWSER:-0}"; then
  args+=(--no-browser)
  connect_args+=(--no-browser)
fi

connect_github=0
if [[ -n "${REBASE_DEV_GITHUB:-}" ]]; then
  if bool_enabled "${REBASE_DEV_GITHUB}"; then
    connect_github=1
  fi
elif [[ -n "${REBASE_DEV_REPO_SCOPE:-}" || -n "${REBASE_DEV_REPO:-}" || -n "${REBASE_DEV_PROJECT:-}" ]] || bool_enabled "${REBASE_DEV_CREATE_REPO:-0}"; then
  connect_github=1
fi

cd "${toolkit_dir}"

printf 'Running: uv run rebase'
printf ' %q' "${args[@]}" "$@"
printf '\n'
if bool_enabled "${connect_github}"; then
  printf 'Then: uv run rebase'
  printf ' %q' "${connect_args[@]}"
  printf '\n'
fi
if bool_enabled "${print_command}"; then
  exit 0
fi
uv run rebase "${args[@]}" "$@"
if bool_enabled "${connect_github}"; then
  uv run rebase "${connect_args[@]}"
fi
