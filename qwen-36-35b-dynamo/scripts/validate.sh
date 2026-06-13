#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

STRICT="${STRICT:-0}"
SERVER_DRY_RUN="${SERVER_DRY_RUN:-0}"
NAMESPACE="${NAMESPACE:-qwen36-35b}"

warn_or_fail() {
  local message="$1"
  if [[ "${STRICT}" == "1" ]]; then
    echo "ERROR: ${message}" >&2
    exit 1
  fi
  echo "WARN: ${message}" >&2
}

native_linker_available() {
  local cc_string="${CC:-cc}"
  local -a cc_cmd
  local tmpdir
  local output

  read -r -a cc_cmd <<< "${cc_string}"
  tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/qwen36-link-check.XXXXXX")"
  printf 'int main(void) { return 0; }\n' > "${tmpdir}/main.c"

  if ! output="$("${cc_cmd[@]}" "${tmpdir}/main.c" -o "${tmpdir}/main" 2>&1)"; then
    rm -rf "${tmpdir}"
    warn_or_fail "native C linker prerequisites are missing; ${cc_string} cannot link a minimal C program. Install build-essential and libc6-dev on Ubuntu/Debian, then rerun make validate."
    return 1
  fi

  rm -rf "${tmpdir}"
  return 0
}

echo "==> Checking shell syntax"
for script in deploy.sh test-endpoint.sh scripts/validate.sh ../deploy.sh ../routing-workbench/deploy.sh; do
  bash -n "${script}"
done

echo "==> Checking Python benchmark scripts"
PYTHONPYCACHEPREFIX=/tmp/qwen36-pycache python3 -m py_compile \
  k8s/benchmarks/aiperf/run-aiperf-router-demo.py \
  k8s/benchmarks/aiperf/sitecustomize.py \
  k8s/benchmarks/dynosim-aiperf/run-dynosim-aiperf-demo.py \
  k8s/benchmarks/dynosim-aiperf/sitecustomize.py

if command -v kubectl > /dev/null 2>&1; then
  echo "==> Rendering Kubernetes manifests"
  kubectl kustomize k8s/base > /tmp/qwen36-base.yaml
  kubectl kustomize k8s/overlays/daedalus > /tmp/qwen36-daedalus.yaml
  kubectl kustomize k8s/benchmarks/aiperf > /tmp/qwen36-aiperf.yaml
  kubectl kustomize k8s/benchmarks/dynosim-aiperf > /tmp/qwen36-dynosim-aiperf.yaml
else
  warn_or_fail "kubectl not found; skipping Kubernetes manifest validation"
fi

if [[ "${SERVER_DRY_RUN}" == "1" && -f /tmp/qwen36-base.yaml ]]; then
  echo "==> Validating Kubernetes manifests with server-side dry-run"
  kubectl apply --dry-run=server -n "${NAMESPACE}" -k k8s/base > /dev/null
  kubectl apply --dry-run=server -n "${NAMESPACE}" -k k8s/overlays/daedalus > /dev/null
  kubectl apply --dry-run=server -n "${NAMESPACE}" -k k8s/benchmarks/aiperf > /dev/null
  kubectl apply --dry-run=server -n "${NAMESPACE}" -k k8s/benchmarks/dynosim-aiperf > /dev/null
else
  echo "==> Skipping server-side dry-run; set SERVER_DRY_RUN=1 to enable it"
fi

if command -v cargo > /dev/null 2>&1; then
  echo "==> Running shared Routing Workbench Rust tests"
  if native_linker_available; then
    cargo test --manifest-path ../routing-workbench/Cargo.toml
  fi
else
  warn_or_fail "cargo not found; skipping Routing Workbench Rust tests"
fi

echo "==> Validation complete"
