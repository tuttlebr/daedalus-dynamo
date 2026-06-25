#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-${NEMOTRONNANO_BASE_URL:-http://localhost:8000}}"
ENDPOINT="${LLM_ENDPOINT:-${NEMOTRONNANO_ENDPOINT:-${BASE_URL%/}/v1/chat/completions}}"
MESSAGES_ENDPOINT="${MESSAGES_ENDPOINT:-${NEMOTRONNANO_MESSAGES_ENDPOINT:-${BASE_URL%/}/v1/messages}}"
MODEL_NAME="${MODEL_NAME:-nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4}"
NAMESPACE="${NAMESPACE:-nemotron-3-nano}"
DYNAMO_SERVICE="${DYNAMO_SERVICE:-nemotron-3-nano-vllm-agg-frontend}"

pretty_json() {
  if command -v jq > /dev/null 2>&1; then
    jq
  else
    cat
  fi
}

post_json() {
  local url="$1"
  local body="$2"
  shift 2

  curl -sS "${url}" \
    -H "Content-Type: application/json" \
    "$@" \
    -d "${body}" | pretty_json
}

echo "Using chat endpoint: ${ENDPOINT}"
echo "Using messages endpoint: ${MESSAGES_ENDPOINT}"
echo "If you have not exposed Dynamo directly, run:"
echo "  kubectl port-forward -n ${NAMESPACE} svc/${DYNAMO_SERVICE} 8000:8000"
echo

echo "Text response with agentic hints:"
post_json "${ENDPOINT}" "{
  \"model\": \"${MODEL_NAME}\",
  \"messages\": [{\"role\": \"user\", \"content\": \"Hello!\"}],
  \"max_tokens\": 128,
  \"nvext\": {
    \"agent_hints\": {
      \"priority\": 5,
      \"osl\": 128,
      \"speculative_prefill\": true
    },
    \"cache_control\": {
      \"type\": \"ephemeral\",
      \"ttl\": \"1h\"
    },
    \"extra_fields\": [\"worker_id\", \"timing\"]
  }
}"

echo
echo "Anthropic-compatible messages response with agentic hints:"
post_json "${MESSAGES_ENDPOINT}" "{
  \"model\": \"${MODEL_NAME}\",
  \"messages\": [{\"role\": \"user\", \"content\": \"Hello!\"}],
  \"max_tokens\": 128,
  \"nvext\": {
    \"agent_hints\": {
      \"priority\": 5,
      \"osl\": 128,
      \"speculative_prefill\": true
    },
    \"cache_control\": {
      \"type\": \"ephemeral\",
      \"ttl\": \"1h\"
    },
    \"extra_fields\": [\"worker_id\", \"timing\"]
  }
}" -H "anthropic-version: 2023-06-01"
