#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"

KUBECTL="${KUBECTL:-kubectl}"
PYTHON="${PYTHON:-python3}"
EXPECTED_CONTEXT="${EXPECTED_CONTEXT:-}"
NAMESPACE="${NAMESPACE:-daedalus-dynamo}"
GATEWAY_NAME="${GATEWAY_NAME:-inference-gateway}"
GATEWAY_PORT="${GATEWAY_PORT:-32359}"
AIPERF_IMAGE="${AIPERF_IMAGE:-}"
default_endpoint_url="https://daedalus.ddns.me:${GATEWAY_PORT}"
AIPERF_ENDPOINT_URL="${AIPERF_ENDPOINT_URL:-${default_endpoint_url}}"
AIPERF_RUN_ID="${AIPERF_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
AIPERF_RESULTS_DIR="${AIPERF_RESULTS_DIR:-artifacts/aiperf}"
AIPERF_ACTIVE_DEADLINE_SECONDS="${AIPERF_ACTIVE_DEADLINE_SECONDS:-14400}"
AIPERF_POLL_SECONDS="${AIPERF_POLL_SECONDS:-5}"
AIPERF_KEEP_JOB="${AIPERF_KEEP_JOB:-0}"

mode="run"

usage() {
  cat <<'EOF'
Create one bounded AIPerf Job and copy its completed or failed run evidence
back into this repository.

Usage:
  run-kubernetes-job.sh [--preflight] [--help]

--preflight checks the Kubernetes context, namespace, and gateway Service
without creating a Job or sending inference traffic.
EOF
}

log() {
  printf '%s\n' "$*"
}

warn() {
  printf 'WARN: %s\n' "$*" >&2
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

require_positive_integer() {
  local name="$1"
  local value="$2"
  [[ "${value}" =~ ^[0-9]+$ ]] && (( value > 0 )) ||
    fail "${name} must be a positive integer (got ${value})"
}

require_boolean() {
  local name="$1"
  local value="$2"
  [[ "${value}" == "0" || "${value}" == "1" ]] ||
    fail "${name} must be 0 or 1 (got ${value})"
}

for arg in "$@"; do
  case "${arg}" in
    --preflight)
      mode="preflight"
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: ${arg}"
      ;;
  esac
done

require_command "${KUBECTL}"
require_command "${PYTHON}"
require_positive_integer GATEWAY_PORT "${GATEWAY_PORT}"
require_positive_integer AIPERF_ACTIVE_DEADLINE_SECONDS "${AIPERF_ACTIVE_DEADLINE_SECONDS}"
require_positive_integer AIPERF_POLL_SECONDS "${AIPERF_POLL_SECONDS}"
require_boolean AIPERF_KEEP_JOB "${AIPERF_KEEP_JOB}"
[[ -n "${EXPECTED_CONTEXT}" ]] || fail "EXPECTED_CONTEXT must not be empty"

current_context="$("${KUBECTL}" config current-context)" ||
  fail "could not read the current Kubernetes context"
[[ "${current_context}" == "${EXPECTED_CONTEXT}" ]] ||
  fail "current context is ${current_context}; expected ${EXPECTED_CONTEXT}"

"${KUBECTL}" get namespace "${NAMESPACE}" >/dev/null ||
  fail "namespace does not exist: ${NAMESPACE}"
"${KUBECTL}" get service "${GATEWAY_NAME}" -n "${NAMESPACE}" >/dev/null ||
  fail "gateway Service does not exist: ${NAMESPACE}/${GATEWAY_NAME}"
gateway_service_port="$("${KUBECTL}" get service "${GATEWAY_NAME}" \
  -n "${NAMESPACE}" \
  -o "jsonpath={.spec.ports[?(@.port==${GATEWAY_PORT})].port}")"
[[ "${gateway_service_port}" == "${GATEWAY_PORT}" ]] ||
  fail "gateway Service ${NAMESPACE}/${GATEWAY_NAME} does not expose port ${GATEWAY_PORT}"

log "==> Kubernetes preflight passed for ${EXPECTED_CONTEXT}"
log "==> Gateway Service: ${NAMESPACE}/${GATEWAY_NAME}:${GATEWAY_PORT}"
if [[ "${mode}" == "preflight" ]]; then
  exit 0
fi

