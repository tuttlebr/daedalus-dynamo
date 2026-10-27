#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

KUBECTL="${KUBECTL:-kubectl}"
HELM="${HELM:-helm}"
CURL="${CURL:-curl}"
PYTHON="${PYTHON:-python3}"

EXPECTED_CONTEXT="${EXPECTED_CONTEXT:-kubernetes-admin@kubernetes}"
MODEL_NAMESPACE="${NAMESPACE:-daedalus-dynamo}"
GROVE_NAMESPACE="${GROVE_NAMESPACE:-grove-system}"
GROVE_RELEASE="${GROVE_RELEASE:-grove}"
GROVE_MIN_VERSION="${GROVE_VERSION:-v0.1.0-alpha.17}"
GROVE_CHART="${GROVE_CHART:-oci://ghcr.io/ai-dynamo/grove/grove-charts}"
GROVE_RESOURCE_VALUES="${GROVE_RESOURCE_VALUES:-config/grove-resources.yaml}"
GROVE_INIT_RESOURCE_PATCH="${GROVE_INIT_RESOURCE_PATCH:-config/grove-init-resources.patch.json}"
DYNAMO_PLATFORM_NAMESPACE="${DYNAMO_PLATFORM_NAMESPACE:-dynamo-system}"
DYNAMO_PLATFORM_RELEASE="${DYNAMO_PLATFORM_RELEASE:-dynamo-platform}"
DYNAMO_PLATFORM_MIN_VERSION="${DYNAMO_PLATFORM_VERSION:-1.6.0}"
DYNAMO_PLATFORM_CHART_BASE_URL="${DYNAMO_PLATFORM_CHART_BASE_URL:-https://helm.ngc.nvidia.com/nvidia/ai-dynamo/charts}"
PLATFORM_STORAGE_VALUES="${PLATFORM_STORAGE_VALUES:-config/dynamo-platform-storage.yaml}"
PLATFORM_INIT_RESOURCE_PATCH="${PLATFORM_INIT_RESOURCE_PATCH:-config/dynamo-platform-init-resources.patch.json}"
LWS_NAMESPACE="${LWS_NAMESPACE:-lws-system}"
LWS_RESOURCE_PATCH="${LWS_RESOURCE_PATCH:-config/lws-resources.patch.json}"
GROVE_TERMINATION_DELAY="${GROVE_TERMINATION_DELAY:-15m}"
HELM_TIMEOUT="${HELM_TIMEOUT:-15m}"
RECOVERY_TIMEOUT="${SCHEDULING_RECOVERY_TIMEOUT:-45m}"
POLL_SECONDS="${SCHEDULING_RECOVERY_POLL_SECONDS:-15}"
DEDICATED_GPU_PRODUCT="${DEDICATED_GPU_PRODUCT:-NVIDIA-GB10}"
DEDICATED_GPU_TAINT_KEY="${DEDICATED_GPU_TAINT_KEY:-nvidia.com/gpu}"
DEDICATED_GPU_TAINT_VALUE="${DEDICATED_GPU_TAINT_VALUE:-true}"
DEDICATED_GPU_TAINT_EFFECT="${DEDICATED_GPU_TAINT_EFFECT:-NoSchedule}"

MODE="apply"
if [[ "${1:-}" == "--check" ]]; then
  MODE="check"
elif [[ $# -ne 0 ]]; then
  echo "Usage: ${0##*/} [--check]" >&2
  exit 2
fi

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $1" >&2
    exit 127
  fi
}

duration_seconds() {
  local value="$1" amount unit
  if [[ ! "${value}" =~ ^([1-9][0-9]*)([smh])$ ]]; then
    echo "ERROR: unsupported duration ${value}; use a positive integer followed by s, m, or h." >&2
    exit 2
  fi
  amount="${BASH_REMATCH[1]}"
  unit="${BASH_REMATCH[2]}"
  case "${unit}" in
    s) printf '%s\n' "${amount}" ;;
    m) printf '%s\n' "$((amount * 60))" ;;
    h) printf '%s\n' "$((amount * 3600))" ;;
  esac
}

release_chart_version() {
  local namespace="$1" release="$2" chart_prefix="$3"
  "${HELM}" list --namespace "${namespace}" --output json |
    "${PYTHON}" -c '
import json
import sys

release, prefix = sys.argv[1:]
for item in json.load(sys.stdin):
    if item.get("name") == release:
        chart = item.get("chart", "")
        if not chart.startswith(prefix):
            raise SystemExit(f"release {release} uses unexpected chart {chart}")
        print(chart[len(prefix):])
        break
else:
    raise SystemExit(f"release {release} was not found")
' "${release}" "${chart_prefix}"
}

