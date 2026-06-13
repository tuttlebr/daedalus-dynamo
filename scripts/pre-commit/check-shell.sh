#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source scripts/pre-commit/common.sh

mapfile -t scripts < <(
  find "${EXAMPLES[@]}" scripts -type f -name '*.sh' 2> /dev/null | sort
)

if [[ -f deploy.sh ]]; then
  scripts=(deploy.sh "${scripts[@]}")
fi

if [[ "${#scripts[@]}" -eq 0 ]]; then
  exit 0
fi

for script in "${scripts[@]}"; do
  bash -n "${script}"
done

if command -v shellcheck > /dev/null 2>&1; then
  shellcheck "${scripts[@]}"
else
  warn "shellcheck not found; skipped shell linting"
fi
