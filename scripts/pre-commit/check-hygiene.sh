#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source scripts/pre-commit/common.sh

status=0

for file in "$@"; do
  [[ -e "${file}" ]] || continue

  case "${file}" in
    routing-workbench/target/*|*/routing-workbench/target/*|*/__pycache__/*|*.pyc|*.pyo|*.pyd|*.so|*.rlib|*.rmeta)
      echo "Generated build artifact is not allowed: ${file}" >&2
      status=1
      continue
      ;;
    .env|.env.*|*/.env|*/.env.*|*.kubeconfig|*/kubeconfig)
      echo "Local environment or kubeconfig file is not allowed: ${file}" >&2
      status=1
      continue
      ;;
    rendered*.yaml|*/rendered*.yaml|*-base.yaml|*-daedalus.yaml|*-aiperf.yaml|*-dynosim-aiperf.yaml)
      echo "Rendered manifest artifact is not allowed: ${file}" >&2
      status=1
      continue
      ;;
    *.safetensors|*.ckpt|*.pt|*.pth|*.gguf|*.onnx)
      echo "Model/checkpoint artifact is not allowed: ${file}" >&2
      status=1
      continue
      ;;
  esac

  is_text_file "${file}" || continue

  if grep -nE '[[:blank:]]$' "${file}" > /dev/null; then
    echo "Trailing whitespace found in ${file}" >&2
    status=1
  fi

  if [[ -s "${file}" ]] && [[ "$(tail -c 1 "${file}")" != "" ]]; then
    echo "Missing final newline: ${file}" >&2
    status=1
  fi

  if grep -nE '^(<<<<<<<|=======|>>>>>>>)($| )' "${file}" > /dev/null; then
    echo "Merge-conflict marker found in ${file}" >&2
    status=1
  fi
done

exit "${status}"