version_at_least() {
  local current="${1#v}" minimum="${2#v}"
  [[ "$(printf '%s\n%s\n' "${minimum}" "${current}" | sort -V | head -1)" == "${minimum}" ]]
}

apply_deployment_resource_patch() {
  local namespace="$1" deployment="$2" patch_file="$3" patch
  # These upstream installers do not expose the affected resources as values.
  # Validate names before a strategic patch can accidentally append a container.
  patch="$(
    "${KUBECTL}" --context "${EXPECTED_CONTEXT}" get deployment "${deployment}" \
      --namespace "${namespace}" --output json |
      "${PYTHON}" -c '
import json
import sys

current = json.load(sys.stdin)
with open(sys.argv[1], encoding="utf-8") as patch_file:
    patch = json.load(patch_file)
pod = current["spec"]["template"]["spec"]
for group in ("containers", "initContainers"):
    existing = {container["name"] for container in pod.get(group, [])}
    for container in patch["spec"]["template"]["spec"].get(group, []):
        name = container["name"]
        if name not in existing:
            raise SystemExit(
                f"refusing resource patch: {group} {name} is absent"
            )
patch["metadata"] = {"resourceVersion": current["metadata"]["resourceVersion"]}
print(json.dumps(patch))
' "${patch_file}"
  )"
  echo "==> Applying resource requests to ${namespace}/${deployment}"
  "${KUBECTL}" --context "${EXPECTED_CONTEXT}" patch deployment "${deployment}" \
    --namespace "${namespace}" --type=strategic --patch "${patch}" \
    --dry-run=server --output name >/dev/null
  "${KUBECTL}" --context "${EXPECTED_CONTEXT}" patch deployment "${deployment}" \
    --namespace "${namespace}" --type=strategic --patch "${patch}"
  "${KUBECTL}" --context "${EXPECTED_CONTEXT}" rollout status "deployment/${deployment}" \
    --namespace "${namespace}" --timeout="${HELM_TIMEOUT}"
}

prepare_grove_topology_crd_migration() {
  local old_crd="clustertopologies.grove.io" has_obsolete_alias short_names patch
  if ! "${KUBECTL}" get crd "${old_crd}" >/dev/null 2>&1; then
    return 0
  fi
  has_obsolete_alias="$(
    "${KUBECTL}" get crd "${old_crd}" --output json |
      "${PYTHON}" -c '
import json
import sys

names = json.load(sys.stdin).get("spec", {}).get("names", {}).get("shortNames", [])
print("true" if "ct" in names else "false")
'
  )"
  if [[ "${has_obsolete_alias}" != "true" ]]; then
    return 0
  fi
  short_names="$(
    "${KUBECTL}" get crd "${old_crd}" --output json |
      "${PYTHON}" -c '
import json
import sys

names = json.load(sys.stdin).get("spec", {}).get("names", {}).get("shortNames", [])
print(json.dumps([name for name in names if name != "ct"]))
'
  )"
  patch="$(
    "${PYTHON}" -c '
import json
import sys

print(json.dumps({"spec": {"names": {"shortNames": json.loads(sys.argv[1])}}}))
' "${short_names}"
  )"
  echo "==> Releasing the obsolete ct alias from ${old_crd} for the ClusterTopologyBinding CRD"
  "${KUBECTL}" patch crd "${old_crd}" --type=merge --patch "${patch}" >/dev/null
}

device_plugins_ready() {
  "${KUBECTL}" get daemonsets --all-namespaces --output json |
    "${PYTHON}" -c '
import json
import sys

all_plugins = [
    item for item in json.load(sys.stdin)["items"]
    if "nvidia-device-plugin" in item["metadata"]["name"]
]
plugins = [
    item for item in all_plugins
    if int(item.get("status", {}).get("desiredNumberScheduled", 0)) > 0
]
if not plugins:
    print("ERROR: no NVIDIA device-plugin DaemonSet was found", file=sys.stderr)
    raise SystemExit(1)
not_ready = []
for item in plugins:
    status = item.get("status", {})
    desired = int(status.get("desiredNumberScheduled", 0))
    ready = int(status.get("numberReady", 0))
    if ready != desired:
        not_ready.append(
            "{}/{}={}/{}".format(
                item["metadata"]["namespace"],
                item["metadata"]["name"],
                ready,
                desired,
            )
        )
if not_ready:
    print("ERROR: NVIDIA device plugin is not ready: " + ", ".join(not_ready), file=sys.stderr)
    raise SystemExit(1)
print(", ".join(
    "{}/{}={}/{}".format(
        item["metadata"]["namespace"],
        item["metadata"]["name"],
        item.get("status", {}).get("numberReady", 0),
        item.get("status", {}).get("desiredNumberScheduled", 0),
    )
    for item in plugins
))
'
}

