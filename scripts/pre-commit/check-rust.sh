#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source scripts/pre-commit/common.sh

if ! command -v cargo > /dev/null 2>&1; then
  warn "cargo not found; skipped Rust Photo Tagger checks"
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

photo_tagger_native_deps_available() {
  native_linker_available || return 1
  command -v pkg-config > /dev/null 2>&1 || return 1
  pkg-config --exists 'libheif >= 1.17'
}

manifest="photo-tagger/Cargo.toml"
if [[ "$#" -gt 0 ]]; then
  selected=0
  for file in "$@"; do
    case "${file}" in
      photo-tagger/*) selected=1 ;;
    esac
  done
  [[ "${selected}" -eq 1 ]] || exit 0
fi

echo "==> cargo fmt --check ${manifest}"
cargo fmt --manifest-path "${manifest}" -- --check

if ! photo_tagger_native_deps_available; then
  warn "native Photo Tagger deps missing; skipped cargo test. Install build-essential, pkg-config, libheif-dev, and zlib1g-dev."
  exit 0
fi

echo "==> cargo test ${manifest}"
cargo test --manifest-path "${manifest}"
