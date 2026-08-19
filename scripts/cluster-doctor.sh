#!/usr/bin/env bash
set -euo pipefail

# Read-only cluster-readiness doctor for the Daedalus Dynamo demo.
#
# Checks, in order, whether a Kubernetes cluster can run these Dynamo backends
# well: API reachability, required CRDs + operators, GPU capacity and labels,
# the device plugin / DCGM, storage (default + RWX), the event-plane NATS
# service, the worker transport env, and the RDMA / InfiniBand layer (device
# plugin resources, Network Operator, and node-level probes via the vendored
# scripts/check_interconnect.py).
#
# Severity contract:
#   FAIL  hard blocker (missing CRDs/operators/GPUs/default StorageClass).
#   WARN  works for aggregated serving but may limit performance/disagg
#         (RDMA/InfiniBand absence is always a WARN, never a FAIL).
#   SKIP  could not evaluate (tool/pod absent); not counted in the verdict.
# Exit status is non-zero only when at least one check FAILs.
#
# Usage:  make doctor   |   scripts/cluster-doctor.sh   |   DOCTOR_JSON=1 scripts/cluster-doctor.sh

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

NAMESPACE="${NAMESPACE:-daedalus-dynamo}"
PREFLIGHT_TIMEOUT_SECONDS="${PREFLIGHT_TIMEOUT_SECONDS:-20}"
WORKER_REPLICAS="${WORKER_REPLICAS:-1}"
WORKER_TP="${WORKER_TP:-2}"
SERVING_MODE="${SERVING_MODE:-agg}"
TRANSPORT_PROFILE="${TRANSPORT_PROFILE:-tcp}"
MIN_GPU_MEMORY_MIB="${MIN_GPU_MEMORY_MIB:-40959}"
DOCTOR_ENV_BACKEND="${DOCTOR_ENV_BACKEND:-qwen35b}"
DOCTOR_PROBE_IMAGE="${DOCTOR_PROBE_IMAGE:-nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.1}"
DOCTOR_RUN_NODE_PROBE="${DOCTOR_RUN_NODE_PROBE:-auto}" # auto | job | off
DOCTOR_PROBE_NODE_SELECTOR="${DOCTOR_PROBE_NODE_SELECTOR:-nvidia.com/gpu.present=true}"
DOCTOR_PROBE_GPU="${DOCTOR_PROBE_GPU:-1}"
DOCTOR_PROBE_TIMEOUT="${DOCTOR_PROBE_TIMEOUT:-180}"
DOCTOR_JSON="${DOCTOR_JSON:-0}"
GATEWAY_NAME="${GATEWAY_NAME:-inference-gateway}"
AGENTGATEWAY_NAMESPACE="${AGENTGATEWAY_NAMESPACE:-agentgateway-system}"
MIN_GATEWAY_REPLICAS="${MIN_GATEWAY_REPLICAS:-1}"
MIN_NOFILE_LIMIT="${MIN_NOFILE_LIMIT:-131072}"
GATEWAY_FD_METRICS_PORT="${GATEWAY_FD_METRICS_PORT:-9090}"
GROVE_MIN_RECOVERY_VERSION="${GROVE_VERSION:-v0.1.0-alpha.12-rc1}"
GROVE_TERMINATION_DELAY="${GROVE_TERMINATION_DELAY:-15m}"

INTERCONNECT="${REPO_ROOT}/scripts/check_interconnect.py"
PROBE_MANIFEST="${REPO_ROOT}/scripts/cluster-doctor-probe-pod.yaml"
PROBE_POD_NAME="dynamo-cluster-doctor-probe"
PROBE_CREATED=0

# ----------------------------------------------------------------------------
# Verdict engine
# ----------------------------------------------------------------------------
declare -i OK_COUNT=0 WARN_COUNT=0 FAIL_COUNT=0
declare -a RESULTS=()

if [[ -t 1 && "${DOCTOR_JSON}" != "1" ]]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_FAIL=$'\033[31m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_FAIL=""; C_DIM=""; C_RST=""
fi

