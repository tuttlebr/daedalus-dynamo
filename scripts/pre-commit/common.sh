#!/usr/bin/env bash
set -euo pipefail

readonly EXAMPLES=(
  "nemotron-3-nano-omni-dynamo"
  "qwen-36-35b-dynamo"
  "qwen-36-27b-dynamo"
)

warn() {
  echo "WARN: $*" >&2
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

is_text_file() {
  local file="$1"
  [[ -f "${file}" ]] && LC_ALL=C grep -Iq . "${file}"
}
