# Dynamo Model Helm Chart

This chart renders one Dynamo model backend release from a model values file.
It is intended to replace the repeated per-model Kubernetes YAML once the Helm
path is proven in the cluster.

## Render

```bash
helm template nemotronnano ./charts/dynamo-model \
  -n daedalus-dynamo \
  -f values/nemotronnano.yaml
```

Render the DGDR path instead of the direct DGD path:

```bash
helm template qwen35b ./charts/dynamo-model \
  -n daedalus-dynamo \
  -f values/qwen35b.yaml \
  --set model.deploymentMode=dgdr
```

## Install Or Upgrade

For a new backend:

```bash
helm upgrade --install nemotronnano ./charts/dynamo-model \
  -n daedalus-dynamo \
  -f values/nemotronnano.yaml
```

For a backend that was already created outside Helm, Helm 3.17+ can adopt the
existing resources:

```bash
helm upgrade --install nemotronnano ./charts/dynamo-model \
  -n daedalus-dynamo \
  -f values/nemotronnano.yaml \
  --take-ownership
```

The chart preserves the existing Redis selector contract
(`app.kubernetes.io/name`) so existing Redis Deployments can be adopted without
hitting immutable selector changes.

## Planner Namespace Compatibility

When `planner.workerNamespaceCompat=legacy`, the chart starts Planner through a
small Python wrapper that keeps worker runtime discovery on the base
`DYN_NAMESPACE`. This matches operator/runtime stacks where workers publish
forward-pass metrics at:

```text
<k8s-namespace>-<dgd-name>.backend.generate
```

Set `planner.workerNamespaceCompat=hashed` only with an operator/runtime stack
that publishes worker metadata and forward-pass metrics under
`<DYN_NAMESPACE>-<worker-hash>`.

The old post-install annotation hook remains in values for emergency manual use,
but it is disabled by default and is not part of the normal deploy path.

## Current Scope

This chart owns model-local resources:

- model cache PVC
- Redis PVC, Service, and Deployment
- direct `DynamoGraphDeployment` or `DynamoGraphDeploymentRequest`
- Planner namespace compatibility settings
- optional decode-worker `PodMonitor`

The root Makefile handles image build/push, cluster doctor, gateway installer,
and HF token secret creation around this chart.

The direct DGD path starts both `Frontend` and `VllmWorker` through a shell
guard that raises and verifies the `RLIMIT_NOFILE` soft limit. The shared
default is `131072`; override `frontend.nofileLimit` or `worker.nofileLimit`
only when the container runtime hard limit and expected concurrency justify a
different value.

The model cache PVC and Redis data PVC are annotated with
`helm.sh/resource-policy: keep`, so Helm uninstall leaves the storage claims in
place for later reuse or manual cleanup.
