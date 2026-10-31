#!/usr/bin/env bash
# Contract v1. Configuration comes from the consuming pod, never this ConfigMap.
set -euo pipefail
: "${MODEL_NAME:?}" "${MODEL_CACHE_PATH:?}" "${MODEL_EXPRESS_URL:?}"
: "${MODEL_EXPRESS_SERVER_PORT:?}" "${MX_REDIS_HOST:?}" "${MX_REDIS_PORT:=6379}"
: "${MODEL_REVISION:=}" "${MX_RESET_REGISTRY:=true}" "${MX_WAIT_FOR_CACHE_READY:=true}"
: "${MX_DOWNLOAD_STRATEGY:=server-only}" "${MX_CLIENT_CONFIG_PATH:=/tmp/modelexpress.yaml}"

for ((attempt=1; attempt<=60; attempt++)); do
  if timeout 1 bash -c '</dev/tcp/${MX_REDIS_HOST}/${MX_REDIS_PORT}' 2>/dev/null; then
    break
  fi
  if (( attempt == 60 )); then
    echo "Redis metadata backend unavailable" >&2
    exit 1
  fi
  sleep 2
done

snapshots="$MODEL_CACHE_PATH/models--${MODEL_NAME//\//--}/snapshots"
marker="$snapshots/${MODEL_REVISION:+ready-}${MODEL_REVISION:-latest}"
if [[ "$MX_WAIT_FOR_CACHE_READY" == true && -L "$marker" ]]; then
  unlink "$marker"
fi
if [[ "$MX_RESET_REGISTRY" == true ]]; then
  key="mx:model:HuggingFace:${MODEL_NAME}"
  exec 3<>"/dev/tcp/${MX_REDIS_HOST}/${MX_REDIS_PORT}"
  printf '*2\r\n$3\r\nDEL\r\n$%s\r\n%s\r\n' "${#key}" "$key" >&3
  IFS= read -r reply <&3
  exec 3>&-
  [[ "$reply" == :* ]] || { echo "Redis registry reset failed" >&2; exit 1; }
fi

mkdir -p "$MODEL_CACHE_PATH" "$(dirname "$MX_CLIENT_CONFIG_PATH")"
printf 'local_path: %s\nserver_endpoint: %s\ntimeout_secs: null\n' \
  "$MODEL_CACHE_PATH" "$MODEL_EXPRESS_URL" > "$MX_CLIENT_CONFIG_PATH"
./modelexpress-server &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
endpoint="http://localhost:${MODEL_EXPRESS_SERVER_PORT}"
for ((attempt=1; attempt<=60; attempt++)); do
  kill -0 "$server_pid" || { echo "Model Express server exited" >&2; exit 1; }
  if ./modelexpress-cli --endpoint "$endpoint" health >/dev/null 2>&1; then
    break
  fi
  if (( attempt == 60 )); then
    echo "Model Express health deadline exceeded" >&2
    exit 1
  fi
  sleep 2
done
download=(./modelexpress-cli --endpoint "$endpoint" --cache-path "$MODEL_CACHE_PATH"
  model download "$MODEL_NAME" --strategy "$MX_DOWNLOAD_STRATEGY")
if [[ -n "$MODEL_REVISION" ]]; then
  download+=(--revision "$MODEL_REVISION")
fi
"${download[@]}"

# Pinned revisions never fall back to another cached checkpoint.
if [[ -n "$MODEL_REVISION" ]]; then
  snapshot="$snapshots/$MODEL_REVISION"
else
  ref="$MODEL_CACHE_PATH/models--${MODEL_NAME//\//--}/refs/main"
  [[ -f "$ref" ]] || { echo "Downloaded model has no refs/main; pin a revision" >&2; exit 1; }
  snapshot="$snapshots/$(cat "$ref")"
fi
[[ -d "$snapshot" ]] || { echo "Downloaded snapshot missing: $snapshot" >&2; exit 1; }
ln -sfn "$(basename "$snapshot")" "$marker"
echo "Model cache ready: $marker"
wait "$server_pid"
