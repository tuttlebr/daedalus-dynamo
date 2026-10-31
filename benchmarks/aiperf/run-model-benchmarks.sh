#!/usr/bin/env bash
set -Eeuo pipefail

readonly EXPECTED_AIPERF_VERSION="0.13.0"
readonly AIPERF_SOURCE_COMMIT="794f8bb75f8582f22e412d7e650fc71ca2a3d21a"
readonly MOONCAKE_BLOCK_SIZE="512"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
AIPERF_BIN="${AIPERF_BIN:-aiperf}"
AGENTIC_CONFIG="${AGENTIC_CONFIG:-${SCRIPT_DIR}/agentic-code-config.json}"
ENDPOINT_URL="${ENDPOINT_URL:-http://daedalus.ddns.me:32360}"
GPU_TELEMETRY="${GPU_TELEMETRY-192.168.1.235:32400/metrics}"
ARTIFACTS_DIR="${ARTIFACTS_DIR:-/artifacts}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
MODELS="${MODELS:-}"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
TURNS="${TURNS:-5}"
DATASET_SESSIONS="${DATASET_SESSIONS:-128}"
BENCHMARK_DURATION_SECONDS="${BENCHMARK_DURATION_SECONDS:-1800}"
CONCURRENCY="${CONCURRENCY:-3}"
export AIPERF_METRICS_USAGE_PCT_DIFF_THRESHOLD="${AIPERF_METRICS_USAGE_PCT_DIFF_THRESHOLD:-10}"
REQUEST_TIMEOUT_SECONDS="${REQUEST_TIMEOUT_SECONDS:-300}"
MODEL_TIMEOUT_SECONDS="${MODEL_TIMEOUT_SECONDS:-7200}"
SYNTHESIS_TIMEOUT_SECONDS="${SYNTHESIS_TIMEOUT_SECONDS:-120}"
DISCOVERY_TIMEOUT_SECONDS="${DISCOVERY_TIMEOUT_SECONDS:-10}"
RANDOM_SEED="${RANDOM_SEED:-42}"
TOKENIZER="${TOKENIZER:-auto}"
EXTRA_INPUTS_JSON="${EXTRA_INPUTS_JSON:-{\"temperature\":0}}"
USE_SERVER_TOKEN_COUNT="${USE_SERVER_TOKEN_COUNT:-1}"
USE_LEGACY_MAX_TOKENS="${USE_LEGACY_MAX_TOKENS:-1}"
STOP_ON_ERROR="${STOP_ON_ERROR:-0}"
CLIENT_VANTAGE="${CLIENT_VANTAGE:-AIPerf container}"

dry_run=0
list_models=0