record() { # status name detail
  local status="$1" name="$2" detail="$3" tag
  RESULTS+=("${status}|${name}|${detail}")
  case "${status}" in
    ok) OK_COUNT+=1 ;;
    warn) WARN_COUNT+=1 ;;
    fail) FAIL_COUNT+=1 ;;
  esac
  [[ "${DOCTOR_JSON}" == "1" ]] && return 0
  case "${status}" in
    ok) tag="${C_OK}[ OK ]${C_RST}" ;;
    warn) tag="${C_WARN}[WARN]${C_RST}" ;;
    fail) tag="${C_FAIL}[FAIL]${C_RST}" ;;
    *) tag="${C_DIM}[SKIP]${C_RST}" ;;
  esac
  printf '%s %-24s %s\n' "${tag}" "${name}" "${detail}"
  return 0
}
ok() { record ok "$1" "$2"; }
warn() { record warn "$1" "$2"; }
fail() { record fail "$1" "$2"; }
skip() { record skip "$1" "$2"; }

section() {
  [[ "${DOCTOR_JSON}" == "1" ]] && return 0
  printf '\n%s== %s ==%s\n' "${C_DIM}" "$1" "${C_RST}"
  return 0
}

# kubectl that never aborts the script (for value capture)
kq() { kubectl "$@" 2>/dev/null || true; }

# All resources we probe are CRDs; query them by name directly. This is stable,
# unlike `kubectl api-resources`, which can return a partial list when an
# aggregated API on the cluster is briefly unavailable.
api_resource_exists() {
  kubectl get crd "$1" >/dev/null 2>&1
}

