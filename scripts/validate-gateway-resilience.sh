#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

tmp_dir="$(mktemp -d)"
test_image="daedalus-agentgateway-resilience-test:$$"
cleanup() {
  rm -rf "${tmp_dir}"
  docker image rm -f "${test_image}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build \
  --quiet \
  -t "${test_image}" \
  -f Dockerfile.agentgateway \
  . >/dev/null

set +e
entrypoint_output="$(
  docker run --rm \
    --ulimit nofile=1024:524288 \
    --env AGENTGATEWAY_NOFILE_LIMIT=4096 \
    "${test_image}" \
    --version \
    2>&1
)"
entrypoint_status=$?
set -e

if [[ "${entrypoint_status}" != "0" ]]; then
  echo "ERROR: hardened gateway image failed its entrypoint check with ${entrypoint_status}" >&2
  printf '%s\n' "${entrypoint_output}" >&2
  exit 1
fi
if ! grep -Eq 'RLIMIT_NOFILE soft=([4-9][0-9]{3}|[1-9][0-9]{4,})' <<<"${entrypoint_output}"; then
  echo "ERROR: entrypoint did not raise and report RLIMIT_NOFILE" >&2
  printf '%s\n' "${entrypoint_output}" >&2
  exit 1
fi

PYTHONPYCACHEPREFIX="${tmp_dir}/pycache" \
  python3 -m unittest discover -s tests -p 'test_*.py'

shopt -s nullglob
model_configs=(values/*.yaml values/*.yml)
for model_config in "${model_configs[@]}"; do
  make --no-print-directory lint "${model_config}" >/dev/null
done

echo "Gateway resilience validation passed."
