#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source scripts/pre-commit/common.sh

if ! command -v kubectl > /dev/null 2>&1; then
  warn "kubectl not found; skipped Kustomize render checks"
  exit 0
fi

status=0
for example in "${EXAMPLES[@]}"; do
  while IFS= read -r kustomization; do
    dir="$(dirname "${kustomization}")"
    echo "==> kubectl kustomize ${dir}"
    if ! kubectl kustomize "${dir}" > /dev/null; then
      status=1
    fi
  done < <(find "${example}/k8s" -name kustomization.yaml -type f | sort)
done

exit "${status}"