usage() {
  cat <<'EOF'
Run a bounded, multi-turn NVIDIA AIPerf profile against every model exposed by
an OpenAI-compatible inference gateway.

Usage:
  run-model-benchmarks.sh [--dry-run] [--list-models] [--help]

The default endpoint is http://daedalus.ddns.me:32360. The script discovers model
IDs from /v1/models unless MODELS is a comma-separated override.
Use make benchmark-agentic for a pod pinned to the gateway ClusterIP, or the
Compose runner for local access to HTTP NodePort 32360.

Important environment variables:
  ENDPOINT_URL              Gateway base URL (default: http://daedalus.ddns.me:32360)
  GPU_TELEMETRY             DCGM endpoint (default: 192.168.1.235:32400/metrics);
                            set empty to disable GPU telemetry collection
  OPENAI_API_KEY            Bearer token required by the default gateway
  MODELS                    Optional comma-separated model IDs
  ARTIFACTS_DIR             Artifact root (default: /artifacts)
  TURNS                     Turns in each generated session (default: 5)
  DATASET_SESSIONS          Fresh agent sessions to generate (default: 128)
  BENCHMARK_DURATION_SECONDS Session-start window per model (default: 1800);
                            final session starts at this boundary and drains
  MODEL_TIMEOUT_SECONDS     Per-model safety ceiling (default: 7200)
  CONCURRENCY               In-flight request cap (default: 3)
  TOKENIZER                 AIPerf tokenizer; auto uses each model ID (default: auto)
  USE_SERVER_TOKEN_COUNT    Set to 1 only after validating gateway usage fields
  STOP_ON_ERROR             Set to 1 to stop after the first failed model

The generated workload has a shared system prefix, group-shared repository
context, per-session context, and growing request/response turns (five by
default). The default token shape targets about 70% reusable prompt tokens.
AIPerf adds each live assistant response to later requests in that session.
EOF
}

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "${value}"
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

require_positive_number() {
  local name="$1"
  local value="$2"
  jq -en --arg value "${value}" '($value | tonumber) > 0' >/dev/null 2>&1 ||
    fail "${name} must be a positive number (got ${value})"
}

require_boolean() {
  local name="$1"
  local value="$2"
  [[ "${value}" == "0" || "${value}" == "1" ]] ||
    fail "${name} must be 0 or 1 (got ${value})"
}

print_redacted_command() {
  local redact_next=0
  local arg
  local display

  printf '  '
  for arg in "$@"; do
    display="${arg}"
    if (( redact_next == 1 )); then
      display='[REDACTED]'
      redact_next=0
    elif [[ "${arg}" == "--api-key" ]]; then
      redact_next=1
    fi
    printf '%q ' "${display}"
  done
  printf '\n'
}

model_slug() {
  local model="$1"
  local safe
  local digest

  safe="$(printf '%s' "${model}" | tr -cs 'A-Za-z0-9._-' '_' | sed 's/^_*//; s/_*$//')"
  [[ -n "${safe}" ]] || safe="model"
  safe="${safe:0:80}"
  digest="$(printf '%s' "${model}" | sha256sum | cut -c1-12)"
  printf '%s-%s' "${safe}" "${digest}"
}

for arg in "$@"; do
  case "${arg}" in
    --dry-run)
      dry_run=1
      ;;
    --list-models)
      list_models=1
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

require_command jq
require_command sha256sum
require_positive_integer TURNS "${TURNS}"
require_positive_integer DATASET_SESSIONS "${DATASET_SESSIONS}"
[[ "${BENCHMARK_DURATION_SECONDS}" =~ ^[0-9]+$ ]] ||
  fail "BENCHMARK_DURATION_SECONDS must be a nonnegative integer"
require_positive_integer CONCURRENCY "${CONCURRENCY}"
require_positive_integer MODEL_TIMEOUT_SECONDS "${MODEL_TIMEOUT_SECONDS}"
(( MODEL_TIMEOUT_SECONDS > BENCHMARK_DURATION_SECONDS )) ||
  fail "MODEL_TIMEOUT_SECONDS must exceed BENCHMARK_DURATION_SECONDS"
(( BENCHMARK_DURATION_SECONDS == 0 || DATASET_SESSIONS >= 2 )) ||
  fail "timed benchmarks need at least two sessions; set BENCHMARK_DURATION_SECONDS=0 for a single-session smoke test"
require_positive_integer SYNTHESIS_TIMEOUT_SECONDS "${SYNTHESIS_TIMEOUT_SECONDS}"
require_positive_integer RANDOM_SEED "${RANDOM_SEED}"
require_positive_number REQUEST_TIMEOUT_SECONDS "${REQUEST_TIMEOUT_SECONDS}"
require_positive_number DISCOVERY_TIMEOUT_SECONDS "${DISCOVERY_TIMEOUT_SECONDS}"
require_positive_number AIPERF_METRICS_USAGE_PCT_DIFF_THRESHOLD "${AIPERF_METRICS_USAGE_PCT_DIFF_THRESHOLD}"
require_boolean USE_SERVER_TOKEN_COUNT "${USE_SERVER_TOKEN_COUNT}"
require_boolean USE_LEGACY_MAX_TOKENS "${USE_LEGACY_MAX_TOKENS}"
require_boolean STOP_ON_ERROR "${STOP_ON_ERROR}"

