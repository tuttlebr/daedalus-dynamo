# Inference Gateway AIPerf Runner

This container discovers every model from the inference gateway's `/v1/models`
catalog and profiles each model in catalog order with NVIDIA AIPerf 0.13.0.
It does not hard-code the model list.

The frozen workload models an agentic conversation with four prefix layers:

1. A 1,024-token system/tool prefix shared by every session.
2. A 512-token repository prefix shared within one of four groups.
3. A 1,024-token context unique to each session.
4. Five growing conversation turns, adding 3,072 fresh tokens per continuation.
   AIPerf appends every live assistant response
   before sending the next user turn.

The default run replays 128 fresh sessions with a concurrency cap of three and
exactly 640 requests per model. Session starts are evenly spaced across 1,800
seconds, with the final session starting at the 30-minute boundary. Continuation
delays are preserved, and all queued requests and remaining turns drain afterward,
so a successful profile lasts at least 30 minutes per model. The runner checks
AIPerf's measured duration before marking a timed profile successful.

The five-turn shape targets about 70% reusable prompt tokens: 2,560 initial
tokens, 3,072 new tokens per continuation, and 128 generated tokens per turn.
The longest default prompt is approximately 15,360 tokens before chat-template
overhead. This models reusable prefixes, not a guaranteed server KV-cache hit
rate. `workload-plan.json` records the token-weighted estimate, including cold
first occurrences of shared prefixes and the assumptions about retained history.
Sessions are not recycled to fill the duration, which would inflate reuse.

Every session sends `X-Dynamo-Session-ID`, and its HTTP connection is
reused across turns. The generator uses AIPerf 0.13.0's fixed 512-token
Mooncake block size so the generated hash IDs and trace loader reconstruct the
same shared prompt blocks. AIPerf 0.13.0 rejects block-size overrides for file
datasets, so the runner fails early if a custom agentic config uses any other
block size.

## Run as a Kubernetes Job