emit_json() {
  local tmp
  tmp="$(mktemp)"
  if ((${#RESULTS[@]})); then printf '%s\n' "${RESULTS[@]}" > "${tmp}"; fi
  OK_COUNT="${OK_COUNT}" WARN_COUNT="${WARN_COUNT}" FAIL_COUNT="${FAIL_COUNT}" \
    python3 - "${tmp}" <<'PY'
import json, os, sys
checks = []
with open(sys.argv[1]) as f:
    for line in f:
        line = line.rstrip("\n")
        if not line:
            continue
        st, nm, dt = line.split("|", 2)
        checks.append({"name": nm, "status": st, "detail": dt})
fail = int(os.environ["FAIL_COUNT"])
warn = int(os.environ["WARN_COUNT"])
verdict = ("not ready: resolve failed checks" if fail
           else "ready for aggregated serving" + ("; some warnings" if warn else ""))
print(json.dumps({
    "checks": checks,
    "counts": {"ok": int(os.environ["OK_COUNT"]), "warn": warn, "fail": fail},
    "verdict": verdict,
}, indent=2))
PY
  rm -f "${tmp}"
  return 0
}

print_verdict() {
  local rdma="not confirmed by read-only checks"
  if ((${#RESULTS[@]})) && printf '%s\n' "${RESULTS[@]}" | grep -Fq 'ok|rdma:ib-link'; then
    rdma="present (InfiniBand link Active)"
  fi
  printf '\n%sSummary:%s %d ok, %d warn, %d fail\n' "${C_DIM}" "${C_RST}" \
    "${OK_COUNT}" "${WARN_COUNT}" "${FAIL_COUNT}"
  if ((FAIL_COUNT > 0)); then
    printf '%sVerdict:%s NOT READY — resolve the [FAIL] item(s) above before deploying.\n' "${C_FAIL}" "${C_RST}"
  else
    printf '%sVerdict:%s ready for aggregated serving (SERVING_MODE=agg, TRANSPORT_PROFILE=tcp).\n' "${C_OK}" "${C_RST}"
    printf '         Disaggregated serving / TRANSPORT_PROFILE=rdma: RDMA/InfiniBand %s.\n' "${rdma}"
  fi
  return 0
}

finish() {
  if [[ "${DOCTOR_JSON}" == "1" ]]; then emit_json; else print_verdict; fi
  ((FAIL_COUNT > 0)) && exit 1
  exit 0
}

cleanup_probe() {
  if [[ "${PROBE_CREATED}" == "1" ]]; then
    kubectl delete pod "${PROBE_POD_NAME}" -n "${NAMESPACE}" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
}
trap cleanup_probe EXIT

# ----------------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------------
check_kubectl() {
  section "Kubernetes API"
  local ctx
  ctx="$(kq config current-context)"
  if kubectl version --request-timeout="${PREFLIGHT_TIMEOUT_SECONDS}s" >/dev/null 2>&1; then
    ok "kubectl" "reachable; context: ${ctx:-unknown}"
    return 0
  fi
  fail "kubectl" "cannot reach the cluster API (context: ${ctx:-none}). Set KUBECONFIG / kube-context."
  finish
}

check_crds() {
  section "CRDs and operators"
  local crd label
  for crd in \
    dynamographdeployments.nvidia.com \
    dynamographdeploymentscalingadapters.nvidia.com \
    podcliquesets.grove.io \
    gateways.gateway.networking.k8s.io \
    httproutes.gateway.networking.k8s.io \
    inferencepools.inference.networking.k8s.io \
    agentgatewaypolicies.agentgateway.dev; do
    label="crd:${crd%%.*}"
    if api_resource_exists "${crd}"; then
      ok "${label}" "installed"
    else
      fail "${label}" "missing (${crd}). Install operators or run: make install-gateway"
    fi
  done
  return 0
}

operator_ready() { # namespace deployment
  if kubectl get deployment "$2" -n "$1" >/dev/null 2>&1 &&
    kubectl rollout status "deployment/$2" -n "$1" --timeout="${PREFLIGHT_TIMEOUT_SECONDS}s" >/dev/null 2>&1; then
    ok "operator:${2%%-*}" "available in ${1}"
  else
    fail "operator:${2%%-*}" "deployment ${2} not available in namespace ${1}."
  fi
  return 0
}

check_operators() {
  operator_ready "dynamo-system" "dynamo-platform-dynamo-operator-controller-manager"
  operator_ready "grove-system" "grove-operator"
  return 0
}

check_gpus() {
  section "GPU capacity"
  local needed
  needed=$((WORKER_TP * WORKER_REPLICAS))
  [[ "${SERVING_MODE}" == "disagg" ]] && needed=$((needed * 2))

  local total=0 gpu_nodes=0 highmem=0 mem_label=0 line name gpu mem
  while IFS=$'\t' read -r name gpu mem; do
    [[ -z "${name}" ]] && continue
    [[ "${gpu}" =~ ^[0-9]+$ ]] || gpu=0
    if ((gpu > 0)); then
      gpu_nodes=$((gpu_nodes + 1))
      total=$((total + gpu))
    fi
    if [[ -n "${mem}" ]]; then
      mem_label=1
      if [[ "${mem}" =~ ^[0-9]+$ ]] && ((mem > MIN_GPU_MEMORY_MIB)); then
        highmem=$((highmem + 1))
      fi
    fi
  done < <(kq get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.nvidia\.com/gpu}{"\t"}{.metadata.labels.nvidia\.com/gpu\.memory}{"\n"}{end}')

  if ((total == 0)); then
    fail "gpu:capacity" "no allocatable nvidia.com/gpu on any node. Install the NVIDIA GPU Operator / device plugin."
    return 0
  fi
  ok "gpu:capacity" "${total} GPU(s) across ${gpu_nodes} node(s)"

  local mode_note=""
  [[ "${SERVING_MODE}" == "disagg" ]] && mode_note=" x2 for prefill+decode"
  if ((total < needed)); then
    warn "gpu:per-backend" "one backend needs ${needed} GPU(s) (TP ${WORKER_TP} x replicas ${WORKER_REPLICAS}${mode_note}); cluster has ${total}."
  else
    ok "gpu:per-backend" "enough for one backend (${needed} GPU[s] needed)"
  fi

  if ((gpu_nodes < WORKER_REPLICAS)); then
    warn "gpu:spread" "worker pod anti-affinity needs >= ${WORKER_REPLICAS} GPU node(s); found ${gpu_nodes}."
  else
    ok "gpu:spread" "${gpu_nodes} GPU node(s) for anti-affinity (need >= ${WORKER_REPLICAS})"
  fi

  if ((mem_label == 0)); then
    warn "gpu:memory-label" "nodes lack nvidia.com/gpu.memory labels (GPU Feature Discovery). Worker node-affinity requires > ${MIN_GPU_MEMORY_MIB} MiB."
  elif ((highmem == 0)); then
    fail "gpu:memory-label" "no node has nvidia.com/gpu.memory > ${MIN_GPU_MEMORY_MIB} MiB; workers will be unschedulable."
  else
    ok "gpu:memory-label" "${highmem} node(s) with > ${MIN_GPU_MEMORY_MIB} MiB GPUs"
  fi
  return 0
}

check_device_plugin() {
  if kq get ds -A -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep -qiE 'nvidia.*device.?plugin'; then
    ok "gpu:device-plugin" "NVIDIA device plugin DaemonSet present"
  else
    warn "gpu:device-plugin" "no nvidia-device-plugin DaemonSet found (GPUs may still be served by another plugin)."
  fi
  if kq get ds,deploy -A -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep -qi 'dcgm-exporter'; then
    ok "gpu:dcgm" "DCGM exporter present (Grafana GPU panels)"
  else
    warn "gpu:dcgm" "no dcgm-exporter found; the Grafana GPU panels will be empty (see observability/README.md)."
  fi
  return 0
}

check_storage() {
  section "Storage"
  local default_sc rwx_provisioner
  default_sc="$(kq get sc -o jsonpath='{range .items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")]}{.metadata.name}{" "}{end}')"
  default_sc="${default_sc%"${default_sc##*[![:space:]]}"}"
  if [[ -n "${default_sc// /}" ]]; then
    ok "storage:default-sc" "default StorageClass: ${default_sc}"
  else
    fail "storage:default-sc" "no default StorageClass; PVCs (model-cache, redis) will stay Pending. Mark one default or set storageClassName."
  fi

  rwx_provisioner="$(kq get sc -o jsonpath='{range .items[*]}{.provisioner}{"\n"}{end}' | grep -iE 'nfs|cephfs|azurefile|efs|filestore|smb|juicefs|glusterfs|portworx' | head -1 || true)"
  if [[ -n "${rwx_provisioner}" ]]; then
    ok "storage:rwx" "RWX-capable provisioner detected: ${rwx_provisioner}"
  elif kq get pvc -A -o jsonpath='{range .items[?(@.status.phase=="Bound")]}{.spec.accessModes[*]}{"\n"}{end}' | grep -q 'ReadWriteMany'; then
    ok "storage:rwx" "a Bound ReadWriteMany PVC exists in the cluster"
  else
    warn "storage:rwx" "no RWX-capable StorageClass detected. model-cache is 100Gi ReadWriteMany; provide an RWX class or set MODEL_CACHE_STORAGE."
  fi
  return 0
}

check_nats() {
  section "Event plane"
  if kubectl get svc dynamo-platform-nats -n dynamo-system >/dev/null 2>&1; then
    ok "nats" "dynamo-platform-nats service present"
  else
    warn "nats" "no dynamo-platform-nats service. The frontend uses DYN_EVENT_PLANE=zmq (no NATS dependency); only NATS-based event flows are affected."
  fi
  return 0
}

check_workload_recovery() {
  section "Dynamo workload recovery"
  if ! kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1; then
    skip "recovery:namespace" "namespace ${NAMESPACE} does not exist"
    return 0
  fi
  if ! api_resource_exists dynamographdeployments.nvidia.com; then
    skip "recovery:dgd-ready" "DynamoGraphDeployment CRD is not installed"
    return 0
  fi

  local failed_pods gated_pods not_ready grove_chart grove_version delays
  if command -v helm >/dev/null 2>&1; then
    grove_chart="$(
      helm list -n grove-system -o json 2>/dev/null |
        python3 -c 'import json,sys; print(next((x.get("chart", "") for x in json.load(sys.stdin) if x.get("name") == "grove"), ""))' \
        2>/dev/null || true
    )"
    grove_version="${grove_chart#grove-charts-}"
    if [[ -n "${grove_chart}" ]] &&
      [[ "$(printf '%s\n%s\n' "${GROVE_MIN_RECOVERY_VERSION#v}" "${grove_version#v}" | sort -V | head -1)" == "${GROVE_MIN_RECOVERY_VERSION#v}" ]]; then
      ok "recovery:grove-version" "${grove_version} includes gang recovery"
    else
      warn "recovery:grove-version" "${grove_version:-unknown} is older than ${GROVE_MIN_RECOVERY_VERSION}; run make scheduling-resilience"
    fi
  else
    skip "recovery:grove-version" "helm is unavailable; cannot verify the Grove release"
  fi

  delays="$(
    kq get podcliquesets.grove.io -n "${NAMESPACE}" \
      -o jsonpath='{range .items[*]}{.metadata.name}{":"}{.spec.template.terminationDelay}{" "}{end}'
  )"
  if [[ -z "${delays// /}" ]]; then
    skip "recovery:termination-delay" "no PodCliqueSets exist in ${NAMESPACE}"
  elif grep -Eq ':(4h|4h0m0s)( |$)' <<<"${delays}"; then
    warn "recovery:termination-delay" "four-hour recovery gate is active (${delays}); run make scheduling-resilience"
  else
    ok "recovery:termination-delay" "PodCliqueSet recovery delays: ${delays}"
  fi

  failed_pods="$(
    kq get pods \
      -n "${NAMESPACE}" \
      -l 'app.kubernetes.io/managed-by=grove-operator,nvidia.com/dynamo-graph-deployment-name' \
      --field-selector=status.phase=Failed \
      -o jsonpath='{range .items[*]}{.metadata.name}{":"}{.status.reason}{" "}{end}'
  )"
  if [[ -n "${failed_pods// /}" ]]; then
    warn "recovery:failed-pods" "failed Grove/Dynamo pod(s): ${failed_pods}. Run make scheduling-resilience."
  else
    ok "recovery:failed-pods" "no failed Grove/Dynamo pods"
  fi

  gated_pods="$(
    kq get pods -n "${NAMESPACE}" \
      -l 'app.kubernetes.io/managed-by=grove-operator,nvidia.com/dynamo-graph-deployment-name' \
      -o json |
      python3 -c '
import json
import sys

for pod in json.load(sys.stdin)["items"]:
    if any(
        condition.get("type") == "PodScheduled"
        and condition.get("status") == "False"
        and condition.get("reason") == "SchedulingGated"
        for condition in pod.get("status", {}).get("conditions", [])
    ):
        print(pod["metadata"]["name"], end=" ")
'
  )"
  if [[ -n "${gated_pods// /}" ]]; then
    warn "recovery:gated-pods" "SchedulingGated Grove pod(s): ${gated_pods}. Run make scheduling-resilience."
  else
    ok "recovery:gated-pods" "no SchedulingGated Grove/Dynamo pods"
  fi

  not_ready="$(
    kq get dynamographdeployment -n "${NAMESPACE}" --no-headers |
      awk '$2 != "True" {print $1 "=" $2}' |
      tr '\n' ' '
  )"
  if [[ -n "${not_ready// /}" ]]; then
    warn "recovery:dgd-ready" "not-ready DGD(s): ${not_ready}. Inspect the DGD and redeploy the affected model with make deploy-<model>."
  else
    ok "recovery:dgd-ready" "all live DGDs report Ready=True"
  fi
  return 0
}

check_process_nofile_limits() { # label selector description
  local label_selector="$1" description="$2" pods pod limit found=0
  pods="$(
    kq get pods -n "${NAMESPACE}" -l "${label_selector}" \
      -o jsonpath='{range .items[?(@.status.phase=="Running")]}{.metadata.name}{"\n"}{end}'
  )"
  if [[ -z "${pods}" ]]; then
    skip "fd:${description}" "no running ${description} pod"
    return 0
  fi
  while IFS= read -r pod; do
    [[ -z "${pod}" ]] && continue
    found=1
    limit="$(
      kubectl exec -n "${NAMESPACE}" "${pod}" -- \
        sh -c 'grep "^Max open files" /proc/1/limits | tr -s " " | cut -d " " -f 4' \
        2>/dev/null || true
    )"
    if [[ ! "${limit}" =~ ^[0-9]+$ ]]; then
      fail "fd:${description}" "could not read RLIMIT_NOFILE from ${pod}"
    elif ((limit < MIN_NOFILE_LIMIT)); then
      fail "fd:${description}" "${pod} soft limit is ${limit}; require >= ${MIN_NOFILE_LIMIT}"
    else
      ok "fd:${description}" "${pod} soft limit ${limit}"
    fi
  done <<<"${pods}"
  ((found == 1)) || skip "fd:${description}" "no running ${description} pod"
  return 0
}

