#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source scripts/pre-commit/common.sh

if ! command -v cargo > /dev/null 2>&1; then
  warn "cargo not found; skipped Rust Routing Workbench checks"
  exit 0
fi

native_linker_available() {
  local cc_string="${CC:-cc}"
  local -a cc_cmd
  local tmpdir

  read -r -a cc_cmd <<< "${cc_string}"
  tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/daedalus-link-check.XXXXXX")"
  printf 'int main(void) { return 0; }\n' > "${tmpdir}/main.c"
  if "${cc_cmd[@]}" "${tmpdir}/main.c" -o "${tmpdir}/main" > /dev/null 2>&1; then
    rm -rf "${tmpdir}"
    return 0
  fi
  rm -rf "${tmpdir}"
  return 1
}

manifest="routing-workbench/Cargo.toml"
if [[ "$#" -gt 0 ]]; then
  selected=0
  for file in "$@"; do
    case "${file}" in
      routing-workbench/*) selected=1 ;;
    esac
  done
  [[ "${selected}" -eq 1 ]] || exit 0
fi

echo "==> cargo fmt --check ${manifest}"
cargo fmt --manifest-path "${manifest}" -- --check

if ! native_linker_available; then
  warn "native linker prerequisites missing; skipped cargo test. Install build-essential and libc6-dev."
  exit 0
fi

echo "==> cargo test ${manifest}"
cargo test --manifest-path "${manifest}"
