#!/usr/bin/env bash
# Compatibility wrapper. The repository-root deploy.sh is the single
# deployment entrypoint and owns all Kubernetes configuration.

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
backend="gemma431b"
action="${ACTION:-deploy}"

if [[ "$#" -gt 0 ]]; then
  case "$1" in
    deploy|delete|render|install-gateway)
      action="$1"
      shift
      ;;
    -h|--help|help)
      exec "${repo_root}/deploy.sh" "$1"
      ;;
  esac
fi

if [[ -n "${GEMMA_IMAGE_REPOSITORY:-}" && -z "${GEMMA431B_IMAGE_REPOSITORY:-}" ]]; then
  export GEMMA431B_IMAGE_REPOSITORY="${GEMMA_IMAGE_REPOSITORY}"
fi

exec "${repo_root}/deploy.sh" "${action}" "${backend}" "$@"