check_gateway_resilience() {
  section "Inference gateway resilience"
  local controller_desired controller_available controller_image gateway_desired gateway_available
  local gateway_images controller_tag gateway_tag hpa_min hpa_max pdb_min tcproute_served
  local pods pod raw open_fds soft_limit utilization

  if ! kubectl get deployment agentgateway -n "${AGENTGATEWAY_NAMESPACE}" >/dev/null 2>&1; then
    skip "gateway:controller" "agentgateway controller is not installed; run make install-gateway"
  else
    controller_desired="$(kq get deployment agentgateway -n "${AGENTGATEWAY_NAMESPACE}" -o jsonpath='{.spec.replicas}')"
    controller_available="$(kq get deployment agentgateway -n "${AGENTGATEWAY_NAMESPACE}" -o jsonpath='{.status.availableReplicas}')"
    controller_available="${controller_available:-0}"
    if ((controller_available < MIN_GATEWAY_REPLICAS)); then
      fail "gateway:controller" "${controller_available}/${controller_desired:-0} controller replicas available; require >= ${MIN_GATEWAY_REPLICAS}"
    else
      ok "gateway:controller" "${controller_available}/${controller_desired} controller replicas available"
    fi
  fi

  tcproute_served="$(
    kq get crd tcproutes.gateway.networking.k8s.io \
      -o jsonpath='{.spec.versions[?(@.name=="v1alpha2")].served}'
  )"
  if [[ "${tcproute_served}" == "true" ]]; then
    ok "gateway:api-compat" "TCPRoute v1alpha2 is served for the agentgateway controller"
  else
    fail "gateway:api-compat" "TCPRoute v1alpha2 is not served; agentgateway v1.3.1 cannot finish informer sync"
  fi

  if ! kubectl get deployment "${GATEWAY_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    skip "gateway:deployment" "${GATEWAY_NAME} is not deployed in ${NAMESPACE}"
    return 0
  fi

  gateway_desired="$(kq get deployment "${GATEWAY_NAME}" -n "${NAMESPACE}" -o jsonpath='{.spec.replicas}')"
  gateway_available="$(kq get deployment "${GATEWAY_NAME}" -n "${NAMESPACE}" -o jsonpath='{.status.availableReplicas}')"
  gateway_available="${gateway_available:-0}"
  if ((gateway_available < MIN_GATEWAY_REPLICAS)); then
    fail "gateway:deployment" "${gateway_available}/${gateway_desired:-0} proxy replicas available; require >= ${MIN_GATEWAY_REPLICAS}"
  else
    ok "gateway:deployment" "${gateway_available}/${gateway_desired} proxy replicas available"
  fi

  controller_image="$(kq get deployment agentgateway -n "${AGENTGATEWAY_NAMESPACE}" -o jsonpath='{.spec.template.spec.containers[?(@.name=="controller")].image}')"
  gateway_images="$(kq get deployment "${GATEWAY_NAME}" -n "${NAMESPACE}" -o jsonpath='{.spec.template.spec.containers[?(@.name=="agentgateway")].image}')"
  controller_tag="${controller_image##*:}"
  gateway_tag="${gateway_images##*:}"
  if [[ -n "${controller_image}" && -n "${gateway_images}" && "${controller_tag}" == "${gateway_tag}" ]]; then
    ok "gateway:version" "controller and proxy use ${controller_tag}"
  else
    fail "gateway:version" "controller ${controller_image:-missing} and proxy ${gateway_images:-missing} are version-skewed"
  fi

  if kubectl get hpa "${GATEWAY_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    hpa_min="$(kq get hpa "${GATEWAY_NAME}" -n "${NAMESPACE}" -o jsonpath='{.spec.minReplicas}')"
    hpa_max="$(kq get hpa "${GATEWAY_NAME}" -n "${NAMESPACE}" -o jsonpath='{.spec.maxReplicas}')"
    if ((hpa_min < MIN_GATEWAY_REPLICAS || hpa_max <= hpa_min)); then
      fail "gateway:autoscaling" "HPA range ${hpa_min:-0}-${hpa_max:-0} does not preserve HA and scale-out headroom"
    else
      ok "gateway:autoscaling" "HPA range ${hpa_min}-${hpa_max}"
    fi
  else
    fail "gateway:autoscaling" "no HPA targets deployment/${GATEWAY_NAME}"
  fi

  if kubectl get pdb "${GATEWAY_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    pdb_min="$(kq get pdb "${GATEWAY_NAME}" -n "${NAMESPACE}" -o jsonpath='{.spec.minAvailable}')"
    ok "gateway:pdb" "PodDisruptionBudget minAvailable=${pdb_min:-configured}"
  else
    fail "gateway:pdb" "no PodDisruptionBudget protects deployment/${GATEWAY_NAME}"
  fi

  pods="$(
    kq get pods -n "${NAMESPACE}" \
      -l "gateway.networking.k8s.io/gateway-name=${GATEWAY_NAME}" \
      -o jsonpath='{range .items[?(@.status.phase=="Running")]}{.metadata.name}{"\n"}{end}'
  )"
  while IFS= read -r pod; do
    [[ -z "${pod}" ]] && continue
    raw="$(
      kubectl get --raw \
        "/api/v1/namespaces/${NAMESPACE}/pods/${pod}:${GATEWAY_FD_METRICS_PORT}/proxy/metrics" \
        2>/dev/null || true
    )"
    open_fds="$(printf '%s\n' "${raw}" | awk '/^daedalus_gateway_process_open_fds[{ ]/ {print $NF; exit}')"
    soft_limit="$(printf '%s\n' "${raw}" | awk '/^daedalus_gateway_process_soft_limit_fds[{ ]/ {print $NF; exit}')"
    if [[ ! "${open_fds}" =~ ^[0-9]+$ || ! "${soft_limit}" =~ ^[0-9]+$ ]]; then
      fail "gateway:fd-metrics" "FD metrics unavailable from ${pod}"
      continue
    fi
    if ((soft_limit < MIN_NOFILE_LIMIT)); then
      fail "gateway:fd-limit" "${pod} soft limit ${soft_limit}; require >= ${MIN_NOFILE_LIMIT}"
      continue
    fi
    utilization="$(awk -v open="${open_fds}" -v limit="${soft_limit}" 'BEGIN {printf "%.1f", (open / limit) * 100}')"
    if ((open_fds * 10 >= soft_limit * 9)); then
      fail "gateway:fd-headroom" "${pod} uses ${open_fds}/${soft_limit} FDs (${utilization}%)"
    elif ((open_fds * 10 >= soft_limit * 8)); then
      warn "gateway:fd-headroom" "${pod} uses ${open_fds}/${soft_limit} FDs (${utilization}%)"
    else
      ok "gateway:fd-headroom" "${pod} uses ${open_fds}/${soft_limit} FDs (${utilization}%)"
    fi
  done <<<"${pods}"

  check_process_nofile_limits \
    'nvidia.com/dynamo-component-type=frontend,nvidia.com/dynamo-component=Frontend' \
    frontend
  check_process_nofile_limits 'nvidia.com/dynamo-component-type=decode' worker
  return 0
}