dedicated_gpu_nodes() {
  "${KUBECTL}" get nodes \
    --selector="nvidia.com/gpu.product=${DEDICATED_GPU_PRODUCT}" \
    --output=jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'
}

verify_dedicated_worker_tolerations() {
  "${KUBECTL}" get pods --namespace "${MODEL_NAMESPACE}" --output json |
    "${PYTHON}" -c '
import json
import sys

product, key, value, effect = sys.argv[1:]
matched = []
missing = []
for pod in json.load(sys.stdin)["items"]:
    spec = pod.get("spec", {})
    if spec.get("nodeSelector", {}).get("nvidia.com/gpu.product") != product:
        continue
    if not any(
        int(container.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0)) > 0
        for container in spec.get("containers", [])
    ):
        continue
    name = pod["metadata"]["name"]
    matched.append(name)
    tolerates = any(
        toleration.get("key") == key
        and toleration.get("effect", effect) in ("", effect)
        and (
            toleration.get("operator", "Equal") == "Exists"
            or (
                toleration.get("operator", "Equal") == "Equal"
                and str(toleration.get("value", "")) == value
            )
        )
        for toleration in spec.get("tolerations", [])
    )
    if not tolerates:
        missing.append(name)
if missing:
    print(
        "ERROR: dedicated GPU worker(s) lack the reservation toleration: "
        + ", ".join(missing),
        file=sys.stderr,
    )
    raise SystemExit(1)
print(",".join(matched))
' "${DEDICATED_GPU_PRODUCT}" "${DEDICATED_GPU_TAINT_KEY}" \
    "${DEDICATED_GPU_TAINT_VALUE}" "${DEDICATED_GPU_TAINT_EFFECT}"
}

dedicated_gpu_taints_ready() {
  "${KUBECTL}" get nodes \
    --selector="nvidia.com/gpu.product=${DEDICATED_GPU_PRODUCT}" \
    --output json |
    "${PYTHON}" -c '
import json
import sys

key, value, effect = sys.argv[1:]
items = json.load(sys.stdin)["items"]
if not items:
    print("ERROR: no dedicated GPU node was found", file=sys.stderr)
    raise SystemExit(1)
missing = []
for node in items:
    taints = node.get("spec", {}).get("taints", [])
    if not any(
        taint.get("key") == key
        and str(taint.get("value", "")) == value
        and taint.get("effect") == effect
        for taint in taints
    ):
        missing.append(node["metadata"]["name"])
if missing:
    print(
        "ERROR: dedicated GPU reservation taint is missing from: "
        + ", ".join(missing),
        file=sys.stderr,
    )
    raise SystemExit(1)
print(",".join(node["metadata"]["name"] for node in items))
' "${DEDICATED_GPU_TAINT_KEY}" "${DEDICATED_GPU_TAINT_VALUE}" \
    "${DEDICATED_GPU_TAINT_EFFECT}"
}

conflicting_gpu_pods() {
  local node_csv="$1"
  "${KUBECTL}" get pods --all-namespaces --output json |
    "${PYTHON}" -c '
import json
import sys

nodes = set(filter(None, sys.argv[1].split(",")))
key, value, effect = sys.argv[2:]
for pod in json.load(sys.stdin)["items"]:
    spec = pod.get("spec", {})
    if spec.get("nodeName") not in nodes:
        continue
    if pod.get("status", {}).get("phase") in {"Failed", "Succeeded"}:
        continue
    if not any(
        int(container.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0)) > 0
        for container in spec.get("containers", [])
    ):
        continue
    tolerates = any(
        toleration.get("key") == key
        and toleration.get("effect", effect) in ("", effect)
        and (
            toleration.get("operator", "Equal") == "Exists"
            or (
                toleration.get("operator", "Equal") == "Equal"
                and str(toleration.get("value", "")) == value
            )
        )
        for toleration in spec.get("tolerations", [])
    )
    if tolerates:
        continue
    owners = pod.get("metadata", {}).get("ownerReferences", [])
    controller = next((owner for owner in owners if owner.get("controller")), None)
    kind = (controller or {}).get("kind", "")
    name = (controller or {}).get("name", "")
    print("|".join((pod["metadata"]["namespace"], pod["metadata"]["name"], kind, name)))
