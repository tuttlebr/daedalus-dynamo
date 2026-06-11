#!/usr/bin/env bash
# Build, push, deploy, and delete the integrated Nemotron Omni Photo Tagger demo.
#
# Defaults target the Daedalus demo registry and overlay. Override with env:
#   ./deploy.sh [deploy|delete|render]  default: deploy
#   REGISTRY                       default: daedalus.ddns.me:5050/btuttle
#   IMAGE_TAG                      default: UTC timestamp, e.g. 20260513140530
#   ALLOW_LATEST=1                 suppress warning when IMAGE_TAG=latest
#   NEMOTRON_IMAGE_REPOSITORY      default: ${REGISTRY}/nemotron-3-nano-omni
#   PHOTO_TAGGER_IMAGE_REPOSITORY  default: ${REGISTRY}/photo-tagger
#   NAMESPACE                      default: nemotron-omni
#   OVERLAY                        default: k8s/overlays/daedalus
#   HF_TOKEN                       optional; creates/updates hf-token-secret
#   HF_TOKEN_SECRET                default: hf-token-secret
#   POSTGRES_PASSWORD              optional; used only when creating the app DB secret
#   PHOTO_TAGGER_AUTH_TOKEN        optional; used only when creating the app auth secret
#   BASE_IMAGE                     optional Nemotron Docker build arg
#   SKIP_BUILD=1                   skip docker builds
#   SKIP_PUSH=1                    skip docker pushes
#   SKIP_DEPLOY=1                  build/push only
#   DELETE_DATA=1                  delete PVCs during ./deploy.sh delete
#   DELETE_NAMESPACE=1             delete namespace after ./deploy.sh delete
#   DELETE_TIMEOUT_SECONDS         default: 300
#   PREFLIGHT_TIMEOUT_SECONDS      default: 20
#   FORCE_CLEAR_FINALIZERS=1       remove stuck Grove finalizers during delete

set -euo pipefail

cd "$(dirname "$0")"
REPO_ROOT="$(pwd)"

REGISTRY="${REGISTRY:-daedalus.ddns.me:5050/btuttle}"
if [[ -z "${IMAGE_TAG:-}" ]]; then
  IMAGE_TAG="$(date -u +%Y%m%d%H%M%S)"
fi
NEMOTRON_IMAGE_REPOSITORY="${NEMOTRON_IMAGE_REPOSITORY:-${REGISTRY}/nemotron-3-nano-omni}"
PHOTO_TAGGER_IMAGE_REPOSITORY="${PHOTO_TAGGER_IMAGE_REPOSITORY:-${REGISTRY}/photo-tagger}"
NEMOTRON_IMAGE="${NEMOTRON_IMAGE_REPOSITORY}:${IMAGE_TAG}"
PHOTO_TAGGER_IMAGE="${PHOTO_TAGGER_IMAGE_REPOSITORY}:${IMAGE_TAG}"
NAMESPACE="${NAMESPACE:-nemotron-omni}"
OVERLAY="${OVERLAY:-${REPO_ROOT}/k8s/overlays/daedalus}"
HF_TOKEN_SECRET="${HF_TOKEN_SECRET:-hf-token-secret}"
POSTGRES_SECRET="photo-tagger-postgres"
PHOTO_TAGGER_AUTH_SECRET="photo-tagger-auth"
ACTION="${1:-deploy}"
APP_LABEL="app.kubernetes.io/part-of=nemotron-omni-photo-demo"
DYNAMO_DEPLOYMENT="nemotron-omni-vllm-agg"
DYNAMO_LABEL="nvidia.com/dynamo-graph-deployment-name=${DYNAMO_DEPLOYMENT}"
PREFLIGHT_TIMEOUT_SECONDS="${PREFLIGHT_TIMEOUT_SECONDS:-20}"
DELETE_TIMEOUT_SECONDS="${DELETE_TIMEOUT_SECONDS:-300}"
API_RESOURCES_CACHE=""

OVERLAY_PATH="$(cd "$OVERLAY" && pwd)"