[[ "${RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]] ||
  fail "RUN_ID may contain only letters, numbers, dot, underscore, and hyphen"
[[ -n "${ARTIFACTS_DIR}" ]] || fail "ARTIFACTS_DIR must not be empty"
[[ "${ENDPOINT_URL}" =~ ^https?://[^[:space:]]+$ ]] ||
  fail "ENDPOINT_URL must be an http(s) URL without whitespace"
[[ -f "${AGENTIC_CONFIG}" ]] || fail "agentic config not found: ${AGENTIC_CONFIG}"

EXTRA_INPUTS_JSON="$(jq -ce 'if type == "object" then . else error("expected an object") end' <<<"${EXTRA_INPUTS_JSON}")" ||
  fail "EXTRA_INPUTS_JSON must be a JSON object"
DATASET_BLOCK_SIZE="$(
  jq -er '.block_size | select(type == "number" and . > 0 and floor == .)' "${AGENTIC_CONFIG}"
)" || fail "agentic config block_size must be a positive integer"
[[ "${DATASET_BLOCK_SIZE}" == "${MOONCAKE_BLOCK_SIZE}" ]] ||
  fail "AIPerf ${EXPECTED_AIPERF_VERSION} requires block_size ${MOONCAKE_BLOCK_SIZE} for Mooncake file traces (got ${DATASET_BLOCK_SIZE})"

endpoint_base="${ENDPOINT_URL%/}"
if [[ "${endpoint_base}" == */v1 ]]; then
  models_url="${endpoint_base}/models"
  aiperf_url="${endpoint_base%/v1}"
else
  models_url="${endpoint_base}/v1/models"
  aiperf_url="${endpoint_base}"
fi

declare -a models=()
if [[ -n "${MODELS}" ]]; then
  IFS=',' read -r -a requested_models <<<"${MODELS}"
  for model in "${requested_models[@]}"; do
    model="$(trim "${model}")"
    [[ -n "${model}" ]] || fail "MODELS contains an empty model ID"
    models+=("${model}")
  done
else
  require_command curl
  declare -a curl_args=(
    --fail
    --silent
    --show-error
    --max-time "${DISCOVERY_TIMEOUT_SECONDS}"
    --header 'Accept: application/json'
  )
  if [[ -n "${OPENAI_API_KEY}" ]]; then
    curl_args+=(--header "Authorization: Bearer ${OPENAI_API_KEY}")
  fi
  if ! discovery_response="$(curl "${curl_args[@]}" --write-out $'\n%{http_code}' "${models_url}")"; then
    discovery_status="${discovery_response##*$'\n'}"
    if [[ "${discovery_status}" == "401" || "${discovery_status}" == "403" ]]; then
      fail "model discovery failed at ${models_url}: gateway authentication rejected (HTTP ${discovery_status}). Use make benchmark-agentic for the Kubernetes gateway credential, or supply a valid OPENAI_API_KEY."
    fi
    fail "model discovery failed at ${models_url}"
  fi
  catalog="${discovery_response%$'\n'*}"
  jq -e '
    (.data | type) == "array" and
    (.data | length) > 0 and
    all(.data[]; (.id | type) == "string" and (.id | length) > 0)
  ' <<<"${catalog}" >/dev/null ||
    fail "${models_url} returned an invalid or empty model catalog"
  mapfile -t models < <(jq -r '.data[].id' <<<"${catalog}")
fi

declare -a unique_models=()
for model in "${models[@]}"; do
  duplicate=0
  [[ "${model}" != *$'\n'* && "${model}" != *$'\r'* ]] ||
    fail "model IDs must not contain control characters"
  for existing_model in "${unique_models[@]}"; do
    if [[ "${model}" == "${existing_model}" ]]; then
      duplicate=1
      break
    fi
  done
  if (( duplicate == 0 )); then
    unique_models+=("${model}")
  fi
done
models=("${unique_models[@]}")
(( ${#models[@]} > 0 )) || fail "no models selected"

if (( list_models == 1 )); then
  printf '%s\n' "${models[@]}"
  exit 0
fi

if (( dry_run == 0 )); then
  require_command python3
  require_command "${AIPERF_BIN}"
  require_command timeout
  actual_version="$("${AIPERF_BIN}" --version 2>&1 | tail -n 1)"
  [[ "${actual_version}" == "${EXPECTED_AIPERF_VERSION}" ]] ||
    fail "expected AIPerf ${EXPECTED_AIPERF_VERSION}, found ${actual_version}"
fi

run_dir="${ARTIFACTS_DIR%/}/${RUN_ID}"
dataset_parent="${run_dir}/dataset"
dataset_file="${run_dir}/scheduled-dataset.jsonl"
effective_agentic_config="${run_dir}/effective-agentic-code-config.json"
workload_report="${run_dir}/workload-plan.json"

if (( dry_run == 1 )); then
  log "DRY RUN: no dataset will be generated and no inference requests will be sent."
else
  [[ ! -e "${run_dir}" ]] || fail "run directory already exists: ${run_dir}"
  mkdir -p "${ARTIFACTS_DIR}"
  mkdir "${run_dir}"
  mkdir "${dataset_parent}"

  jq --argjson turns "${TURNS}" '
    .turns.mean = $turns |
    .turns.median = $turns |
    .turns.min = $turns |
    .turns.max = $turns
  ' "${AGENTIC_CONFIG}" >"${effective_agentic_config}"

  log "Generating one frozen ${TURNS}-turn agentic dataset for all models..."
  timeout --signal=INT --kill-after=30s "${SYNTHESIS_TIMEOUT_SECONDS}" \
    "${AIPERF_BIN}" synthesize agentic-code \
      --config "${effective_agentic_config}" \
      --num-sessions "${DATASET_SESSIONS}" \
      --seed "${RANDOM_SEED}" \
      --output "${dataset_parent}"

  mapfile -t dataset_files < <(find "${dataset_parent}" -type f -name dataset.jsonl -print)
  (( ${#dataset_files[@]} == 1 )) ||
    fail "expected one generated dataset.jsonl, found ${#dataset_files[@]}"
  dataset_file="${dataset_files[0]}"
  python3 "${SCRIPT_DIR}/prepare-agentic-trace.py" \
    --input "${dataset_file}" \
    --output "${run_dir}/scheduled-dataset.jsonl" \
    --report "${workload_report}" \
    --duration-seconds "${BENCHMARK_DURATION_SECONDS}"
  dataset_file="${run_dir}/scheduled-dataset.jsonl"

  dataset_sha256="$(sha256sum "${dataset_file}" | cut -d' ' -f1)"
  config_sha256="$(sha256sum "${effective_agentic_config}" | cut -d' ' -f1)"
  models_json="$(printf '%s\n' "${models[@]}" | jq -Rsc 'split("\n")[:-1]')"
  jq -n \
    --arg run_id "${RUN_ID}" \
    --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg endpoint_url "${aiperf_url}" \
    --arg models_url "${models_url}" \
    --arg aiperf_version "${EXPECTED_AIPERF_VERSION}" \
    --arg aiperf_source_commit "${AIPERF_SOURCE_COMMIT}" \
    --arg dataset_file "${dataset_file#"${run_dir}/"}" \
    --arg dataset_sha256 "${dataset_sha256}" \
    --arg config_sha256 "${config_sha256}" \
    --arg tokenizer "${TOKENIZER}" \
    --arg client_vantage "${CLIENT_VANTAGE}" \
    --arg gpu_telemetry "${GPU_TELEMETRY}" \
    --argjson models "${models_json}" \
    --argjson dataset_sessions "${DATASET_SESSIONS}" \
    --argjson turns "${TURNS}" \
    --argjson dataset_block_size "${DATASET_BLOCK_SIZE}" \
    --argjson concurrency "${CONCURRENCY}" \
    --argjson benchmark_duration_seconds "${BENCHMARK_DURATION_SECONDS}" \
    --slurpfile workload_plan "${workload_report}" \
    --argjson random_seed "${RANDOM_SEED}" \
    --argjson use_server_token_count "${USE_SERVER_TOKEN_COUNT}" \
    '{
      schema_version: 1,
      run_id: $run_id,
      created_at: $created_at,
      estimand: "Client-observed prefix-reuse sensitivity for a frozen multi-turn agentic workload through the inference gateway.",
      claim_ceiling: "Results do not prove KV-cache hits without provider or server telemetry.",
      endpoint: {
        base_url: $endpoint_url,
        discovery_url: $models_url,
        client_vantage: $client_vantage
      },
      aiperf: {
        version: $aiperf_version,
        source_commit: $aiperf_source_commit,
        export_level: "records"
      },
      models: $models,
      dataset: {
        type: "agentic-code mooncake trace",
        file: $dataset_file,
        sha256: $dataset_sha256,
        config_sha256: $config_sha256,
        prompt_hash_block_size: $dataset_block_size,
        random_seed: $random_seed,
        turns_per_session: $turns,
        live_assistant_responses_accumulate: true
      },
      workload_plan: $workload_plan[0],
      load: {
        mode: "fixed-schedule agentic trace",
        dataset_sessions: $dataset_sessions,
        expected_requests_per_model: ($dataset_sessions * $turns),
        session_start_span_seconds: $benchmark_duration_seconds,
        concurrency_cap: $concurrency,
        session_header: "X-Dynamo-Session-ID"
      },
      token_count: {
        tokenizer: $tokenizer,
        source: (if $use_server_token_count == 1 then "server usage fields" else "AIPerf client tokenizer" end)
      },
      observability: {
        model_revision: "unknown",
        gpu_telemetry: (if $gpu_telemetry == "" then null else $gpu_telemetry end),
        hardware: "unknown",
        replicas: "unknown",
        autoscaling_state: "unknown",
        batching_policy: "unknown",
        cache_hits: "unknown",
        provider_load: "unknown"
      }
    }' >"${run_dir}/run-manifest.json"
fi

log "Endpoint: ${aiperf_url}"
log "GPU telemetry: ${GPU_TELEMETRY:-disabled}"
log "Models (${#models[@]}):"
printf '  %s\n' "${models[@]}"
log "Workload: ${DATASET_SESSIONS} sessions, ${TURNS} turns/session, $((DATASET_SESSIONS * TURNS)) requests/model"
log "Session starts span ${BENCHMARK_DURATION_SECONDS}s per model; final session and queued work drain afterward."
log "Interpretation: prefix-reuse sensitivity, not confirmed KV-cache hits."

results_file="${run_dir}/model-results.jsonl"
failed_count=0
completed_count=0

for model in "${models[@]}"; do
  slug="$(model_slug "${model}")"
  artifact_dir="${run_dir}/models/${slug}"
  resolved_tokenizer="${TOKENIZER}"
  [[ "${resolved_tokenizer}" != "auto" ]] || resolved_tokenizer="${model}"

  declare -a profile_cmd=(
    "${AIPERF_BIN}" profile
    --model "${model}"
    --url "${aiperf_url}"
    --endpoint-type chat
    --streaming
    --input-file "${dataset_file}"
    --custom-dataset-type mooncake-trace
    --fixed-schedule
    --fixed-schedule-auto-offset
    --concurrency "${CONCURRENCY}"
    --tokenizer "${resolved_tokenizer}"
    --random-seed "${RANDOM_SEED}"
    --session-header X-Dynamo-Session-ID
    --connection-reuse-strategy sticky-user-sessions
    --extra-inputs "${EXTRA_INPUTS_JSON}"
    --request-timeout-seconds "${REQUEST_TIMEOUT_SECONDS}"
    --artifact-dir "${artifact_dir}"
    --export-level records
    --ui none
    --no-auto-plot
  )
  if [[ -n "${GPU_TELEMETRY}" ]]; then
    profile_cmd+=(--gpu-telemetry "${GPU_TELEMETRY}")
  else
    profile_cmd+=(--no-gpu-telemetry)
  fi
  if (( USE_LEGACY_MAX_TOKENS == 1 )); then
    profile_cmd+=(--use-legacy-max-tokens)
  fi
  if (( USE_SERVER_TOKEN_COUNT == 1 )); then
    profile_cmd+=(--use-server-token-count)
  fi
  if [[ -n "${OPENAI_API_KEY}" ]]; then
    profile_cmd+=(--api-key "${OPENAI_API_KEY}")
  fi

  declare -a run_cmd=(
    timeout --signal=INT --kill-after=60s "${MODEL_TIMEOUT_SECONDS}"
    "${profile_cmd[@]}"
  )

  log ""
  log "=== ${model} ==="
  if (( dry_run == 1 )); then
    print_redacted_command "${run_cmd[@]}"
    continue
  fi

  started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  set +e
  "${run_cmd[@]}"
  exit_code=$?
  set -e
  completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  profile_summary="${artifact_dir}/profile_export_aiperf.json"
  if (( exit_code == 0 && BENCHMARK_DURATION_SECONDS > 0 )); then
    if ! jq -e --argjson minimum "${BENCHMARK_DURATION_SECONDS}" \
      '.benchmark_duration.unit == "sec" and .benchmark_duration.avg >= $minimum' \
      "${profile_summary}" >/dev/null 2>&1; then
      log "FAIL: measured benchmark duration did not reach ${BENCHMARK_DURATION_SECONDS}s; see ${profile_summary}"
      exit_code=1
    fi
  fi
  output_length_evidence_json='{"status":"unavailable"}'
  if [[ -f "${profile_summary}" ]]; then
    output_length_evidence_json="$(
      jq -ce \
        --arg source "${profile_summary#"${run_dir}/"}" \
        '.request_count.avg as $requests |
        .osl_mismatch_count.avg as $mismatches |
        .osl_mismatch_diff_pct.avg as $average_diff |
        if (($requests | type) == "number") and
           (($mismatches | type) == "number") and
           (($average_diff | type) == "number") then
          {
            status: "observed",
            request_count: $requests,
            threshold_exceeding_requests: $mismatches,
            threshold_exceeding_request_pct:
              (if $requests > 0 then (($mismatches / $requests) * 100) else null end),
            average_diff_pct: $average_diff,
            source: $source
          }
        else
          {status: "unavailable", source: $source}
        end' "${profile_summary}"
    )"
  fi

  if (( exit_code == 0 )); then
    status="completed"
    completed_count=$((completed_count + 1))
    log "PASS: ${model}"
  else
    status="failed"
    failed_count=$((failed_count + 1))
    log "FAIL: ${model} exited with status ${exit_code}"
  fi

  jq -cn \
    --arg model "${model}" \
    --arg status "${status}" \
    --arg started_at "${started_at}" \
    --arg completed_at "${completed_at}" \
    --arg artifact_dir "${artifact_dir#"${run_dir}/"}" \
    --argjson exit_code "${exit_code}" \
    --argjson output_length_evidence "${output_length_evidence_json}" \
    '{
      model: $model,
      status: $status,
      exit_code: $exit_code,
      started_at: $started_at,
      completed_at: $completed_at,
      artifact_dir: $artifact_dir,
      output_length_evidence: $output_length_evidence
    }' >>"${results_file}"

  if (( exit_code != 0 && STOP_ON_ERROR == 1 )); then
    break
  fi
done

if (( dry_run == 1 )); then
  exit 0
fi

jq -s \
  --arg run_id "${RUN_ID}" \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson completed_models "${completed_count}" \
  --argjson failed_models "${failed_count}" \
  '{
    schema_version: 1,
    run_id: $run_id,
    completed_at: $completed_at,
    completed_models: $completed_models,
    failed_models: $failed_models,
    results: .
  }' "${results_file}" >"${run_dir}/run-summary.json"

log ""
log "Artifacts: ${run_dir}"
log "Completed models: ${completed_count}; failed models: ${failed_count}"
(( failed_count == 0 )) || exit 1
