# Scheduling controller resource patches

`make scheduling-resilience` reapplies these strategic merge patches after the
corresponding Grove and Dynamo platform Helm upgrades, then waits for each
Deployment rollout. The script checks the expected Kubernetes context, verifies
that named containers exist, performs a server dry-run, and uses the observed
resource version to reject concurrent changes. Existing resource limits and
other container fields remain intact.

| Patch | Target | Requests |
|---|---|---|
| `grove-init-resources.patch.json` | Grove `crd-installer` init container | 100m CPU, 128Mi RAM |
| `dynamo-platform-init-resources.patch.json` | Dynamo operator `crd-apply` init container | 100m CPU, 128Mi RAM |
| `lws-resources.patch.json` | LWS controller `manager` | 100m CPU, 128Mi RAM; amd64 placement |

LWS is installed separately from this repository. When its Deployment is present,
the scheduling maintenance script reapplies its patch after the platform upgrade.
After an external LWS reinstall, run `make scheduling-resilience` to restore these
local settings. The script does not create LWS when it is absent.

The paths can be overridden with `GROVE_INIT_RESOURCE_PATCH`,
`PLATFORM_INIT_RESOURCE_PATCH`, and `LWS_RESOURCE_PATCH`; `LWS_NAMESPACE` defaults
to `lws-system`. `make check-scheduling-resilience` remains read-only.

The init reservations are conservative startup estimates because their short
executions were not captured in the historical metrics. Main Grove/Dynamo and
NATS requests are configured in the adjacent Helm values files.