require_command sha256sum
[[ -n "${AIPERF_IMAGE}" ]] || fail "AIPERF_IMAGE must not be empty"
[[ "${AIPERF_RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]] ||
  fail "AIPERF_RUN_ID may contain only letters, numbers, dots, underscores, and hyphens"
[[ "${AIPERF_RUN_ID}" != "." && "${AIPERF_RUN_ID}" != ".." ]] ||
  fail "AIPERF_RUN_ID must not be . or .."
(( ${#AIPERF_RUN_ID} <= 128 )) || fail "AIPERF_RUN_ID must be at most 128 characters"

results_dir="$(${PYTHON} - "${REPO_ROOT}" "${AIPERF_RESULTS_DIR}" <<'PY'
from pathlib import Path
import sys

repo_root = Path(sys.argv[1]).resolve()
configured = Path(sys.argv[2])
resolved = (repo_root / configured if not configured.is_absolute() else configured).resolve()
try:
    resolved.relative_to(repo_root)
except ValueError:
    raise SystemExit("AIPERF_RESULTS_DIR must resolve inside the repository")
print(resolved)
PY
)" || fail "AIPERF_RESULTS_DIR must resolve inside the repository"

run_dir="${results_dir}/${AIPERF_RUN_ID}"
[[ ! -e "${run_dir}" && ! -L "${run_dir}" ]] ||
  fail "local run directory already exists: ${run_dir}"
mkdir -p "${run_dir}/kubernetes"

export KUBECTL EXPECTED_CONTEXT NAMESPACE GATEWAY_NAME GATEWAY_PORT
export AIPERF_IMAGE AIPERF_ENDPOINT_URL AIPERF_RUN_ID
export AIPERF_ACTIVE_DEADLINE_SECONDS
submitted_job="${run_dir}/kubernetes/submitted-job.json"
"${PYTHON}" "${SCRIPT_DIR}/render-kubernetes-job.py" >"${submitted_job}"
"${PYTHON}" - "${submitted_job}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    pod = json.load(source)["spec"]["template"]["spec"]
for alias in pod.get("hostAliases", []):
    print(f"==> Local endpoint route: {', '.join(alias['hostnames'])} -> {alias['ip']}")
PY
job_name="$(${PYTHON} - "${submitted_job}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    print(json.load(source)["metadata"]["name"])
PY
)"

source_commit="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || printf 'unknown')"
if [[ -n "$(git -C "${REPO_ROOT}" status --porcelain 2>/dev/null || true)" ]]; then
  source_dirty="true"
else
  source_dirty="false"
fi

log "==> Creating Job ${NAMESPACE}/${job_name}"
if ! "${KUBECTL}" create -f "${submitted_job}"; then
  fail "Job creation failed; submitted manifest remains at ${submitted_job}"
fi

job_result="unknown"
stop_reason="unknown"
wait_started="${SECONDS}"
wait_limit=$((AIPERF_ACTIVE_DEADLINE_SECONDS + 180))
pod_name=""
benchmark_exit_code=""
while true; do
  pod_name="$("${KUBECTL}" get pods -n "${NAMESPACE}" \
    -l "job-name=${job_name}" \
    -o 'jsonpath={.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -n "${pod_name}" ]]; then
    benchmark_exit_code="$("${KUBECTL}" exec "${pod_name}" -n "${NAMESPACE}" \
      -c aiperf -- cat "/artifacts/${AIPERF_RUN_ID}/benchmark-exit-code" \
      2>/dev/null || true)"
    if [[ "${benchmark_exit_code}" =~ ^[0-9]+$ ]]; then
      if [[ "${benchmark_exit_code}" == "0" ]]; then
        job_result="completed"
      else
        job_result="failed"
      fi
      stop_reason="benchmark_exit"
      break
    fi
  fi
  complete_condition="$("${KUBECTL}" get job "${job_name}" -n "${NAMESPACE}" \
    -o 'jsonpath={.status.conditions[?(@.type=="Complete")].status}' 2>/dev/null || true)"
  failed_condition="$("${KUBECTL}" get job "${job_name}" -n "${NAMESPACE}" \
    -o 'jsonpath={.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null || true)"
  if [[ "${complete_condition}" == "True" ]]; then
    job_result="completed"
    stop_reason="job_complete"
    break
  fi
  if [[ "${failed_condition}" == "True" ]]; then
    job_result="failed"
    stop_reason="job_failed"
    break
  fi
  if (( SECONDS - wait_started >= wait_limit )); then
    job_result="incomplete"
    stop_reason="local_wait_timeout"
    break
  fi
  sleep "${AIPERF_POLL_SECONDS}"
done

for (( attempt = 1; attempt <= 30; attempt++ )); do
  pod_name="$("${KUBECTL}" get pods -n "${NAMESPACE}" \
    -l "job-name=${job_name}" \
    -o 'jsonpath={.items[0].metadata.name}' 2>/dev/null || true)"
  [[ -z "${pod_name}" ]] || break
  sleep 1
done

"${KUBECTL}" get job "${job_name}" -n "${NAMESPACE}" -o json \
  >"${run_dir}/kubernetes/job.json" 2>"${run_dir}/kubernetes/job-get-error.log" || true

artifacts_copied="false"
if [[ -n "${pod_name}" ]]; then
  "${KUBECTL}" get pod "${pod_name}" -n "${NAMESPACE}" -o json \
    >"${run_dir}/kubernetes/pod.json" 2>"${run_dir}/kubernetes/pod-get-error.log" || true
  "${KUBECTL}" logs "${pod_name}" -n "${NAMESPACE}" --timestamps \
    >"${run_dir}/kubernetes/job.log" 2>"${run_dir}/kubernetes/logs-error.log" || true

  mkdir "${run_dir}/workload"
  if "${KUBECTL}" cp \
    -n "${NAMESPACE}" \
    -c aiperf \
    "${pod_name}:/artifacts/${AIPERF_RUN_ID}/." \
    "${run_dir}/workload" &&
    [[ -f "${run_dir}/workload/run-manifest.json" ]]; then
    artifacts_copied="true"
    (
      cd "${run_dir}/workload"
      find . -type f -print0 | sort -z | xargs -0 -r sha256sum
    ) >"${run_dir}/kubernetes/workload-sha256.txt"
  else
    warn "workload artifacts could not be copied from ${NAMESPACE}/${pod_name}"
  fi
else
  warn "no pod was created for Job ${NAMESPACE}/${job_name}"
fi

# Release the holding container only after the copy and checksums succeeded.
# Without an acknowledgement it remains available until the Job deadline.
if [[ "${artifacts_copied}" == "true" ]]; then
  "${KUBECTL}" exec "${pod_name}" -n "${NAMESPACE}" -c aiperf -- \
    touch "/artifacts/${AIPERF_RUN_ID}/artifacts-collected" ||
    warn "could not release the artifact holding container"
fi

job_deleted="false"
if [[ "${artifacts_copied}" == "true" && "${AIPERF_KEEP_JOB}" == "0" ]]; then
  if "${KUBECTL}" delete job "${job_name}" -n "${NAMESPACE}" --wait=true; then
    job_deleted="true"
  else
    warn "local evidence is complete, but Job cleanup failed: ${NAMESPACE}/${job_name}"
  fi
elif [[ "${AIPERF_KEEP_JOB}" == "1" ]]; then
  log "==> Retaining Job ${NAMESPACE}/${job_name} because AIPERF_KEEP_JOB=1"
else
  warn "retaining Job ${NAMESPACE}/${job_name} so its artifacts can be recovered"
fi

"${PYTHON}" - \
  "${run_dir}/kubernetes/orchestration-summary.json" \
  "${AIPERF_RUN_ID}" \
  "${job_name}" \
  "${NAMESPACE}" \
  "${current_context}" \
  "${AIPERF_ENDPOINT_URL}" \
  "${AIPERF_IMAGE}" \
  "${source_commit}" \
  "${source_dirty}" \
  "${job_result}" \
  "${stop_reason}" \
  "${artifacts_copied}" \
  "${job_deleted}" <<'PY'
from datetime import datetime, timezone
import json
import sys

(
    output,
    run_id,
    job_name,
    namespace,
    context,
    endpoint_url,
    image,
    source_commit,
    source_dirty,
    job_result,
    stop_reason,
    artifacts_copied,
    job_deleted,
) = sys.argv[1:]

summary = {
    "schema_version": 1,
    "run_id": run_id,
    "completed_at": datetime.now(timezone.utc).isoformat(),
    "kubernetes": {
        "context": context,
        "namespace": namespace,
        "job": job_name,
        "result": job_result,
        "stop_reason": stop_reason,
        "job_deleted": job_deleted == "true",
    },
    "endpoint_url": endpoint_url,
    "image": image,
    "repository": {
        "commit": source_commit,
        "dirty": source_dirty == "true",
    },
    "artifacts": {
        "copied": artifacts_copied == "true",
        "workload_path": "workload" if artifacts_copied == "true" else None,
        "checksums": (
            "kubernetes/workload-sha256.txt" if artifacts_copied == "true" else None
        ),
    },
}
with open(output, "w", encoding="utf-8") as destination:
    json.dump(summary, destination, indent=2, sort_keys=True)
    destination.write("\n")
PY

log "==> Local benchmark evidence: ${run_dir}"
if [[ "${artifacts_copied}" != "true" ]]; then
  fail "Job evidence was not copied; the Job was retained for recovery"
fi
if [[ "${job_result}" != "completed" ]]; then
  fail "AIPerf Job ended with ${job_result}; failed-run evidence was retained locally"
fi

log "==> Agentic evaluation completed for every model advertised by the gateway"