check_transport_env() {
  section "Worker transport (NIXL/UCX/NCCL)"
  command -v python3 >/dev/null 2>&1 || { skip "transport:env" "python3 not available"; return 0; }
  [[ -f "${INTERCONNECT}" ]] || { skip "transport:env" "scripts/check_interconnect.py not found"; return 0; }
  local tmp out verdict
  tmp="$(mktemp)"
  if make --no-print-directory -C "${REPO_ROOT}" render MODEL="${DOCTOR_ENV_BACKEND}" >"${tmp}" 2>/dev/null; then
    out="$(python3 "${INTERCONNECT}" env "${tmp}" 2>/dev/null || true)"
    verdict="$(printf '%s' "${out}" | sed -n 's/.*"verdict": *"\(.*\)".*/\1/p' | head -1)"
    if printf '%s' "${verdict}" | grep -q 'no transport gaps'; then
      ok "transport:env" "rendered ${DOCTOR_ENV_BACKEND} manifest: ${verdict}"
    elif [[ -n "${verdict}" ]]; then
      warn "transport:env" "${verdict}. Expected for TRANSPORT_PROFILE=tcp; set TRANSPORT_PROFILE=rdma for disaggregated serving."
    else
      skip "transport:env" "could not evaluate transport env from the rendered manifest"
    fi
  else
    skip "transport:env" "could not render ${DOCTOR_ENV_BACKEND} for the env scan"
  fi
  rm -f "${tmp}"
  return 0
}