From the repository root, inspect the exact Job without changing the cluster
(kubectl reads the gateway Service's current ClusterIP):

```bash
make benchmark-agentic-render
```

Run the all-model evaluation in the configured `NAMESPACE`:

```bash
make benchmark-agentic
```

The Make target checks `EXPECTED_CONTEXT` and the in-cluster gateway Service
before building or sending traffic. It builds and pushes the pinned client
image and creates a Job with `backoffLimit: 0`. The Job uses
`https://daedalus.ddns.me:32359`, a pod-local hostname mapping directly to
the gateway Service's ClusterIP, and verified TLS. Both discovery and generation
stay inside the cluster; they do not traverse the public endpoint or LAN VIP.
The URL retains the hostname because the existing certificate only covers
`daedalus.ddns.me`, not the Service FQDN. It loads `OPENAI_API_KEY` from Secret
`inference-gateway-api-keys`, key `switchyard`, for model discovery and
generation. The default `AIPERF_CONNECT_IP=auto` looks up `GATEWAY_NAME` in
`NAMESPACE` on each render or run and fails if that lookup fails. Set an explicit
local IP to change the mapping, or set it empty to opt into normal DNS.
Custom endpoint hostnames use their own DNS unless an explicit IP is supplied.
The Job uses ephemeral storage. When the benchmark exits, its
container stays running while the target copies the run back to:

```text
artifacts/aiperf/<run-id>/
├── workload/       # AIPerf dataset, manifest, per-model records, and summary
└── kubernetes/     # submitted/runtime objects, logs, checksums, orchestration summary
```

The Job is deleted only after `workload/run-manifest.json` is local. A failed
benchmark still copies its available evidence and returns a nonzero Make exit.
After copying, the target releases the container with its benchmark exit code.
If copying fails, the Job stays available for manual recovery until its active
deadline. A deadline, eviction, or node loss can destroy the ephemeral evidence.
Set
`AIPERF_KEEP_JOB=1` to retain it after a successful copy.

By default, `make benchmark-agentic` builds and pushes `AIPERF_IMAGE`. Reuse an
already published image with `AIPERF_SKIP_BUILD=1 AIPERF_SKIP_PUSH=1`. The
default credential reference renders no secret value. To use a different
Secret in the Job's namespace:

```bash
AIPERF_API_KEY_SECRET=gateway-benchmark-key \
  AIPERF_API_KEY_SECRET_KEY=OPENAI_API_KEY \
  make benchmark-agentic
```

Set `AIPERF_API_KEY_SECRET_KEY` to the key stored in that Secret. Useful bounded-workload overrides
include `AIPERF_TURNS`, `AIPERF_DATASET_SESSIONS`, `AIPERF_CONCURRENCY`,
`AIPERF_BENCHMARK_DURATION_SECONDS` (default 1800),
`AIPERF_MODEL_TIMEOUT_SECONDS`, and `AIPERF_ACTIVE_DEADLINE_SECONDS`.
The per-model timeout defaults to 7200 seconds; the whole-Job deadline remains
14400 seconds. Increase the Job deadline for large model catalogs. Both ceilings
must exceed the session-start window. Set `AIPERF_BENCHMARK_DURATION_SECONDS=0`
for an immediate, finite smoke run; timed runs need at least two sessions.
`AIPERF_MODELS` remains available for a deliberate focused run; leave it empty
to evaluate every model returned by `/v1/models`.

## Build and inspect

From the repository root, set `OPENAI_API_KEY` to an existing gateway key.
For the current cluster, load it without printing the value:

```bash
export OPENAI_API_KEY="$(kubectl get secret inference-gateway-api-keys \
  -n daedalus-dynamo -o jsonpath='{.data.switchyard}' | base64 --decode)"
```

Keep the HTTPS hostname in the URL. Docker's `--add-host` provides the LAN
NodePort IP override for both discovery and generation while preserving certificate
verification:

```bash
docker build \
  -t daedalus-dynamo-aiperf:0.13.0 \
  benchmarks/aiperf

docker run --rm \
  --add-host daedalus.ddns.me:192.168.1.234 \
  -e ENDPOINT_URL=https://daedalus.ddns.me:32359 \
  -e OPENAI_API_KEY \
  daedalus-dynamo-aiperf:0.13.0 \
  --list-models
```

`--list-models` only reads the model catalog. `--dry-run` reads the catalog and
prints redacted AIPerf commands without generating inference traffic:

```bash
docker run --rm \
  --add-host daedalus.ddns.me:192.168.1.234 \
  -e ENDPOINT_URL=https://daedalus.ddns.me:32359 \
  -e OPENAI_API_KEY \
  daedalus-dynamo-aiperf:0.13.0 \
  --dry-run
```

## Run every model

Create a writable host artifact directory, then run the client as your current
user:

```bash
mkdir -p artifacts/aiperf

docker run --rm --init \
  --user "$(id -u):$(id -g)" \
  --add-host daedalus.ddns.me:192.168.1.234 \
  -e ENDPOINT_URL=https://daedalus.ddns.me:32359 \
  -e OPENAI_API_KEY \
  -v "$PWD/artifacts/aiperf:/artifacts" \
  daedalus-dynamo-aiperf:0.13.0
```

The equivalent Compose workflow maps the hostname to node `192.168.1.234`
and uses the existing fixed NodePort `32359`. Set `ENDPOINT_CONNECT_IP` to
select another cluster node's LAN IP:

```bash
mkdir -p artifacts/aiperf
AIPERF_UID="$(id -u)" AIPERF_GID="$(id -g)" \
  docker compose -f benchmarks/aiperf/docker-compose.yaml run --rm aiperf
```

The gateway requires the exported key for both discovery and generation.
HTTP 401 or 403 means the gateway rejected the supplied credential even if the
local connection succeeded. Use a current gateway key, or use
`make benchmark-agentic` to load the configured Kubernetes Secret directly into
the pod.
You can also enter a key interactively. The runner omits it from its run
manifest and redacts it in dry-run commands:

```bash
read -rsp 'Gateway API key: ' OPENAI_API_KEY && printf '\n'
export OPENAI_API_KEY
AIPERF_UID="$(id -u)" AIPERF_GID="$(id -g)" \
  docker compose -f benchmarks/aiperf/docker-compose.yaml run --rm aiperf
unset OPENAI_API_KEY
```

## Tune the bounded workload

All model runs use the same generated dataset and seed. The request count is
`DATASET_SESSIONS * TURNS`. `BENCHMARK_DURATION_SECONDS` controls the session-start
window, not a stop timer. For example, this doubles the default session density
over the same 30-minute window:

```bash
mkdir -p artifacts/aiperf
AIPERF_UID="$(id -u)" AIPERF_GID="$(id -g)" \
DATASET_SESSIONS=256 BENCHMARK_DURATION_SECONDS=1800 \
  docker compose -f benchmarks/aiperf/docker-compose.yaml run --rm aiperf
```

The checked-in default concurrency of three models the current maximum of one
active turn from each of the two people and the autonomous bot. A higher value
is a deliberate stress test, not an SLA-representative run. The generated
sessions and their token shape remain a recipe proxy until replaced by a
Daedalus trace.

Changing turn counts or token lengths changes the reuse estimate. Larger turn
counts may require increasing `max_prompt_tokens` in a custom `AGENTIC_CONFIG`.
For a short Kubernetes validation, use:

```bash
make benchmark-agentic AIPERF_DATASET_SESSIONS=2 AIPERF_TURNS=2 \
  AIPERF_BENCHMARK_DURATION_SECONDS=30
```

Set `MODELS` to a comma-separated list for a focused run. Discovery remains the
default because it is the only way to cover every model currently advertised by
the gateway.

The default `auto` tokenizer resolves each advertised model ID and counts with
that model's tokenizer. This can download tokenizer files from Hugging Face,
but avoids the output-length drift produced by AIPerf's generic `builtin`
tokenizer. Set `TOKENIZER=builtin` only as an approximate offline fallback. Set
`USE_SERVER_TOKEN_COUNT=1` only after confirming that all gateway responses
contain consistent usage fields.

## GPU telemetry

Each model profile passes `--gpu-telemetry 192.168.1.235:32400/metrics` by
default. Set `GPU_TELEMETRY` to another DCGM exporter endpoint, or set it to an
empty string to pass `--no-gpu-telemetry` and disable collection entirely:

```bash
GPU_TELEMETRY=192.168.1.235:9400/metrics ./benchmarks/aiperf/run-model-benchmarks.sh
GPU_TELEMETRY= ./benchmarks/aiperf/run-model-benchmarks.sh
```

Compose forwards the same variable. Rebuild the image after changing the
script, which is copied into the image during the build:

```bash
docker compose -f benchmarks/aiperf/docker-compose.yaml build aiperf
GPU_TELEMETRY= docker compose -f benchmarks/aiperf/docker-compose.yaml run --rm aiperf
```

The runner records the configured endpoint in `run-manifest.json` (null when
disabled). AIPerf writes collected samples to each model's
`gpu_telemetry_export.jsonl` and summaries under `telemetry_data` in
`profile_export_aiperf.json`. The runner uses `--ui none`, so there is no live
telemetry dashboard.

The default NodePort Service load-balances across DCGM exporter pods; its node
IP does not select that node's GPUs. A scrape can therefore capture another
workload's GPU. Check the exported `hostname` and `pod_name` before attributing
telemetry to the benchmarked model, and use a dedicated exporter endpoint when
measuring a specific worker.

## Dynamo KV-cache telemetry

The workload creates real repeated-prefix opportunities independently of the
frontend router mode: each session retains prior user and live assistant
messages while adding the next turn. However,
`dynamo_frontend_cached_tokens_sum` is frontend-router evidence, not a direct
TensorRT-LLM cache-hit counter. It only records observations when the frontend
uses KV-aware routing.

For this chart, `frontend.routerMode: kv` enables approximate KV routing because
the frontend is launched with `--no-router-kv-events` and
`--router-track-output-blocks`. If a values override selects `round-robin`, the
frontend cached-token count remains zero even when the single TensorRT-LLM
worker has block reuse enabled. Confirm the observation path before the run:

```promql
sum by (model) (rate(dynamo_frontend_cached_tokens_count[$__rate_interval]))
```

A zero result means the cached-token ratio cannot establish cache reuse. Do not
treat it as proof that the backend cache missed.

## Artifacts and interpretation

Each run gets an immutable timestamped directory with:

- `run-manifest.json`, including the exact workload, dataset digest, AIPerf
  version and source commit, models, and unknown infrastructure facts.
- The generated `dataset.jsonl`, generator manifest, and quality reports.
- `scheduled-dataset.jsonl`, the exact replay input, and `workload-plan.json`,
  containing the effective session-start span and modeled prefix reuse.
- Per-model `profile_export_aiperf.json` summaries and
  `profile_export.jsonl` request records.
- `run-summary.json` with the exit status and artifact directory for every
  attempted model, plus observed output-length mismatch counts and rates when
  AIPerf emitted a typed summary.

Call these results client-observed prefix-reuse sensitivity. Repeated prefixes
and growing histories create cache-reuse opportunities, but they do not prove a
KV-cache hit. Confirm actual hits with Dynamo router or backend cache telemetry.
