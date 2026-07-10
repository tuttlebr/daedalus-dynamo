#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source scripts/pre-commit/common.sh

mapfile -t files < <(
  {
    find scripts -maxdepth 1 -type f -name '*.py'
    find tests -maxdepth 1 -type f -name '*.py'
  } | sort -u
)

if [[ "${#files[@]}" -eq 0 ]]; then
  exit 0
fi

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/daedalus-precommit-pycache.XXXXXX")"
trap 'rm -rf "${tmpdir}"' EXIT

PYTHONPYCACHEPREFIX="${tmpdir}" python3 -m py_compile "${files[@]}"

if command -v ruff > /dev/null 2>&1; then
  ruff check "${files[@]}"
else
  warn "ruff not found; skipped Python linting"
fi