check_rdma_resources() {
  section "RDMA / InfiniBand"
  local res
  res="$(kq get nodes -o json | grep -oE '"(rdma/[^"]+|nvidia\.com/rdma[^"]*)"' | tr -d '"' | sort -u | tr '\n' ' ' || true)"
  if [[ -n "${res// /}" ]]; then
    ok "rdma:resources" "node RDMA device-plugin resources: ${res}"
  else
    warn "rdma:resources" "no rdma/* device-plugin resources on nodes. For RDMA via a device plugin install the NVIDIA Network Operator (RDMA can also work via hostNetwork/shared HCA)."
  fi
  return 0
}

check_network_operator() {
  if api_resource_exists nicclusterpolicies.mellanox.com; then
    if [[ "$(kq get nicclusterpolicy -o name | wc -l | tr -d ' ')" != "0" ]]; then
      ok "rdma:network-operator" "NicClusterPolicy present"
    else
      warn "rdma:network-operator" "Network Operator CRD installed but no NicClusterPolicy applied."
    fi
  elif kubectl get ns nvidia-network-operator >/dev/null 2>&1; then
    ok "rdma:network-operator" "nvidia-network-operator namespace present"
  else
    warn "rdma:network-operator" "NVIDIA Network Operator not detected (only needed for RDMA device-plugin / RoCE setup)."
  fi
  return 0
}

