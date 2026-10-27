# Stack upgrade — October 27, 2026

Both active model deployments are ready on stable Dynamo 1.6.0. Existing LAN
addresses, fixed NodePorts, source ranges, TLS, API keys, model aliases, PVCs
and application routes were preserved. Downtime was authorized for this rollout.

## Versions

| Component | Installed / configured version |
| --- | --- |
| Dynamo platform and operator | [1.6.0](https://github.com/ai-dynamo/dynamo/releases/tag/v1.6.0) |
| Qwen frontend and worker | Dynamo 1.6.0, stock vLLM 0.31.0, CUDA 13 |
| Bolt frontend and worker | Dynamo 1.6.0, model-required vLLM `0.32.0rc1.dev100+g77154ca61`, b12x 1.5.1 |
| Planner | 1.6.0 image and chart default; CLI smoke passed; disabled in both active profiles |
| Grove | [0.1.0-alpha.17](https://github.com/ai-dynamo/grove/releases/tag/v0.1.0-alpha.17) |
| KAI Scheduler | [0.18.6](https://github.com/kai-scheduler/KAI-Scheduler/releases/tag/v0.18.6) |
| LeaderWorkerSet | [1.0.0](https://github.com/kubernetes-sigs/lws/releases/tag/v1.0.0) |
| agentgateway controller, CRDs and custom proxy | [1.6.0](https://github.com/agentgateway/agentgateway/releases/tag/v1.6.0) |
| Gateway API / inference extension | 1.6.3 standard / 1.6.2 |
| ModelExpress | [0.7.0](https://github.com/ai-dynamo/modelexpress/releases/tag/v0.7.0) |
| Redis | 8.10.3-alpine |
| NATS / config reloader | 2.15.2 / 0.25.0 |
| NVIDIA GPU Operator | [26.7.2](https://github.com/NVIDIA/gpu-operator/releases/tag/v26.7.2) |
| kube-prometheus-stack / Prometheus Operator | 92.14.0 / 0.94.2 |
| Active Prometheus | 3.15.1-distroless |
| AIPerf client image | 0.13.0; image built, pushed and version checked |

These were the latest compatible releases checked against official release
metadata during this upgrade. Dynamo's installation guidance requires at least
Grove alpha.15 and KAI 0.17 for this release; the selected versions pass the
packaged compatibility checks. [Dynamo installation guidance](https://docs.nvidia.com/dynamo/dev/kubernetes/installation/install-dynamo).

Kubernetes, Cilium, host GPU drivers, storage infrastructure and unrelated
applications were outside this inference-stack upgrade. GPU Operator continues
to use the existing host-installed drivers (`driver.enabled: false`). The
inactive Muse, Lightning and Qwen Flash Next recipes retain their specialized
preview engines; they were not started or qualified by this rollout.

## Compatibility changes

- Qwen retains its pinned Qwen3.6 FP8 checkpoint, offline cache, both model IDs,
  131,072-token limit and fixed 4 GiB BF16 KV pool. Its empty-argument tool
  wrapper was reviewed against XGrammar 0.2.7 and its exact source hash. The
  worker uses `Recreate` so replacement does not require a spare GPU.
- Bolt retains its checkpoint, Mamba checkpoint correction, disabled DFlash,
  compiled kernels, context limit and 48 GiB KV pool. Stable Dynamo required a
  guarded deferred import for an unused embedding API removed by the model's
  vLLM engine. Multiprocess embeddings are not supported by this Bolt image.
- ModelExpress 0.7 runs with the former UID for existing root-owned caches.
  Downloads explicitly select the existing revisions instead of following a
  changing model revision.
- Docker Hub had not published NATS 2.15.2. `Dockerfile.nats` installs the
  official 2.15.2 amd64 binary, verified against its published SHA-256, into the
  pinned 2.15.1 Alpine image. The NATS pod remains on amd64 `daedalus-03`.
- LWS retains its existing immutable Deployment selector and resource requests.
  Gateway API retains the standard channel and TCPRoute compatibility endpoint.
- Monitoring retains the active maintenance Prometheus CR and metrics PVC. Its
  intentionally unassigned LoadBalancer Services retain their fixed NodePorts;
  readiness is checked on workloads because Helm `--wait` cannot complete on
  those Services. The chart now upgrades its CRDs during replay.

## Validation

- All 72 inventoried stack pods were ready or successfully completed; both DGDs
  reported `Ready=True`. All five nodes remained Ready.
- Existing Service contracts compared equal across the cluster: no address,
  port, Service type, external traffic policy or source-range changes. Only the
  generated Qwen worker metrics Service changed its operator revision suffix.
- Non-node LAN source `192.168.1.246` passed authenticated, certificate-verified
  `/v1/models`, Chat Completions and Responses against both active models via
  gateway VIP `192.168.1.12`, retaining port/NodePort `32359`.
- Each model passed seven tool checks: automatic, required and named empty
  argument calls; Chat tool result round trip; streaming tool call; Responses
  tool call and tool result round trip.
- The Daedalus backend passed the same seven checks through the existing
  Switchyard Service and `daedalus/local` route, which currently selects Qwen3.6.
- Repository checks: 102 tests, 10 skipped, no failures; shell syntax checks
  passed. `make doctor`: 37 OK, 9 warnings, 0 failures. Warnings concern RDMA
  facilities absent from the current aggregated TCP deployment.
- Planner 1.6 CLI and AIPerf 0.13 CLI checks passed. Autoscaling was not enabled,
  and performance/long-context benchmarks were not rerun. Functional checks do
  not establish performance equivalence or qualify inactive model recipes.

The LAN test used an isolated macvlan network namespace, with a temporary
namespace-only destination-port translation to avoid host Cilium socket-LB
interception. It reached the actual gateway listener with source `.246`; no
cluster networking was changed. The temporary namespace and Planner test pod
were removed after validation.

## Replaying the pinned stack

The version pins are in [`config/stack-versions.env`](../config/stack-versions.env).
[`scripts/upgrade-stack.sh`](../scripts/upgrade-stack.sh) captures current
configuration and endpoints, validates required custom images, then upgrades
the operators, gateway, GPU/monitoring stack and both active model profiles.
It uses the Qwen offline-cache post-renderer and waits for DGD readiness.

```bash
bash scripts/upgrade-stack.sh
make doctor
```

The replay script was syntax checked and its component operations were executed
during this rollout; the assembled script was not rerun against the already
healthy cluster. A future replay still requires actual LAN and application API
acceptance checks. Build the custom images with `make gateway-image`,
`DOCKER_HOST=ssh://daedalus-06 make image values/nemotron35bolt-gb10.yaml`,
and `docker build -f Dockerfile.nats -t daedalus.ddns.me:5050/btuttle/daedalus-nats:2.15.2 .`
followed by pushing NATS. The already published Bolt frontend tag is retained.

## Evidence and rollback

Protected backup/evidence directory:
[`artifacts/stack-upgrade-20261027T102842Z`](../artifacts/stack-upgrade-20261027T102842Z/).
It includes original Helm values/manifests/revisions, CRDs, endpoints and
policies, repository snapshots, Redis snapshots, NATS data, image build logs,
final pod/image inventory and API test results. Keep its permissions private:
some saved manifests contain credentials.

See its [`upgrade-report.md`](../artifacts/stack-upgrade-20261027T102842Z/upgrade-report.md)
for exact evidence and rollback commands. Rollback artifacts have been rendered
and checked, but a live rollback was not performed. Do not blindly downgrade
Redis 8 data to Redis 7 or delete CRDs/PVCs. Use the serving-only rollback first
for a runtime regression; it retains the upgraded Redis data format and existing
client endpoints.