usage() {
  cat <<EOF
Usage: $0 [deploy|delete|render]

Commands:
  deploy  Build, push, and apply the integrated demo. This is the default.
  delete  Delete the demo resources before deleting the namespace.
  render  Print the rendered Kubernetes manifest without applying it.

Delete options:
  DELETE_DATA=1       Also delete PVCs: model-cache, redis-data, and Postgres data.
  DELETE_NAMESPACE=1  Delete ${NAMESPACE} after resources are gone. Requires DELETE_DATA=1.
  FORCE_CLEAR_FINALIZERS=1
                        If Grove resources are already deleting but stuck past
                        the timeout, clear their finalizers. PVCs are still
                        preserved unless DELETE_DATA=1 is also set.
EOF
}

warn_mutable_image_tag() {
  if [[ "${IMAGE_TAG}" == "latest" && "${ALLOW_LATEST:-0}" != "1" ]]; then
    echo "WARN: IMAGE_TAG=latest is mutable. Set ALLOW_LATEST=1 to acknowledge, or omit IMAGE_TAG for timestamp tag." >&2
  fi
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

api_resources() {
  if [[ -z "${API_RESOURCES_CACHE}" ]]; then
    API_RESOURCES_CACHE="$(kubectl api-resources --verbs=list -o name 2> /dev/null || true)"
  fi
  printf '%s\n' "${API_RESOURCES_CACHE}"
}

api_resource_exists() {
  local resource="$1"
  api_resources | grep -Fxq "${resource}"
}

namespace_exists() {
  kubectl get namespace "${NAMESPACE}" > /dev/null 2>&1
}

ensure_namespace_not_terminating() {
  local deletion_timestamp

  if ! namespace_exists; then
    return
  fi

  deletion_timestamp="$(
    kubectl get namespace "${NAMESPACE}" \
      -o jsonpath='{.metadata.deletionTimestamp}' \
      2> /dev/null || true
  )"
  if [[ -n "${deletion_timestamp}" ]]; then
    die "Namespace ${NAMESPACE} is terminating. Wait for it to finish before deploying."
  fi
}

resource_exists() {
  local object="$1"
  kubectl get "${object}" -n "${NAMESPACE}" > /dev/null 2>&1
}

object_deletion_timestamp() {
  local object="$1"
  kubectl get "${object}" \
    -n "${NAMESPACE}" \
    -o jsonpath='{.metadata.deletionTimestamp}' \
    2> /dev/null || true
}

operator_rollout_available() {
  local namespace="$1"
  local deployment="$2"

  kubectl get deployment "${deployment}" -n "${namespace}" > /dev/null 2>&1 &&
    kubectl rollout status "deployment/${deployment}" \
      -n "${namespace}" \
      --timeout="${PREFLIGHT_TIMEOUT_SECONDS}s" \
      > /dev/null
}

preflight_cluster() {
  echo "==> Checking Kubernetes and Dynamo prerequisites"
  kubectl version --request-timeout="${PREFLIGHT_TIMEOUT_SECONDS}s" > /dev/null
  ensure_namespace_not_terminating

  api_resource_exists "dynamographdeployments.nvidia.com" ||
    die "DynamoGraphDeployment CRD is not installed."
  api_resource_exists "podcliquesets.grove.io" ||
    die "Grove PodCliqueSet CRD is not installed."

  operator_rollout_available "dynamo-system" "dynamo-platform-dynamo-operator-controller-manager" ||
    die "Dynamo operator deployment is not available in namespace dynamo-system."
  operator_rollout_available "grove-system" "grove-operator" ||
    die "Grove operator deployment is not available in namespace grove-system."
}

known_dynamo_controller_objects() {
  api_resource_exists "dynamographdeployments.nvidia.com" &&
    printf 'dynamographdeployments.nvidia.com/%s\n' "${DYNAMO_DEPLOYMENT}"
  api_resource_exists "dynamographdeploymentrequests.nvidia.com" &&
    printf 'dynamographdeploymentrequests.nvidia.com/%s\n' "${DYNAMO_DEPLOYMENT}"
  api_resource_exists "podcliquesets.grove.io" &&
    printf 'podcliquesets.grove.io/%s\n' "${DYNAMO_DEPLOYMENT}"
  api_resource_exists "podcliques.grove.io" && {
    printf 'podcliques.grove.io/%s-0-frontend\n' "${DYNAMO_DEPLOYMENT}"
    printf 'podcliques.grove.io/%s-0-modelexpress\n' "${DYNAMO_DEPLOYMENT}"
    printf 'podcliques.grove.io/%s-0-vllmworker\n' "${DYNAMO_DEPLOYMENT}"
  }
  api_resource_exists "podgangs.scheduler.grove.io" &&
    printf 'podgangs.scheduler.grove.io/%s-0\n' "${DYNAMO_DEPLOYMENT}"
}