record_interconnect_checks() { # json
  local tmp
  tmp="$(mktemp)"
  printf '%s' "$1" > "${tmp}"
  local st nm dt
  while IFS='|' read -r st nm dt; do
    [[ -z "${st}" ]] && continue
    record "${st}" "${nm}" "${dt}"
  done < <(python3 - "${tmp}" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"warn|rdma:probe|could not parse probe output: {e}")
    sys.exit(0)
# RDMA gaps are advisory for this demo, so never let a probe hard-fail.
remap = {"skipped": "skip", "unknown": "warn", "fail": "warn"}
for c in d.get("checks", []):
    st = remap.get(c.get("status", "warn"), c.get("status", "warn"))
    nm = c.get("name", "probe")
    dt = (c.get("detail", "") or "").replace("\n", " ")[:160]
    print(f"{st}|rdma:{nm}|{dt}")
PY
  )
  rm -f "${tmp}"
  return 0
}

run_probe_pod() {
  local key="${DOCTOR_PROBE_NODE_SELECTOR%%=*}" val="${DOCTOR_PROBE_NODE_SELECTOR#*=}"
  [[ -f "${PROBE_MANIFEST}" ]] || { warn "rdma:node" "probe manifest ${PROBE_MANIFEST##*/} not found"; return 0; }
  PROBE_CREATED=1
  if ! sed \
    -e "s|__NAMESPACE__|${NAMESPACE}|g" \
    -e "s|__PROBE_IMAGE__|${DOCTOR_PROBE_IMAGE}|g" \
    -e "s|__NODE_SELECTOR_KEY__|${key}|g" \
    -e "s|__NODE_SELECTOR_VAL__|${val}|g" \
    -e "s|__SLEEP__|600|g" \
    -e "s|__GPU__|${DOCTOR_PROBE_GPU}|g" \
    "${PROBE_MANIFEST}" | kubectl apply -f - >/dev/null 2>&1; then
    warn "rdma:node" "failed to create the probe pod"
    return 0
  fi
  if kubectl wait --for=condition=Ready "pod/${PROBE_POD_NAME}" -n "${NAMESPACE}" --timeout="${DOCTOR_PROBE_TIMEOUT}s" >/dev/null 2>&1; then
    record_interconnect_checks "$(python3 "${INTERCONNECT}" node --namespace "${NAMESPACE}" --pod "${PROBE_POD_NAME}" --container probe 2>/dev/null || true)"
  else
    warn "rdma:node" "probe pod not Ready within ${DOCTOR_PROBE_TIMEOUT}s (no free GPU on a matching node?). Try DOCTOR_RUN_NODE_PROBE=auto against a running worker."
  fi
  return 0
}

