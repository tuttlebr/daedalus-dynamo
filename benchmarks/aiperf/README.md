# Inference Gateway AIPerf Runner

This container discovers every model from the inference gateway's `/v1/models`
catalog and profiles each model in catalog order with NVIDIA AIPerf 0.11.0.
It does not hard-code the model list.

The frozen workload models an agentic conversation with four prefix layers:

1. A 1,024-token system/tool prefix shared by every session.
2. A 512-token repository prefix shared within one of four groups.
3. A 1,024-token context unique to each session.
4. Six growing conversation turns. AIPerf appends every live assistant response
   before sending the next user turn.

The default run replays eight sessions with the generator's bounded agentic
inter-turn schedule, a concurrency cap of four, and exactly 48 requests per
model. Every session sends `X-Dynamo-Session-ID`, and its HTTP connection is
reused across turns. The generator uses AIPerf 0.11.0's fixed 512-token
Mooncake block size so the generated hash IDs and trace loader reconstruct the
same shared prompt blocks. AIPerf 0.11.0 rejects block-size overrides for file
datasets, so the runner fails early if a custom agentic config uses any other
block size.

## Build and inspect

From the repository root:

```bash
docker build \
  -t daedalus-dynamo-aiperf:0.11.0 \
  benchmarks/aiperf

docker run --rm \
  -e ENDPOINT_URL=http://192.168.1.234:32359 \
  daedalus-dynamo-aiperf:0.11.0 \
  --list-models
```

`--list-models` only reads the model catalog. `--dry-run` reads the catalog and
prints redacted AIPerf commands without generating inference traffic:

```bash
docker run --rm \
  -e ENDPOINT_URL=http://192.168.1.234:32359 \
  daedalus-dynamo-aiperf:0.11.0 \
  --dry-run
```

## Run every model

Create a writable host artifact directory, then run the client as your current
user:

```bash
mkdir -p artifacts/aiperf

docker run --rm --init \
  --user "$(id -u):$(id -g)" \
  -e ENDPOINT_URL=http://192.168.1.234:32359 \
  -v "$PWD/artifacts/aiperf:/artifacts" \
  daedalus-dynamo-aiperf:0.11.0
```

The equivalent Compose workflow is:

```bash
mkdir -p artifacts/aiperf
AIPERF_UID="$(id -u)" AIPERF_GID="$(id -g)" \
  docker compose -f benchmarks/aiperf/docker-compose.yaml run --rm aiperf
```

For an authenticated endpoint, export the key only for the container process.
The runner never writes it to the run manifest or rendered commands:

```bash
read -rsp 'Gateway API key: ' OPENAI_API_KEY && printf '\n'
export OPENAI_API_KEY
AIPERF_UID="$(id -u)" AIPERF_GID="$(id -g)" \
  docker compose -f benchmarks/aiperf/docker-compose.yaml run --rm aiperf
unset OPENAI_API_KEY
```

## Tune the bounded workload

All model runs use the same generated dataset and seed. The request count is
`DATASET_SESSIONS * TURNS`. For example, this runs 16 eight-turn sessions, or
128 requests per model:

```bash
mkdir -p artifacts/aiperf
AIPERF_UID="$(id -u)" AIPERF_GID="$(id -g)" \
DATASET_SESSIONS=16 TURNS=8 CONCURRENCY=8 \
  docker compose -f benchmarks/aiperf/docker-compose.yaml run --rm aiperf
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
- Per-model `profile_export_aiperf.json` summaries and
  `profile_export.jsonl` request records.
- `run-summary.json` with the exit status and artifact directory for every
  attempted model, plus observed output-length mismatch counts and rates when
  AIPerf emitted a typed summary.

Call these results client-observed prefix-reuse sensitivity. Repeated prefixes
and growing histories create cache-reuse opportunities, but they do not prove a
KV-cache hit. Confirm actual hits with Dynamo router or backend cache telemetry.