' "${node_csv}" "${DEDICATED_GPU_TAINT_KEY}" \
    "${DEDICATED_GPU_TAINT_VALUE}" "${DEDICATED_GPU_TAINT_EFFECT}"
}

resolve_conflicting_deployments() {
  local node_csv="$1" namespace pod owner_kind owner_name deployment record
  local conflicts
  declare -A seen=()
  conflicts="$(conflicting_gpu_pods "${node_csv}")"
  while IFS='|' read -r namespace pod owner_kind owner_name; do
    [[ -n "${namespace}" ]] || continue
    if [[ "${owner_kind}" != "ReplicaSet" || -z "${owner_name}" ]]; then
      echo "ERROR: GPU pod ${namespace}/${pod} blocks the dedicated node and is owned by unsupported ${owner_kind:-none}/${owner_name:-none}." >&2
      return 1
    fi
    record="$("${KUBECTL}" get replicaset "${owner_name}" --namespace "${namespace}" --output jsonpath='{range .metadata.ownerReferences[?(@.controller==true)]}{.kind}{"|"}{.name}{end}')"
    IFS='|' read -r owner_kind deployment <<<"${record}"
    if [[ "${owner_kind}" != "Deployment" || -z "${deployment}" ]]; then
      echo "ERROR: GPU pod ${namespace}/${pod} does not resolve to a Deployment controller." >&2
      return 1
    fi
    seen["${namespace}|${deployment}"]=1
  done <<<"${conflicts}"
  printf '%s\n' "${!seen[@]}" | sort
}

reserve_dedicated_gpu_nodes() {
  local worker_pods node_lines node_csv deployments namespace deployment
  worker_pods="$(verify_dedicated_worker_tolerations)"
  if [[ -z "${worker_pods}" ]]; then
    echo "==> No GPU worker selects nvidia.com/gpu.product=${DEDICATED_GPU_PRODUCT}; skipping dedicated-node reservation"
    return 0
  fi
  node_lines="$(dedicated_gpu_nodes)"
  if [[ -z "${node_lines}" ]]; then
    echo "ERROR: no node exposes nvidia.com/gpu.product=${DEDICATED_GPU_PRODUCT}." >&2
    return 1
  fi
  node_csv="$(tr '\n' ',' <<<"${node_lines}" | sed 's/,$//')"
  deployments="$(resolve_conflicting_deployments "${node_csv}")"

  echo "==> Reserving ${DEDICATED_GPU_PRODUCT} node(s) ${node_csv} for worker(s) ${worker_pods}"
  while IFS= read -r node; do
    [[ -n "${node}" ]] || continue
    "${KUBECTL}" taint node "${node}" \
      "${DEDICATED_GPU_TAINT_KEY}=${DEDICATED_GPU_TAINT_VALUE}:${DEDICATED_GPU_TAINT_EFFECT}" \
      --overwrite >/dev/null
  done <<<"${node_lines}"

  while IFS='|' read -r namespace deployment; do
    [[ -n "${namespace}" ]] || continue
    echo "==> Relocating conflicting GPU deployment ${namespace}/${deployment}"
    "${KUBECTL}" rollout restart deployment/"${deployment}" --namespace "${namespace}"
    "${KUBECTL}" rollout status deployment/"${deployment}" \
      --namespace "${namespace}" --timeout="${HELM_TIMEOUT}"
  done <<<"${deployments}"

  dedicated_gpu_taints_ready >/dev/null
}

stale_grove_pods() {
  "${KUBECTL}" get pods,podcliques.grove.io \
    --namespace "${MODEL_NAMESPACE}" --output json |
    "${PYTHON}" -c '
import json
import sys

items = json.load(sys.stdin)["items"]
desired = {
    item["metadata"]["name"]: item.get("status", {}).get("currentPodTemplateHash", "")
    for item in items
    if item.get("kind") == "PodClique"
}
priority = {"Frontend": 10, "ModelExpress": 20, "Planner": 40}
stale = []
for pod in items:
    if pod.get("kind") != "Pod":
        continue
    owners = pod.get("metadata", {}).get("ownerReferences", [])
    owner = next(
        (item for item in owners if item.get("controller") and item.get("kind") == "PodClique"),
        None,
    )
    if not owner or not desired.get(owner["name"]):
        continue
    actual = pod.get("metadata", {}).get("labels", {}).get("grove.io/pod-template-hash", "")
    expected = desired[owner["name"]]
    if actual == expected:
        continue
    component = pod.get("metadata", {}).get("labels", {}).get("nvidia.com/dynamo-component", "")
    stale.append((
        priority.get(component, 30),
        pod["metadata"]["name"],
        owner["name"],
        actual or "unset",
        expected,
    ))
for record in sorted(stale):
    print("|".join(map(str, record)))
'
}

