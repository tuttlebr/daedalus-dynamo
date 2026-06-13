#!/usr/bin/env bash
# Compatibility wrapper. The repository-root deploy.sh is the single
# deployment entrypoint and owns all Kubernetes configuration.

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
backend="qwen35b"
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

if [[ -n "${QWEN_IMAGE_REPOSITORY:-}" && -z "${QWEN35B_IMAGE_REPOSITORY:-}" ]]; then
  export QWEN35B_IMAGE_REPOSITORY="${QWEN_IMAGE_REPOSITORY}"
fi

exec "${repo_root}/deploy.sh" "${action}" "${backend}" "$@"
