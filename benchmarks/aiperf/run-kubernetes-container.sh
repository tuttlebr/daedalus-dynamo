#!/usr/bin/env bash
set -euo pipefail

# kubectl cp needs a running container. Publish the benchmark exit code, then
# hold the container open until the host confirms that its evidence is local.
run_dir="${ARTIFACTS_DIR:-/artifacts}/${RUN_ID:?RUN_ID must be set}"
exit_code=0
"${AIPERF_BENCHMARK_SCRIPT:-/opt/aiperf/run-model-benchmarks.sh}" || exit_code=$?
# The benchmark owns creation of its run directory and rejects existing runs.
# Early failures can happen before it creates the directory; still publish
# their exit code so the host can collect any available evidence.
mkdir -p "${run_dir}"
printf '%s\n' "${exit_code}" >"${run_dir}/benchmark-exit-code.tmp"
mv "${run_dir}/benchmark-exit-code.tmp" "${run_dir}/benchmark-exit-code"
printf 'Benchmark exited with %s; waiting for artifact collection.\n' "${exit_code}"
trap 'exit 143' TERM
trap 'exit 130' INT
while [[ ! -f "${run_dir}/artifacts-collected" ]]; do
  sleep 1
done
exit "${exit_code}"
