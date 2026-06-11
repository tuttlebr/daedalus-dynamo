# Photo Tagger

A Rust service that walks NAS-resident photo directories, sends each unseen
image to the Nemotron-3-Nano-Omni vLLM endpoint, and persists structured
metadata for text-based search. Single-page UI for triggering scans, watching
progress, browsing results, and full-text search.

## What it does

- Walks any subtree of the mounted NAS shares (allowed roots are configurable).
- Skips files it has already processed (identity = SHA256 of file contents,
  rename-tolerant). Survives pod restarts.
- Persists job summaries and failed-file path records in Postgres so scan
  history and failures remain visible after pod restarts.
- Accepts JPG / PNG / WebP / GIF / BMP / TIFF on disk; non-PNG/JPG inputs are
  transcoded to JPEG before being sent to the LLM. Oversized images
  (>2048px on the longest edge) are also downscaled.
- Submits one chat-completions call per image to the Nemotron Omni endpoint
  with a structured-JSON response format and a defensive parser for reasoning
  output.
- Persists a row per file into Postgres (with full-text search)
  and writes a `<image>.metadata.json` sidecar alongside each image (where the
  share is writable).
- UI tabs: **Scan** (kick off a scan, watch SSE progress), **Jobs** (history),
  **Search** (FTS over caption / description / tags / detected text), **Browse**
  (recently processed gallery).

## Mounted NAS shares (in-pod)

The deployment mounts NAS shares directly via NFS (no `hostPath` dependency on
the worker node):

| In-pod path        | NFS source                                    | Access     |
|--------------------|-----------------------------------------------|------------|
| `/volume1/homes`   | `192.168.1.232:/volume1/homes` (Synology)     | read-only  |
| `/volume2/daedalus`| `192.168.1.77:/var/nfs/shared/daedalus`       | read-write |

Metadata lives in the `photo-tagger-postgres` StatefulSet. Sidecar JSON files
are written only under configured writable roots, which default to
`/volume2/daedalus`; `/volume1/homes` scans persist metadata to Postgres only.

To expose additional NFS shares, add another `volumeMount` + `volumes.nfs`
entry to `../k8s/overlays/daedalus/photo-tagger-daedalus-patch.yaml` and
append the new path to `ALLOWED_ROOTS` in the overlay kustomization.

## Storage layout

```
/volume2/daedalus/<…>/<your-photos>/  # read-write share — sidecars persisted
├── IMG_0001.jpg
├── IMG_0001.jpg.metadata.json        # written by the service
└── …

/volume1/homes/<user>/<…>/            # read-only share — sidecar writes skipped
├── IMG_0001.jpg                      # Postgres row still captures everything
└── …
```

The Postgres row is always authoritative; sidecars are a portable belt-and-braces
copy where the underlying NFS share is writable.

## Endpoints

| Method | Path | Notes |
|--------|------|-------|
| GET    | `/`                         | UI |
| GET    | `/health`                   | `{ status }` |
| GET    | `/ready`                    | `{ status, database, endpoint_reachable }` |
| POST   | `/scan`                     | `{ path, recursive }` → `{ job_id }` |
| POST   | `/reprocess`                | `{ path }` → `{ job_id }` (clears + re-runs one file) |
| GET    | `/jobs`                     | List recent jobs |
| GET    | `/jobs/{id}`                | Single job summary |
| GET    | `/jobs/{id}/events`         | SSE stream of progress + completions |
| GET    | `/metadata?path=...`        | Full PhotoMetadata JSON for one absolute path |
| GET    | `/search?q=...&limit=`      | Postgres full-text hits |
| GET    | `/browse?limit=`            | Recently processed |
| GET    | `/stats`                    | Totals broken down by setting type, model, and failed paths |
| GET    | `/thumb?path=...`           | 256px JPEG thumbnail |

## Configuration (env vars)

