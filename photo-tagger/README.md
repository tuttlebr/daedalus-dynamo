# Photo Tagger

Photo Tagger is the repository's single shared reference application. It is a
Rust web service that walks mounted photo directories, sends each unseen image
to an OpenAI-compatible chat-completions endpoint, and stores searchable
metadata in Postgres.

The app is intentionally model-neutral. The repository-root `deploy.sh` injects
`LLM_ENDPOINT`, `MODEL_NAME`, and the image delivery mode for the selected
Dynamo backend. Developers can replace this app with any application that calls
the same OpenAI-compatible gateway.

## Architecture Contract

Photo Tagger depends on three external surfaces:

- OpenAI-compatible chat completions:
  `POST ${LLM_ENDPOINT}` with a request body containing `model`, `messages`,
  `max_tokens`, and optional `response_format`.
- Postgres metadata storage through `DATABASE_URL`.
- Mounted image roots listed in `ALLOWED_ROOTS`.

The shared Dynamo deployment exposes:

- `http://inference-gateway:80/v1/chat/completions` inside the namespace.
- `/v1/models` through the generated model catalog.
- Model routing by the OpenAI request `model` field.

To integrate a different app, deploy it in the same namespace and point its
OpenAI client at `http://inference-gateway:80/v1`. Set
`DEPLOY_PHOTO_TAGGER=0` on the root deploy command if you only want the gateway
and model backends.

## What It Does

- Walks any subtree of the configured mounted shares.
- Skips files it already processed by SHA256 content hash.
- Persists job summaries, failed paths, and photo metadata in Postgres.
- Accepts JPG, PNG, WebP, GIF, BMP, TIFF, and HEIF/HEIC inputs.
- Transcodes non-JPEG/PNG inputs to JPEG and downscales oversized images.
- Writes optional `<image>.metadata.json` sidecars under writable roots.
- Provides a single-page UI for scans, job history, search, and browsing.

## Image Delivery

`IMAGE_DELIVERY` controls how the app passes images to the OpenAI-compatible
backend:

- `data-url`: inline each prepared image as a base64 data URL. This works with
  backends that accept data URLs.
- `url`: serve each prepared image from `/llm-image/{token}` and pass that
  short-lived HTTP URL. This is required for backends that only accept HTTP(S)
  image URLs.

The root deploy script selects the right default for the configured backend.
Override with `PHOTO_TAGGER_IMAGE_DELIVERY` if a custom backend needs a specific
mode.

## Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/` | Browser UI |
| `GET` | `/health` | Process health |
| `GET` | `/ready` | Database and LLM endpoint readiness |
| `POST` | `/scan` | Start a scan: `{ "path": "...", "recursive": true }` |
| `POST` | `/reprocess` | Clear and rerun one file |
| `GET` | `/jobs` | Recent jobs |
| `GET` | `/jobs/{id}` | Job summary |
| `GET` | `/jobs/{id}/events` | SSE progress stream |
| `GET` | `/metadata?path=...` | Full metadata for one file |
| `GET` | `/search?q=...&limit=` | Full-text metadata search |
| `GET` | `/browse?limit=` | Recently processed gallery |
| `GET` | `/stats` | Processed and failed totals |
| `GET` | `/thumb?path=...` | Thumbnail for one file |
| `GET` | `/llm-image/{token}` | Short-lived worker image URL in `url` mode |

