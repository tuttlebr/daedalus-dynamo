# Dynamo model chart

The chart renders one aggregated DynamoGraphDeployment plus its cache PVC,
optional Redis metadata backend, and availability resources. The current template
uses the installed `nvidia.com/v1alpha1` services schema. The operator accepts it
but deprecates it in favor of `v1beta1`; changing the DGD API requires a separate
services-to-components/podTemplate migration.

Use `make render <recipe.yaml>` for layered catalog entries. Raw `helm -f` does
not resolve `extends`; first run `scripts/model_values.py resolve`. The chart's
`values.yaml` and `values.schema.json` define the final resolved contract.

## Shared Model Express startup

Install `charts/dynamo-model-runtime` once per namespace (`make runtime-install`).
It owns one immutable ConfigMap, `dynamo-modelexpress-runtime-v1`, containing
`server.sh` and `wait-cache.sh`. Model releases reference it with
`modelexpress.runtimeConfigMap` and do not own or delete it.

Each ModelExpress pod passes model ID, pinned revision, cache location, Redis
endpoint and download policy as environment variables. Its worker mounts the
same scripts and waits for that exact revision's completion marker. A failed
download cannot publish an older snapshot. Separate per-recipe PVCs and Redis
services retain model isolation; sharing code does not require sharing caches.

The script contract uses ordinary pod volumes, mounts and environment variables.
It can also be mounted by a hand-authored DGD or a future API's pod template.
It is not automatically injected into arbitrary DGDs. ConfigMaps are namespaced;
install one copy in each namespace that needs it. Version the name when changing
an immutable script contract, and migrate consumers deliberately.

An empty `modelexpress.runtimeConfigMap` retains the historical inline startup
path for regression fixtures and external existing callers. Catalog recipes all
use the shared ConfigMap. `worker.vllmOptions` provides mergeable named flags:
true emits a flag, false/null omits it, scalar values become quoted argv, and
mapping values become quoted JSON. `worker.vllmArgs` remains available for literal
shell argv fragments, including the served-model environment variable.

## Deployment scope

Catalog recipes use one worker, PP1/DP1 and local multiprocessing. L40S TP2
requests two GPUs in one pod. Spark and A30 use TP1. Model revisions are pinned
both in Model Express and vLLM. Planner autoscaling is disabled for these fixed
single-node recipes. The generic chart retains its existing Planner and TRT-LLM
options for callers that explicitly select them.

PVCs carry `helm.sh/resource-policy: keep` and survive model release uninstall.
The shared runtime release is independent of model release lifecycle. See the
[recipe guide](../../docs/model-recipes.md) for the hardware inventory, fit
assumptions, complete coverage and validation evidence.