wait_for_podclique_update() {
  local podclique="$1" expected_hash="$2" timeout_seconds deadline state
  timeout_seconds="$(duration_seconds "${RECOVERY_TIMEOUT}")"
  deadline=$((SECONDS + timeout_seconds))
  while ((SECONDS < deadline)); do
    state="$(
      "${KUBECTL}" get podclique.grove.io "${podclique}" \
        --namespace "${MODEL_NAMESPACE}" --output json |
        "${PYTHON}" -c '
import json
import sys

item = json.load(sys.stdin)
status = item.get("status", {})
print("|".join(map(str, (
    status.get("currentPodTemplateHash", ""),
    item.get("spec", {}).get("replicas", 0),
    status.get("updatedReplicas", 0),
    status.get("readyReplicas", 0),
))))
'
    )"
    local current_hash replicas updated ready
    IFS='|' read -r current_hash replicas updated ready <<<"${state}"
    if [[ "${current_hash}" == "${expected_hash}" ]] &&
      ((updated == replicas && ready == replicas)); then
      return 0
    fi
    sleep "${POLL_SECONDS}"
  done
  echo "ERROR: PodClique ${MODEL_NAMESPACE}/${podclique} did not become updated and ready within ${RECOVERY_TIMEOUT}." >&2
  return 1
}

recycle_stale_grove_pods() {
  local stale count priority pod podclique actual expected
  stale="$(stale_grove_pods)"
  [[ -n "${stale}" ]] || return 0
  count="$(wc -l <<<"${stale}" | tr -d ' ')"
  if ((count > 64)); then
    echo "ERROR: refusing to recycle unexpected stale Grove pod count: ${count}." >&2
    return 1
  fi
  echo "==> Recycling ${count} pod(s) carrying pre-upgrade Grove template hashes"
  while IFS='|' read -r priority pod podclique actual expected; do
    [[ -n "${pod}" ]] || continue
    echo "==> Replacing stale pod ${MODEL_NAMESPACE}/${pod} (${actual} -> ${expected})"
    "${KUBECTL}" delete pod "${pod}" --namespace "${MODEL_NAMESPACE}" \
      --wait=true --timeout="${HELM_TIMEOUT}"
    wait_for_podclique_update "${podclique}" "${expected}"
  done <<<"${stale}"
}

terminal_admission_pods() {
  "${KUBECTL}" get pods --all-namespaces --output json |
    "${PYTHON}" -c '
import json
import sys

for pod in json.load(sys.stdin)["items"]:
    status = pod.get("status", {})
    if status.get("phase") != "Failed" or status.get("reason") != "UnexpectedAdmissionError":
        continue
    owners = pod.get("metadata", {}).get("ownerReferences", [])
    owner = next((item for item in owners if item.get("controller")), None)
    print("|".join((
        pod["metadata"]["namespace"],
        pod["metadata"]["name"],
        (owner or {}).get("kind", ""),
        (owner or {}).get("name", ""),
    )))
'
}

cleanup_terminal_admission_pods() {
  local pods count namespace pod owner_kind owner_name
  pods="$(terminal_admission_pods)"
  [[ -n "${pods}" ]] || return 0
  count="$(wc -l <<<"${pods}" | tr -d ' ')"
  if ((count > 64)); then
    echo "ERROR: refusing to remove unexpected terminal admission pod count: ${count}." >&2
    return 1
  fi
  while IFS='|' read -r namespace pod owner_kind owner_name; do
    [[ -n "${namespace}" && -n "${pod}" ]] || continue
    case "${owner_kind}" in
      PodClique | ReplicaSet | StatefulSet | DaemonSet) ;;
      *)
        echo "ERROR: terminal admission pod ${namespace}/${pod} has unsupported controller ${owner_kind:-none}/${owner_name:-none}." >&2
        return 1
        ;;
    esac
  done <<<"${pods}"
  echo "==> Removing ${count} terminal admission pod record(s) after device-plugin recovery"
  while IFS='|' read -r namespace pod owner_kind owner_name; do
    [[ -n "${namespace}" && -n "${pod}" ]] || continue
    echo "==> Removing failed pod ${namespace}/${pod} owned by ${owner_kind}/${owner_name}"
    "${KUBECTL}" delete pod "${pod}" --namespace "${namespace}" \
      --wait=true --timeout="${HELM_TIMEOUT}"
  done <<<"${pods}"
}

