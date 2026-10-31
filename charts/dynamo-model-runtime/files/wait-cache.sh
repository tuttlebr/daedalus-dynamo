#!/usr/bin/env bash
# Source this file before starting any inference backend.
: "${MODEL_NAME:?}" "${MODEL_CACHE_PATH:?}" "${MODEL_EXPRESS_HOST:?}" "${MODEL_EXPRESS_PORT:?}"
: "${MODEL_REVISION:=}" "${MX_WAIT_FOR_CACHE_READY:=true}"
for ((attempt=1; attempt<=180; attempt++)); do
  if timeout 1 bash -c '</dev/tcp/${MODEL_EXPRESS_HOST}/${MODEL_EXPRESS_PORT}' 2>/dev/null; then
    break
  fi
  if (( attempt == 180 )); then
    echo "Model Express unavailable: ${MODEL_EXPRESS_HOST}:${MODEL_EXPRESS_PORT}" >&2
    exit 1
  fi
  sleep 2
done
if [[ "$MX_WAIT_FOR_CACHE_READY" == true ]]; then
  snapshots="$MODEL_CACHE_PATH/models--${MODEL_NAME//\//--}/snapshots"
  marker="$snapshots/${MODEL_REVISION:+ready-}${MODEL_REVISION:-latest}"
  while [[ ! -L "$marker" || ! -d "$marker" ]]; do
    sleep 5
  done
  echo "Model cache ready: $marker"
fi