| Variable | Default | Notes |
|----------|---------|-------|
| `LISTEN_ADDR`         | `0.0.0.0:3000` | |
| `DATABASE_URL`        | `postgres://photo_tagger:photo_tagger@photo-tagger-postgres:5432/photo_tagger` | Postgres connection URL |
| `LLM_ENDPOINT`        | `http://nemotron-omni-vllm-agg-frontend:8000/v1/chat/completions` | Same-namespace Dynamo chat-completions endpoint; override if running outside the cluster |
| `MODEL_NAME`          | `nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8` | |
| `PHOTO_TAGGER_REQUIRE_AUTH` | `false` | When true, all UI/API routes except `/health` and `/ready` require auth |
| `PHOTO_TAGGER_AUTH_USER` | `photo-tagger` | HTTP Basic username |
| `PHOTO_TAGGER_AUTH_TOKEN` | unset | HTTP Basic password, bearer token, or `X-Photo-Tagger-Token` value |
| `WRITE_SIDECAR`       | `true` | Enable sidecar writes under `SIDECAR_ROOTS` |
| `SIDECAR_ROOTS`       | `/volume2/daedalus` | Comma-separated writable roots for `<img>.metadata.json` |
| `ALLOWED_ROOTS`       | `/volume1/homes,/volume2/daedalus` | Comma-separated; rejects paths outside (must match in-pod NFS mounts) |
| `MAX_CONCURRENT_LLM`  | `4`  | Bounds in-flight requests to vLLM |
| `MAX_CONCURRENT_HASH` | `8`  | Bounds disk I/O / hashing / transcoding |
| `MAX_FILE_BYTES`      | `104857600` | Reject files larger than this before reading them into memory |
| `MAX_IMAGE_PIXELS`    | `80000000` | Reject decoded images above this pixel count |
| `MAX_EDGE_PIXELS`     | `2048` | Downscale longest edge to this when oversized |
| `DOWNSCALE_LARGE`     | `true` | Toggle the downscale path |
| `LLM_TIMEOUT_SECS`    | `180` | Per-request timeout |
| `LLM_MAX_RETRIES`     | `3`  | Exponential backoff on 429 / 5xx / parse failure |
| `JPEG_QUALITY`        | `85` | Used for transcoded images and thumbnails |
| `RUST_LOG`            | `info,photo_tagger=info` | Tracing filter |

`NEMOTRON_ENDPOINT` and `QWEN_ENDPOINT` are still accepted as compatibility
fallbacks when `LLM_ENDPOINT` is unset.

## Deployment

```bash
cd /volume2/daedalus/datasets/nemotron-3-nano-omni-dynamo
HF_TOKEN=<your-hf-token> NAMESPACE=${NAMESPACE:-default} ./deploy.sh
```

The repo-level script builds and pushes both images, creates/updates
`hf-token-secret`, creates `photo-tagger-postgres` with a generated password if
the secret is missing, creates `photo-tagger-auth` with a generated token if it
is missing, then applies the integrated overlay for Dynamo, Redis, Postgres, and
this app. `photo-tagger/deploy.sh` delegates to the same script.

The app pod uses direct NFS volumes (so worker nodes don't need pre-existing
host mounts). The overlay also creates `photo-tagger-postgres` with a PVC for
metadata storage. NodeAffinity is set to `daedalus-02|03|04` for the app pod.

NodePort 30150 exposes the UI for cluster-external access:

```
http://<any-node-ip>:30150/
```

The Daedalus overlay sets `PHOTO_TAGGER_REQUIRE_AUTH=true`. The browser will
prompt for HTTP Basic auth; use username `photo-tagger` and the token stored in
the `photo-tagger-auth` secret:

```bash
kubectl get secret photo-tagger-auth -n ${NAMESPACE} \
  -o jsonpath='{.data.token}' | base64 -d
```

The app validates configured roots at startup. `ALLOWED_ROOTS` and
`SIDECAR_ROOTS` entries must exist in the pod, and sidecar roots must be inside
allowed roots when `WRITE_SIDECAR=true`.

## Local development

```bash
cd /volume2/daedalus/datasets/nemotron-3-nano-omni-dynamo/photo-tagger
DATABASE_URL=postgres://photo_tagger:photo_tagger@localhost:5432/photo_tagger cargo run --release
# In another terminal:
curl -X POST http://localhost:3000/scan \
  -H 'Content-Type: application/json' \
  -d '{"path": "/volume1/homes/<user>/Photos", "recursive": true}'
```

## Metadata schema

See `src/models.rs`. Top-level envelope (filled by the harness) records the
absolute path, SHA256, file mtime, dimensions, model + prompt versions, and
processing duration. The `content` block (filled by the LLM) covers caption,
description, tags, scene, setting type, detected text, dominant colors, people
observations, objects, and a quality assessment.

`SCHEMA_VERSION` is bumped whenever the persisted shape changes.
`PROMPT_VERSION` is bumped whenever the prompt is edited (so old rows can be
identified as needing reprocessing).