check_rdma_node() {
  [[ "${DOCTOR_RUN_NODE_PROBE}" == "off" ]] && { skip "rdma:node" "node probes disabled (DOCTOR_RUN_NODE_PROBE=off)"; return 0; }
  command -v python3 >/dev/null 2>&1 || { skip "rdma:node" "python3 not available for the node probe"; return 0; }
  [[ -f "${INTERCONNECT}" ]] || { skip "rdma:node" "scripts/check_interconnect.py not found"; return 0; }

  local pod cname
  pod="$(kq get pods -n "${NAMESPACE}" -l nvidia.com/dynamo-component -o jsonpath='{range .items[?(@.status.phase=="Running")]}{.metadata.name}{"\n"}{end}' | head -1)"
  if [[ -n "${pod}" ]]; then
    cname="$(kq get pod "${pod}" -n "${NAMESPACE}" -o jsonpath='{.spec.containers[0].name}')"
    record_interconnect_checks "$(python3 "${INTERCONNECT}" node --namespace "${NAMESPACE}" --pod "${pod}" ${cname:+--container "${cname}"} 2>/dev/null || true)"
    return 0
  fi

  if [[ "${DOCTOR_RUN_NODE_PROBE}" == "job" ]]; then
    run_probe_pod
  else
    skip "rdma:node" "no running Dynamo worker to exec. Run 'DOCTOR_RUN_NODE_PROBE=job ${0##*/}' to launch a short-lived GPU-node probe pod, or run after deploying."
  fi
  return 0
}

main() {
  if [[ "${DOCTOR_JSON}" != "1" ]]; then
    printf '%s== Dynamo cluster doctor ==%s  namespace=%s  serving=%s  transport=%s\n' \
      "${C_DIM}" "${C_RST}" "${NAMESPACE}" "${SERVING_MODE}" "${TRANSPORT_PROFILE}"
  fi
  check_kubectl
  check_crds
  check_operators
  check_gpus
  check_device_plugin
  check_storage
  check_nats
  check_workload_recovery
  check_gateway_resilience
  check_transport_env
  check_rdma_resources
  check_network_operator
  check_rdma_node
  finish
}

main "$@"