label_selected_controller_objects() {
  local resource
  local selector

  for resource in \
    dynamocomponentdeployments.nvidia.com \
    dynamographdeploymentrequests.nvidia.com \
    dynamographdeploymentscalingadapters.nvidia.com \
    podcliques.grove.io \
    podcliquescalinggroups.grove.io \
    podgangs.scheduler.grove.io; do
    if ! api_resource_exists "${resource}"; then
      continue
    fi
    kubectl get "${resource}" \
      -n "${NAMESPACE}" \
      -l "${DYNAMO_LABEL}" \
      --ignore-not-found \
      -o name \
      2> /dev/null || true
  done

  selector="app.kubernetes.io/part-of=${DYNAMO_DEPLOYMENT}"
  for resource in podcliques.grove.io podcliquescalinggroups.grove.io podgangs.scheduler.grove.io; do
    if ! api_resource_exists "${resource}"; then
      continue
    fi
    kubectl get "${resource}" \
      -n "${NAMESPACE}" \
      -l "${selector}" \
      --ignore-not-found \
      -o name \
      2> /dev/null || true
  done
}

existing_dynamo_controller_objects() {
  if ! namespace_exists; then
    return
  fi

  {
    while IFS= read -r object; do
      if [[ -n "${object}" ]] && resource_exists "${object}"; then
        printf '%s\n' "${object}"
      fi
    done < <(known_dynamo_controller_objects)
    label_selected_controller_objects
  } | sort -u
}

deleting_dynamo_controller_objects() {
  local deletion_timestamp
  local object

  while IFS= read -r object; do
    [[ -n "${object}" ]] || continue
    deletion_timestamp="$(object_deletion_timestamp "${object}")"
    if [[ -n "${deletion_timestamp}" ]]; then
      printf '%s\n' "${object}"
    fi
  done < <(existing_dynamo_controller_objects)
}

ensure_no_stuck_controller_objects() {
  local deleting

  deleting="$(deleting_dynamo_controller_objects)"
  if [[ -z "${deleting}" ]]; then
    return
  fi

  echo "ERROR: Found deleting Dynamo/Grove resources that must finish before deploy:" >&2
  echo "${deleting}" >&2
  echo >&2
  echo "Run this to recover while preserving PVCs:" >&2
  echo "  FORCE_CLEAR_FINALIZERS=1 ./deploy.sh delete" >&2
  exit 1
}

validate_rendered_manifest() {
  echo "==> Validating rendered manifest with server-side dry-run"
  render_manifest | kubectl apply --dry-run=server -n "${NAMESPACE}" -f - > /dev/null
}

delete_object_if_present() {
  local object="$1"

  kubectl delete "${object}" \
    -n "${NAMESPACE}" \
    --ignore-not-found \
    --wait=false \
    > /dev/null 2>&1 || true
}

delete_labeled_objects_for_resource() {
  local resource="$1"
  local selector="$2"
  local object

  if ! api_resource_exists "${resource}"; then
    return
  fi

  while IFS= read -r object; do
    [[ -n "${object}" ]] || continue
    delete_object_if_present "${object}"
  done < <(
    kubectl get "${resource}" \
      -n "${NAMESPACE}" \
      -l "${selector}" \
      --ignore-not-found \
      -o name \
      2> /dev/null || true
  )
}

