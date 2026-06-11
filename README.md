# Daedalus Dynamo Examples

This directory contains three NVIDIA Dynamo deployment examples with the same
developer workflow and companion Photo Tagger app:

| Folder | Model | Backend | Photo Tagger UI |
| --- | --- | --- | --- |
| `nemotron-3-nano-omni-dynamo/` | `nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8` | vLLM | NodePort `30150` |
| `qwen-36-35b-dynamo/` | `Qwen/Qwen3.6-35B-A3B-FP8` | vLLM | NodePort `30151` |
| `qwen-36-27b-dynamo/` | `Qwen/Qwen3.6-27B-FP8` | SGLang | NodePort `30152` |

Each folder is self-contained, but the user experience is intentionally the
same:

```bash
cd <example-folder>
make validate
NAMESPACE=<namespace> HF_TOKEN=<hugging-face-token> ./deploy.sh
kubectl port-forward -n <namespace> svc/<dynamo-frontend-service> 8000:8000
./test-endpoint.sh
```

The repo-level `deploy.sh` in each example builds and pushes the model image and
Photo Tagger image, creates required secrets, applies the Daedalus Kustomize
overlay, and supports `deploy`, `delete`, and `render` commands.

The Photo Tagger app uses the same API, UI, database schema, auth model,
sidecar export behavior, and validation flow in all three examples. Use
`LLM_ENDPOINT` as the model-neutral app setting for the Dynamo
`/v1/chat/completions` endpoint; older model-specific endpoint variables are
accepted only as compatibility fallbacks.

The main intentional implementation differences are:

- The model image repository variable is model-specific:
  `NEMOTRON_IMAGE_REPOSITORY` for Nemotron and `QWEN_IMAGE_REPOSITORY` for Qwen.
- Qwen 27B uses SGLang, so Photo Tagger defaults to `IMAGE_DELIVERY=url` and
  serves short-lived prepared images over HTTP for the inference worker.
- The two vLLM examples inline prepared images as data URLs and do not expose
  the internal `/llm-image/{token}` route.