pod_recovery_state() {
  "${KUBECTL}" get pods --all-namespaces --output json |
    "${PYTHON}" -c '
import json
import sys

failed = []
gated = []
for pod in json.load(sys.stdin)["items"]:
    status = pod.get("status", {})
    name = "{}/{}".format(pod["metadata"]["namespace"], pod["metadata"]["name"])
    if status.get("phase") == "Failed" and status.get("reason") == "UnexpectedAdmissionError":
        failed.append(name)
    conditions = status.get("conditions", [])
    if any(
        condition.get("type") == "PodScheduled"
        and condition.get("status") == "False"
        and condition.get("reason") == "SchedulingGated"
        for condition in conditions
    ):
        gated.append(name)
print("{}|{}|{}|{}".format(
    len(failed), len(gated), ",".join(failed), ",".join(gated)
))
'
}

dgd_readiness_state() {
  "${KUBECTL}" get dynamographdeployments.nvidia.com \
    --namespace "${MODEL_NAMESPACE}" --output json |
    "${PYTHON}" -c '
import json
import sys

not_ready = []
items = json.load(sys.stdin)["items"]
for item in items:
    ready = next(
        (condition for condition in item.get("status", {}).get("conditions", [])
         if condition.get("type") == "Ready"),
        None,
    )
    if not ready or ready.get("status") != "True":
        reason = (ready or {}).get("reason", "missing_ready_condition")
        not_ready.append("{}:{}".format(item["metadata"]["name"], reason))
print("{}|{}|{}".format(len(items), len(not_ready), ",".join(not_ready)))
'
}

pcs_delay_state() {
  "${KUBECTL}" get podcliquesets.grove.io --namespace "${MODEL_NAMESPACE}" --output json |
    "${PYTHON}" -c '
import json
import sys

expected = sys.argv[1]
accepted = {expected}
if expected.endswith("m"):
    accepted.add(expected + "0s")
if expected.endswith("h"):
    accepted.add(expected + "0m0s")
mismatched = []
items = json.load(sys.stdin)["items"]
for item in items:
    delay = item.get("spec", {}).get("template", {}).get("terminationDelay", "")
    if delay not in accepted:
        mismatched.append("{}:{}".format(
            item["metadata"]["name"], delay or "unset"
        ))
print("{}|{}|{}".format(len(items), len(mismatched), ",".join(mismatched)))
' "${GROVE_TERMINATION_DELAY}"
}

report_state() {
  local pod_state dgd_state delay_state failed gated failed_names gated_names
  local dgd_total dgd_not_ready dgd_names pcs_total pcs_mismatched pcs_names
  pod_state="$(pod_recovery_state)"
  IFS='|' read -r failed gated failed_names gated_names <<<"${pod_state}"
  dgd_state="$(dgd_readiness_state)"
  IFS='|' read -r dgd_total dgd_not_ready dgd_names <<<"${dgd_state}"
  delay_state="$(pcs_delay_state)"
  IFS='|' read -r pcs_total pcs_mismatched pcs_names <<<"${delay_state}"

  printf 'Recovery state: failed-admission=%s gated=%s DGD-ready=%s/%s PCS-delay-mismatch=%s/%s\n' \
    "${failed}" "${gated}" "$((dgd_total - dgd_not_ready))" "${dgd_total}" \
    "${pcs_mismatched}" "${pcs_total}"
  [[ -z "${failed_names}" ]] || printf '  Failed admission pods: %s\n' "${failed_names}"
  [[ -z "${gated_names}" ]] || printf '  Scheduling-gated pods: %s\n' "${gated_names}"
  [[ -z "${dgd_names}" ]] || printf '  Not-ready DGDs: %s\n' "${dgd_names}"
  [[ -z "${pcs_names}" ]] || printf '  PodCliqueSet delays: %s\n' "${pcs_names}"

  ((failed == 0 && gated == 0 && dgd_not_ready == 0 && pcs_mismatched == 0))
}

wait_for_recovery() {
  local timeout_seconds deadline
  timeout_seconds="$(duration_seconds "${RECOVERY_TIMEOUT}")"
  deadline=$((SECONDS + timeout_seconds))
  echo "==> Waiting up to ${RECOVERY_TIMEOUT} for Grove to replace terminal pods and release scheduling gates"
  while ((SECONDS < deadline)); do
    if report_state; then
      return 0
    fi
    sleep "${POLL_SECONDS}"
  done
  echo "ERROR: scheduling recovery did not converge within ${RECOVERY_TIMEOUT}." >&2
  report_state || true
  return 1
}