delete_dynamo_controller_objects() {
  local object

  echo "==> Deleting Dynamo and Grove controller resources for ${DYNAMO_DEPLOYMENT}"
  while IFS= read -r object; do
    [[ -n "${object}" ]] || continue
    delete_object_if_present "${object}"
  done < <(known_dynamo_controller_objects)

  delete_labeled_objects_for_resource "dynamocomponentdeployments.nvidia.com" "${DYNAMO_LABEL}"
  delete_labeled_objects_for_resource "dynamographdeploymentrequests.nvidia.com" "${DYNAMO_LABEL}"
  delete_labeled_objects_for_resource "dynamographdeploymentscalingadapters.nvidia.com" "${DYNAMO_LABEL}"
  delete_labeled_objects_for_resource "podcliques.grove.io" "${DYNAMO_LABEL}"
  delete_labeled_objects_for_resource "podcliquescalinggroups.grove.io" "${DYNAMO_LABEL}"
  delete_labeled_objects_for_resource "podgangs.scheduler.grove.io" "${DYNAMO_LABEL}"
  delete_labeled_objects_for_resource "podcliques.grove.io" "app.kubernetes.io/part-of=${DYNAMO_DEPLOYMENT}"
  delete_labeled_objects_for_resource "podcliquescalinggroups.grove.io" "app.kubernetes.io/part-of=${DYNAMO_DEPLOYMENT}"
  delete_labeled_objects_for_resource "podgangs.scheduler.grove.io" "app.kubernetes.io/part-of=${DYNAMO_DEPLOYMENT}"

  kubectl delete \
    service/"${DYNAMO_DEPLOYMENT}-frontend" \
    service/"${DYNAMO_DEPLOYMENT}-modelexpress" \
    service/"${DYNAMO_DEPLOYMENT}-vllmworker" \
    serviceaccount/"${DYNAMO_DEPLOYMENT}-k8s-service-discovery" \
    role/"${DYNAMO_DEPLOYMENT}-k8s-service-discovery-role" \
    rolebinding/"${DYNAMO_DEPLOYMENT}-k8s-service-discovery-binding" \
    -n "${NAMESPACE}" \
    --ignore-not-found \
    --wait=false \
    > /dev/null 2>&1 || true
}