All routes except `/health`, `/ready`, and `/llm-image/{token}` are auth-gated
when `PHOTO_TAGGER_REQUIRE_AUTH=true`.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `LISTEN_ADDR` | `0.0.0.0:3000` | HTTP listen address |
| `DATABASE_URL` | `postgres://photo_tagger:photo_tagger@photo-tagger-postgres:5432/photo_tagger` | Postgres connection URL |
| `LLM_ENDPOINT` | required | OpenAI-compatible chat-completions endpoint |
| `MODEL_NAME` | required | OpenAI model id sent in each request |
| `IMAGE_DELIVERY` | `data-url` | `data-url` or `url` |
| `IMAGE_BASE_URL` | `http://photo-tagger:3000` | Base URL workers use for `url` image delivery |
| `PHOTO_TAGGER_REQUIRE_AUTH` | `false` | Require auth for UI/API routes |
| `PHOTO_TAGGER_AUTH_USER` | `photo-tagger` | HTTP Basic username |
| `PHOTO_TAGGER_AUTH_TOKEN` | unset | HTTP Basic password, bearer token, or `X-Photo-Tagger-Token` |
| `WRITE_SIDECAR` | `true` | Write sidecar JSON under `SIDECAR_ROOTS` |
| `SIDECAR_ROOTS` | `/volume2/daedalus` | Comma-separated writable roots |
| `ALLOWED_ROOTS` | `/volume1/homes,/volume2/daedalus` | Comma-separated scan boundary roots |
| `MAX_CONCURRENT_LLM` | `4` | In-flight LLM requests |
| `MAX_CONCURRENT_HASH` | `8` | Disk/hash/transcode concurrency |
| `MAX_FILE_BYTES` | `104857600` | Reject larger files before reading |
| `MAX_IMAGE_PIXELS` | `80000000` | Reject larger decoded images |
| `MAX_EDGE_PIXELS` | `2048` | Longest-edge downscale target |
| `DOWNSCALE_LARGE` | `true` | Enable large-image downscale |
| `LLM_TIMEOUT_SECS` | `180` | Per-request timeout |
| `LLM_MAX_RETRIES` | `3` | Retries for 429, 5xx, and parse failures |
| `JPEG_QUALITY` | `85` | JPEG transcode and thumbnail quality |
| `RUST_LOG` | `info,photo_tagger=info` | Tracing filter |

`QWEN_ENDPOINT` and `NEMOTRON_ENDPOINT` are accepted only as compatibility
fallbacks when `LLM_ENDPOINT` is unset.

## Local Development

Run from the repository root:

```bash
cargo test --manifest-path photo-tagger/Cargo.toml

DATABASE_URL=postgres://photo_tagger:photo_tagger@localhost:5432/photo_tagger \
LLM_ENDPOINT=http://localhost:8000/v1/chat/completions \
MODEL_NAME=<model-id> \
ALLOWED_ROOTS=/path/to/photos \
WRITE_SIDECAR=false \
cargo run --manifest-path photo-tagger/Cargo.toml
```

Then start a scan:

```bash
curl -X POST http://localhost:3000/scan \
  -H 'Content-Type: application/json' \
  -d '{"path": "/path/to/photos", "recursive": true}'
```

## Kubernetes Deployment

The root deployment renders one `photo-tagger` Deployment, one
`photo-tagger-postgres` StatefulSet, one NodePort service, and one NetworkPolicy.
It mounts the configured NFS shares and sets:

- `LLM_ENDPOINT=http://inference-gateway:80/v1/chat/completions`
- `MODEL_NAME` from `DEFAULT_MODEL` or the selected default backend
- `IMAGE_DELIVERY` from the selected default backend

Use the root deployment entrypoint:

```bash
HF_TOKEN=<hugging-face-token> ./deploy.sh deploy nemotron qwen35b qwen27b
DEPLOY_PHOTO_TAGGER=0 HF_TOKEN=<hugging-face-token> ./deploy.sh deploy all
```

## Metadata Schema

See `src/models.rs`. The top-level envelope records path, SHA256, file mtime,
dimensions, model, prompt versions, and processing duration. The LLM-filled
`content` block includes caption, description, tags, scene, setting type,
detected text, dominant colors, people observations, objects, and quality.

`SCHEMA_VERSION` changes when the persisted shape changes. `PROMPT_VERSION`
changes when the image-analysis prompt changes.