for command in "${KUBECTL}" "${HELM}" "${CURL}" "${PYTHON}" realpath sort; do
  require_command "${command}"
done
if [[ ! -f "${PLATFORM_STORAGE_VALUES}" ]]; then
  echo "ERROR: Dynamo platform storage values not found: ${PLATFORM_STORAGE_VALUES}" >&2
  exit 2
fi
if [[ ! -f "${GROVE_RESOURCE_VALUES}" ]]; then
  echo "ERROR: Grove resource values not found: ${GROVE_RESOURCE_VALUES}" >&2
  exit 2
fi
for patch_file in "${GROVE_INIT_RESOURCE_PATCH}" "${PLATFORM_INIT_RESOURCE_PATCH}" "${LWS_RESOURCE_PATCH}"; do
  if [[ ! -f "${patch_file}" ]]; then
    echo "ERROR: controller resource patch not found: ${patch_file}" >&2
    exit 2
  fi
done

current_context="$("${KUBECTL}" config current-context)"
if [[ "${current_context}" != "${EXPECTED_CONTEXT}" ]]; then
  echo "ERROR: refusing to mutate context ${current_context}; expected ${EXPECTED_CONTEXT}." >&2
  exit 2
fi
"${KUBECTL}" version --request-timeout=20s >/dev/null
"${KUBECTL}" get namespace "${MODEL_NAMESPACE}" "${GROVE_NAMESPACE}" \
  "${DYNAMO_PLATFORM_NAMESPACE}" >/dev/null

installed_grove_version="$(
  release_chart_version "${GROVE_NAMESPACE}" "${GROVE_RELEASE}" "grove-charts-"
)"
platform_version="$(
  release_chart_version "${DYNAMO_PLATFORM_NAMESPACE}" "${DYNAMO_PLATFORM_RELEASE}" "dynamo-platform-"
)"

echo "==> Context guard passed: ${current_context}"
echo "==> Installed Grove ${installed_grove_version}; required minimum ${GROVE_MIN_VERSION}"
echo "==> Installed Dynamo platform ${platform_version}; required minimum ${DYNAMO_PLATFORM_MIN_VERSION}"
device_plugin_state="$(device_plugins_ready)"
echo "==> NVIDIA device plugin ready: ${device_plugin_state}"

if [[ "${MODE}" == "check" ]]; then
  if ! version_at_least "${installed_grove_version}" "${GROVE_MIN_VERSION}"; then
    echo "ERROR: Grove ${installed_grove_version} lacks the required gang-recovery fixes." >&2
    exit 1
  fi
  if ! version_at_least "${platform_version}" "${DYNAMO_PLATFORM_MIN_VERSION}"; then
    echo "ERROR: Dynamo platform ${platform_version} is incompatible with Grove ${GROVE_MIN_VERSION}; require ${DYNAMO_PLATFORM_MIN_VERSION} or newer." >&2
    exit 1
  fi
  "${KUBECTL}" rollout status deployment/grove-operator \
    --namespace "${GROVE_NAMESPACE}" --timeout=30s >/dev/null
  "${KUBECTL}" rollout status deployment/dynamo-platform-dynamo-operator-controller-manager \
    --namespace "${DYNAMO_PLATFORM_NAMESPACE}" --timeout=30s >/dev/null
  dedicated_workers="$(verify_dedicated_worker_tolerations)"
  if [[ -n "${dedicated_workers}" ]]; then
    dedicated_gpu_taints_ready >/dev/null
  else
    echo "==> Dedicated ${DEDICATED_GPU_PRODUCT} reservation is not used by the current workload"
  fi
  stale="$(stale_grove_pods)"
  if [[ -n "${stale}" ]]; then
    echo "ERROR: pod(s) still carry pre-upgrade Grove template hashes:" >&2
    printf '%s\n' "${stale}" >&2
    exit 1
  fi
  report_state
  exit
fi

target_grove_version="${GROVE_MIN_VERSION}"
if version_at_least "${installed_grove_version}" "${GROVE_MIN_VERSION}"; then
  target_grove_version="${installed_grove_version}"
fi
target_platform_version="${DYNAMO_PLATFORM_MIN_VERSION}"
if version_at_least "${platform_version}" "${DYNAMO_PLATFORM_MIN_VERSION}"; then
  target_platform_version="${platform_version}"
fi

prepare_grove_topology_crd_migration