clear_grove_finalizers_if_allowed() {
  local remaining="$1"
  local object

  if [[ "${FORCE_CLEAR_FINALIZERS:-0}" != "1" ]]; then
    return 1
  fi

  echo "==> FORCE_CLEAR_FINALIZERS=1 set; clearing finalizers from deleting demo Grove resources"
  while IFS= read -r object; do
    [[ -n "${object}" ]] || continue
    case "${object}" in
      podcliquesets.grove.io/*|podcliques.grove.io/*|podcliquescalinggroups.grove.io/*|podgangs.scheduler.grove.io/*)
        if [[ -n "$(object_deletion_timestamp "${object}")" ]]; then
          kubectl patch "${object}" \
            -n "${NAMESPACE}" \
            --type=merge \
            -p '{"metadata":{"finalizers":[]}}' \
            > /dev/null 2>&1 || true
        fi
        ;;
    esac
  done <<< "${remaining}"
}

wait_for_dynamo_controller_cleanup() {
  local end=$((SECONDS + DELETE_TIMEOUT_SECONDS))
  local remaining

  while (( SECONDS < end )); do
    remaining="$(existing_dynamo_controller_objects)"
    if [[ -z "${remaining}" ]]; then
      return 0
    fi

    echo "==> Waiting for Dynamo/Grove controller resources to disappear..."
    sleep 5
  done

  remaining="$(existing_dynamo_controller_objects)"
  if clear_grove_finalizers_if_allowed "${remaining}"; then
    end=$((SECONDS + 60))
    while (( SECONDS < end )); do
      remaining="$(existing_dynamo_controller_objects)"
      if [[ -z "${remaining}" ]]; then
        return 0
      fi
      echo "==> Waiting after clearing Grove finalizers..."
      sleep 5
    done
  fi

  echo "ERROR: Timed out waiting for Dynamo/Grove controller resources to disappear:" >&2
  existing_dynamo_controller_objects >&2
  echo >&2
  echo "If these resources are stuck in deletion and belong to this demo, retry with:" >&2
  echo "  FORCE_CLEAR_FINALIZERS=1 ./deploy.sh delete" >&2
  return 1
}

ensure_hf_token_secret() {
  kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

  if [[ -n "${HF_TOKEN:-}" ]]; then
    echo "==> Creating/updating ${HF_TOKEN_SECRET} in namespace ${NAMESPACE}"
    kubectl create secret generic "${HF_TOKEN_SECRET}" \
      --from-literal=HF_TOKEN="${HF_TOKEN}" \
      -n "${NAMESPACE}" \
      --dry-run=client -o yaml | kubectl apply -f -
    kubectl label secret "${HF_TOKEN_SECRET}" -n "${NAMESPACE}" "${APP_LABEL}" --overwrite > /dev/null
    return
  fi

  if kubectl get secret "${HF_TOKEN_SECRET}" -n "${NAMESPACE}" > /dev/null 2>&1; then
    echo "==> Reusing existing ${HF_TOKEN_SECRET} in namespace ${NAMESPACE}"
    kubectl label secret "${HF_TOKEN_SECRET}" -n "${NAMESPACE}" "${APP_LABEL}" --overwrite > /dev/null
    return
  fi

  echo "ERROR: ${HF_TOKEN_SECRET} does not exist in namespace ${NAMESPACE}." >&2
  echo "Set HF_TOKEN and re-run, or create the secret before deploying." >&2
  exit 1
}

generate_postgres_password() {
  if command -v openssl > /dev/null 2>&1; then
    openssl rand -base64 32
    return
  fi
  od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
  printf '\n'
}

ensure_photo_tagger_postgres_secret() {
  kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

  if kubectl get secret "${POSTGRES_SECRET}" -n "${NAMESPACE}" > /dev/null 2>&1; then
    echo "==> Reusing existing ${POSTGRES_SECRET} in namespace ${NAMESPACE}"
    if [[ -n "${POSTGRES_PASSWORD:-}" ]]; then
      echo "WARN: POSTGRES_PASSWORD was provided but ${POSTGRES_SECRET} already exists; preserving existing password." >&2
    fi
    kubectl label secret "${POSTGRES_SECRET}" -n "${NAMESPACE}" "${APP_LABEL}" --overwrite > /dev/null
    return
  fi

  local password
  password="${POSTGRES_PASSWORD:-$(generate_postgres_password)}"
  echo "==> Creating ${POSTGRES_SECRET} in namespace ${NAMESPACE}"
  kubectl create secret generic "${POSTGRES_SECRET}" \
    --from-literal=password="${password}" \
    -n "${NAMESPACE}" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl label secret "${POSTGRES_SECRET}" -n "${NAMESPACE}" "${APP_LABEL}" --overwrite > /dev/null
}

ensure_photo_tagger_auth_secret() {
  kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

  if kubectl get secret "${PHOTO_TAGGER_AUTH_SECRET}" -n "${NAMESPACE}" > /dev/null 2>&1; then
    echo "==> Reusing existing ${PHOTO_TAGGER_AUTH_SECRET} in namespace ${NAMESPACE}"
    if [[ -n "${PHOTO_TAGGER_AUTH_TOKEN:-}" ]]; then
      echo "WARN: PHOTO_TAGGER_AUTH_TOKEN was provided but ${PHOTO_TAGGER_AUTH_SECRET} already exists; preserving existing token." >&2
    fi
    kubectl label secret "${PHOTO_TAGGER_AUTH_SECRET}" -n "${NAMESPACE}" "${APP_LABEL}" --overwrite > /dev/null
    return
  fi

  local token
  token="${PHOTO_TAGGER_AUTH_TOKEN:-$(generate_postgres_password)}"
  echo "==> Creating ${PHOTO_TAGGER_AUTH_SECRET} in namespace ${NAMESPACE}"
  kubectl create secret generic "${PHOTO_TAGGER_AUTH_SECRET}" \
    --from-literal=token="${token}" \
    -n "${NAMESPACE}" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl label secret "${PHOTO_TAGGER_AUTH_SECRET}" -n "${NAMESPACE}" "${APP_LABEL}" --overwrite > /dev/null
}

build_images() {
  if [[ "${SKIP_BUILD:-0}" == "1" ]]; then
    return
  fi

  NEMOTRON_BUILD_ARGS=()
  if [[ -n "${BASE_IMAGE:-}" ]]; then
    NEMOTRON_BUILD_ARGS+=(--build-arg "BASE_IMAGE=${BASE_IMAGE}")
  fi

  echo "==> Building ${NEMOTRON_IMAGE}"
  docker build "${NEMOTRON_BUILD_ARGS[@]}" -t "${NEMOTRON_IMAGE}" -f Dockerfile .

  echo "==> Building ${PHOTO_TAGGER_IMAGE}"
  docker build -t "${PHOTO_TAGGER_IMAGE}" -f photo-tagger/Dockerfile photo-tagger
}

push_images() {
  if [[ "${SKIP_PUSH:-0}" == "1" ]]; then
    return
  fi

  echo "==> Pushing ${NEMOTRON_IMAGE}"
  docker push "${NEMOTRON_IMAGE}"

  echo "==> Pushing ${PHOTO_TAGGER_IMAGE}"
  docker push "${PHOTO_TAGGER_IMAGE}"
}

prepare_render_dir() {
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  OVERLAY_REF="$(realpath --relative-to="$TMP" "$OVERLAY_PATH")"

  cat > "${TMP}/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ${OVERLAY_REF}
patches:
  - path: dynamo-images.yaml
    target:
      group: nvidia.com
      version: v1alpha1
      kind: DynamoGraphDeployment
      name: nemotron-omni-vllm-agg
  - path: photo-tagger-image.yaml
    target:
      group: apps
      version: v1
      kind: Deployment
      name: photo-tagger
EOF

  cat > "${TMP}/dynamo-images.yaml" <<EOF
- op: replace
  path: /spec/services/Frontend/extraPodSpec/mainContainer/image
  value: ${NEMOTRON_IMAGE}
- op: replace
  path: /spec/services/VllmWorker/extraPodSpec/mainContainer/image
  value: ${NEMOTRON_IMAGE}
EOF

  cat > "${TMP}/photo-tagger-image.yaml" <<EOF
- op: replace
  path: /spec/template/spec/containers/0/image
  value: ${PHOTO_TAGGER_IMAGE}
EOF
}

render_manifest() {
  kubectl kustomize --load-restrictor=LoadRestrictionsNone "${TMP}"
}

apply_deployment() {
  if [[ "${SKIP_DEPLOY:-0}" == "1" ]]; then
    build_images
    push_images
    echo "==> SKIP_DEPLOY=1 set; build/push completed without applying Kubernetes resources."
    return
  fi

  preflight_cluster
  ensure_hf_token_secret
  ensure_photo_tagger_postgres_secret
  ensure_photo_tagger_auth_secret
  ensure_no_stuck_controller_objects
  prepare_render_dir
  validate_rendered_manifest

  build_images
  push_images

  echo "==> Applying integrated demo to namespace ${NAMESPACE}"
  render_manifest | kubectl apply -n "${NAMESPACE}" -f -

  echo
  echo "Done. Watch pods with:"
  echo "  kubectl get pods -n ${NAMESPACE} -w"
  echo
  echo "Open the Photo Tagger UI:"
  echo "  http://<any-node-ip>:30150/"
  echo
  echo "For direct Nemotron API testing:"
  echo "  kubectl port-forward -n ${NAMESPACE} svc/${DYNAMO_DEPLOYMENT}-frontend 8000:8000"
}

delete_by_label() {
  local selector="$1"
  local object
  local resource

  while IFS= read -r resource; do
    if [[ -z "${resource}" ]]; then
      continue
    fi
    if [[ "${DELETE_DATA:-0}" != "1" && "${resource}" == "persistentvolumeclaims" ]]; then
      continue
    fi
    if [[ "${DELETE_DATA:-0}" != "1" && "${resource}" == "secrets" ]]; then
      while IFS= read -r object; do
        [[ -n "${object}" ]] || continue
        if [[ "${object}" == "secret/${POSTGRES_SECRET}" ]]; then
          continue
        fi
        delete_object_if_present "${object}"
      done < <(
        kubectl get "${resource}" \
          -n "${NAMESPACE}" \
          -l "${selector}" \
          --ignore-not-found \
          -o name \
          2> /dev/null || true
      )
      continue
    fi

    kubectl delete "${resource}" \
      -n "${NAMESPACE}" \
      -l "${selector}" \
      --ignore-not-found \
      --wait=false \
      > /dev/null 2>&1 || true
  done < <(kubectl api-resources --verbs=list --namespaced -o name 2> /dev/null || true)
}

list_namespace_objects() {
  local resource
  local resources

  if ! resources="$(kubectl api-resources --verbs=list --namespaced -o name 2>&1)"; then
    echo "ERROR: Kubernetes API discovery failed while checking ${NAMESPACE}." >&2
    echo "Fix broken APIService entries before deleting the namespace:" >&2
    echo "  kubectl get apiservice | grep False" >&2
    echo "${resources}" >&2
    return 1
  fi

  while IFS= read -r resource; do
    if [[ -z "${resource}" ]]; then
      continue
    fi

    kubectl get "${resource}" \
      -n "${NAMESPACE}" \
      --ignore-not-found \
      -o name \
      2> /dev/null || true
  done <<< "${resources}"
}

wait_for_namespace_empty() {
  local timeout="${DELETE_TIMEOUT_SECONDS}"
  local end=$((SECONDS + timeout))
  local remaining

  while (( SECONDS < end )); do
    if ! remaining="$(list_namespace_objects)"; then
      return 1
    fi
    if [[ -z "${remaining}" ]]; then
      return 0
    fi

    echo "==> Waiting for namespace ${NAMESPACE} to empty..."
    sleep 5
  done

  echo "ERROR: Timed out waiting for namespace ${NAMESPACE} to empty. Remaining objects:" >&2
  list_namespace_objects >&2
  return 1
}

delete_deployment() {
  if ! namespace_exists; then
    echo "==> Namespace ${NAMESPACE} does not exist; nothing to delete."
    return
  fi

  prepare_render_dir

  echo "==> Deleting ${DYNAMO_DEPLOYMENT} first so the Dynamo operator can clean up its children"
  kubectl delete dynamographdeployment.nvidia.com "${DYNAMO_DEPLOYMENT}" \
    -n "${NAMESPACE}" \
    --ignore-not-found \
    --wait=false || true

  delete_dynamo_controller_objects

  echo "==> Deleting rendered demo resources"
  if [[ "${DELETE_DATA:-0}" == "1" ]]; then
    render_manifest | kubectl delete -n "${NAMESPACE}" -f - --ignore-not-found --wait=false || true
  else
    kubectl delete \
      deployment/photo-tagger \
      deployment/redis \
      statefulset/photo-tagger-postgres \
      service/photo-tagger \
      service/photo-tagger-np \
      service/photo-tagger-postgres \
      service/redis \
      configmap/photo-tagger-config \
      secret/"${HF_TOKEN_SECRET}" \
      -n "${NAMESPACE}" \
      --ignore-not-found \
      --wait=false || true
  fi

  echo "==> Deleting label-selected leftovers from demo controllers"
  delete_by_label "${DYNAMO_LABEL}"
  delete_by_label "${APP_LABEL}"

  wait_for_dynamo_controller_cleanup

  if [[ "${DELETE_DATA:-0}" == "1" ]]; then
    echo "==> Deleting known demo PVCs"
    kubectl delete pvc \
      model-cache \
      redis-data \
      postgres-data-photo-tagger-postgres-0 \
      -n "${NAMESPACE}" \
      --ignore-not-found \
      --wait=false || true
    kubectl delete secret "${POSTGRES_SECRET}" \
      -n "${NAMESPACE}" \
      --ignore-not-found \
      --wait=false || true
  else
    echo "==> Preserving PVCs and ${POSTGRES_SECRET}. Set DELETE_DATA=1 to remove model-cache, redis-data, Postgres data, and its password secret."
  fi

  if [[ "${DELETE_NAMESPACE:-0}" == "1" ]]; then
    if [[ "${DELETE_DATA:-0}" != "1" ]]; then
      echo "ERROR: DELETE_NAMESPACE=1 requires DELETE_DATA=1; PVCs keep the namespace non-empty." >&2
      exit 1
    fi

    wait_for_namespace_empty
    echo "==> Deleting namespace ${NAMESPACE}"
    kubectl delete namespace "${NAMESPACE}" --ignore-not-found
  fi
}

case "${ACTION}" in
  deploy)
    warn_mutable_image_tag
    apply_deployment
    ;;
  delete)
    delete_deployment
    ;;
  render)
    warn_mutable_image_tag
    prepare_render_dir
    render_manifest
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
