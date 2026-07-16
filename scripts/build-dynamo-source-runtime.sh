#!/usr/bin/env bash
set -euo pipefail

if (( $# == 0 )); then
  echo "Usage: $0 <image> [<additional-image-tag> ...]" >&2
  exit 2
fi

docker_bin="${DOCKER:-docker}"
python_bin="${PYTHON:-python3}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
default_source_repo="https://github.com/ai-dynamo/dynamo.git"
default_source_ref="de2ad9b9de09737d553a9b8f2baf917ec7be2ef0"
default_vllm_runtime_tag="v0.25.1-ubuntu2404@sha256:acb93b1843633e41b40db68f3a38e81c7335dac6b354145c1a749a03e1fc2af7"
default_output_tag="1.3.0-src-de2ad9b-vllm0.25.1-daedalus.1"
expected_patch_bundle_sha256="e8fc5239422cce9294155645e8b72c6aff98034079dc646132dac8dcdf18beac"
source_repo="${DYNAMO_SOURCE_REPO:-$default_source_repo}"
source_ref="${DYNAMO_SOURCE_REF:-$default_source_ref}"
vllm_runtime_tag="${DYNAMO_VLLM_RUNTIME_TAG:-$default_vllm_runtime_tag}"

command -v git >/dev/null || { echo "ERROR: git not found." >&2; exit 127; }
command -v sha256sum >/dev/null || { echo "ERROR: sha256sum not found." >&2; exit 127; }
command -v "$docker_bin" >/dev/null || { echo "ERROR: $docker_bin not found." >&2; exit 127; }
command -v "$python_bin" >/dev/null || { echo "ERROR: $python_bin not found." >&2; exit 127; }

work_dir="$(mktemp -d /tmp/daedalus-dynamo-source.XXXXXX)"
source_dir="$work_dir/source"

cleanup() {
  if [[ "$work_dir" == /tmp/daedalus-dynamo-source.* ]]; then
    rm -rf -- "$work_dir"
  fi
}
trap cleanup EXIT

if [[ -n "${DYNAMO_SOURCE_DIR:-}" ]]; then
  source_checkout="${DYNAMO_SOURCE_DIR}"
  git -C "$source_checkout" rev-parse --git-dir >/dev/null 2>&1 || {
    echo "ERROR: DYNAMO_SOURCE_DIR is not a git checkout: $source_checkout" >&2
    exit 2
  }
  resolved_ref="$(git -C "$source_checkout" rev-parse --verify "${source_ref}^{commit}" 2>/dev/null)" || {
    echo "ERROR: DYNAMO_SOURCE_REF $source_ref is not available in $source_checkout." >&2
    exit 2
  }
  # Clone the committed object into an isolated tree. This deliberately ignores
  # and never modifies caller-owned working-tree changes.
  git clone --quiet --no-checkout "$source_checkout" "$source_dir"
  git -C "$source_dir" checkout --quiet --detach "$resolved_ref"
else
  mkdir -p "$source_dir"
  git -C "$source_dir" init --quiet
  git -C "$source_dir" remote add origin "$source_repo"
  git -C "$source_dir" fetch --depth 1 origin "$source_ref"
  git -C "$source_dir" checkout --quiet --detach FETCH_HEAD
fi

resolved_ref="$(git -C "$source_dir" rev-parse HEAD)"
if [[ "$source_ref" =~ ^[0-9a-f]{40}$ ]] && [[ "$resolved_ref" != "$source_ref" ]]; then
  echo "ERROR: requested Dynamo commit $source_ref but resolved $resolved_ref." >&2
  exit 3
fi

if [[ "$source_repo" != "$default_source_repo" || \
      "$source_ref" != "$default_source_ref" || \
      "$vllm_runtime_tag" != "$default_vllm_runtime_tag" ]]; then
  for image in "$@"; do
    if [[ "${image##*:}" == "$default_output_tag" ]]; then
      echo "ERROR: source/runtime overrides require a distinct IMAGE_TAG; refusing to overwrite provenance tag $default_output_tag." >&2
      exit 3
    fi
  done
fi

# Keep patches small and fail closed against the pinned source revision:
# - Fetch the same FFmpeg 8.1 tag from its official GitHub mirror because
#   ffmpeg.org is unreachable from the Daedalus build network.
# - Add only pinned packages proven absent from the upstream vLLM solve; do not
#   let a resolver upgrade the CUDA/vLLM stack.
source_patches=(
  "$script_dir/patches/ai-dynamo-ffmpeg-github-source.patch"
  "$script_dir/patches/ai-dynamo-vllm-shared-runtime-deps.patch"
)
patch_bundle_sha256="$(
  for source_patch in "${source_patches[@]}"; do
    sha256sum "$source_patch" | cut -d' ' -f1
  done | sha256sum | cut -d' ' -f1
)"
if [[ "$patch_bundle_sha256" != "$expected_patch_bundle_sha256" ]]; then
  echo "ERROR: source patch bundle changed ($patch_bundle_sha256); bump its expected digest and output tag together." >&2
  exit 3
fi
for source_patch in "${source_patches[@]}"; do
  [[ -f "$source_patch" ]] || {
    echo "ERROR: missing source-build patch: $source_patch" >&2
    exit 2
  }
  if git -C "$source_dir" apply --check "$source_patch" 2>/dev/null; then
    git -C "$source_dir" apply "$source_patch"
  elif git -C "$source_dir" apply --reverse --check "$source_patch" 2>/dev/null; then
    echo "==> Source patch is already applied: $(basename "$source_patch")"
  else
    echo "ERROR: source patch does not apply cleanly to $resolved_ref: $source_patch" >&2
    exit 3
  fi
done

echo "==> Rendering ai-dynamo vLLM runtime at $resolved_ref"
"$python_bin" "$source_dir/container/render.py" \
  --framework vllm \
  --target runtime \
  --platform linux/amd64 \
  --cuda-version 13.0 \
  --output-short-filename

image_args=()
for image in "$@"; do
  image_args+=(-t "$image")
done

echo "==> Building source runtime from $source_repo@$resolved_ref"
"$docker_bin" build \
  --progress=plain \
  --platform linux/amd64 \
  --target runtime \
  --build-arg "RUNTIME_IMAGE_TAG=$vllm_runtime_tag" \
  --build-arg "DYNAMO_COMMIT_SHA=$resolved_ref" \
  --label "org.opencontainers.image.source=$source_repo" \
  --label "org.opencontainers.image.revision=$resolved_ref" \
  --label "io.daedalus.dynamo.patch-bundle.sha256=$patch_bundle_sha256" \
  "${image_args[@]}" \
  -f "$source_dir/container/rendered.Dockerfile" \
  "$source_dir"