temp_root="$(realpath -e "${TMPDIR:-/tmp}")"
temp_dir="$(mktemp -d "${temp_root}/daedalus-dynamo-scheduling.XXXXXX")"
case "${temp_dir}" in
  "${temp_root}"/daedalus-dynamo-scheduling.*) ;;
  *)
    echo "ERROR: unexpected temporary directory path: ${temp_dir}" >&2
    exit 1
    ;;
esac
platform_chart="${temp_dir}/dynamo-platform-${target_platform_version}.tgz"
cleanup() {
  if [[ -d "${temp_dir}" && ! -L "${temp_dir}" ]] &&
    [[ "${temp_dir}" == "${temp_root}"/daedalus-dynamo-scheduling.* ]]; then
    rm -rf -- "${temp_dir:?}"
  else
    echo "WARN: refusing to remove unexpected temporary path: ${temp_dir}" >&2
  fi
}
trap cleanup EXIT

echo "==> Fetching Dynamo platform chart ${target_platform_version}"
"${CURL}" --fail --silent --show-error --location \
  --output "${platform_chart}" \
  "${DYNAMO_PLATFORM_CHART_BASE_URL}/dynamo-platform-${target_platform_version}.tgz"
fetched_platform_version="$("${HELM}" show chart "${platform_chart}" | awk '$1 == "version:" {print $2; exit}')"
if [[ "${fetched_platform_version}" != "${target_platform_version}" ]]; then
  echo "ERROR: fetched platform chart version ${fetched_platform_version:-unknown}; expected ${target_platform_version}." >&2
  exit 1
fi

grove_args=(
  upgrade "${GROVE_RELEASE}" "${GROVE_CHART}"
  --namespace "${GROVE_NAMESPACE}"
  --version "${target_grove_version}"
  --reset-then-reuse-values
  --values "${GROVE_RESOURCE_VALUES}"
  --set crdInstaller.enabled=true
  --set priorityClass.enabled=true
  --set config.server.healthProbes.enable=true
  --history-max 10
)
platform_args=(
  upgrade "${DYNAMO_PLATFORM_RELEASE}" "${platform_chart}"
  --namespace "${DYNAMO_PLATFORM_NAMESPACE}"
  --reset-then-reuse-values
  --values "${PLATFORM_STORAGE_VALUES}"
  --set global.grove.enabled=true
  --set global.kai-scheduler.enabled=true
  --set global.nats.install=true
  --set-string "dynamo-operator.dynamo.groveTerminationDelay=${GROVE_TERMINATION_DELAY}"
  --history-max 10
)

echo "==> Server-side dry-run: Grove ${target_grove_version}"
"${HELM}" "${grove_args[@]}" --dry-run=server --hide-secret >/dev/null
echo "==> Server-side dry-run: Dynamo ${target_platform_version} with recovery delay ${GROVE_TERMINATION_DELAY}"
"${HELM}" "${platform_args[@]}" --dry-run=server --hide-secret >/dev/null

reserve_dedicated_gpu_nodes

echo "==> Upgrading Grove to ${target_grove_version} with CRD reconciliation, priority, and health probes"
"${HELM}" "${grove_args[@]}" \
  --atomic --cleanup-on-fail --wait --timeout "${HELM_TIMEOUT}"
apply_deployment_resource_patch "${GROVE_NAMESPACE}" grove-operator "${GROVE_INIT_RESOURCE_PATCH}"

echo "==> Upgrading Dynamo to ${target_platform_version} and setting Grove termination delay ${GROVE_TERMINATION_DELAY}"
"${HELM}" "${platform_args[@]}" \
  --atomic --cleanup-on-fail --wait --timeout "${HELM_TIMEOUT}"
apply_deployment_resource_patch "${DYNAMO_PLATFORM_NAMESPACE}" \
  dynamo-platform-dynamo-operator-controller-manager "${PLATFORM_INIT_RESOURCE_PATCH}"

# LWS can be installed separately from the platform chart. Preserve its local
# requests and architecture placement whenever this maintenance path runs.
lws_deployment="$("${KUBECTL}" --context "${EXPECTED_CONTEXT}" get deployment lws-controller-manager \
  --namespace "${LWS_NAMESPACE}" --ignore-not-found --output name)"
if [[ -n "${lws_deployment}" ]]; then
  apply_deployment_resource_patch "${LWS_NAMESPACE}" lws-controller-manager "${LWS_RESOURCE_PATCH}"
fi

recycle_stale_grove_pods
cleanup_terminal_admission_pods
wait_for_recovery
echo "==> Scheduling resilience is healthy"
