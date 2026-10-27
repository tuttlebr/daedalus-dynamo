# One active Bolt model

The active inference worker loads
`nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB` at revision
`c6cc2e8ffa353403213d6b9b18aaf8eb684d705a` on GB10. The former model IDs
`Qwen/Qwen3.6-35B-A3B-FP8` and `Qwen/Qwen3.8-27B-FP8` are compatibility
aliases for these Bolt weights. They do not select Qwen weights.

The Bolt worker lists its canonical name first in `--served-model-name`, followed
by both aliases. Dynamo's vLLM configuration splits these into a primary name and
aliases and passes the aliases to native model registration. Its frontend
resolves alias requests to the primary model. This includes frontend model
discovery; it is not only a vLLM engine option. NVIDIA documents
the behavior in the [multiple served names release notes](https://docs.nvidia.com/dynamo/dev/reference/releases/v1-4-0).

`config/model-compatibility.json` is the reviewed gateway mapping. The route
renderer preserves the requested model ID, route name, header match, request
timeout and port while selecting the Bolt frontend. Alias annotations identify
the actual canonical model and revision. The catalog retains the old model IDs
and exposes `compatibility_alias`, `canonical_model` and `model_revision` fields.
The resolver rejects unknown aliases, unknown canonical deployments, cycles,
invalid revisions and incomplete alias annotations. Other model routes retain
their configured backends.

Both the historical and current Qwen L40S profiles default to zero inference
workers. Their frontend, ModelExpress, Redis, cache claims and rollback settings
remain. The current stack upgrade script therefore preserves the retired state.
An explicit restoration must also restore the compatible route/catalog state;
do not uninstall the release or delete its caches as part of this transition.

## Controlled transition

1. Save current Helm values and revisions, full graph components, frontend and
   worker identities, gateway routes, catalog configuration and deployment
   checksum, Services, network policies and PVC/PV identities. Keep these local
   backups private. Record gateway authentication/TLS references without copying
   credentials into a commit.
2. Review the complete rendered diff. The Bolt worker command changes only its
   served names. Qwen worker replicas become zero. No image, model revision,
   cache, resource limit, parser, context limit, Service selector, NodePort,
   gateway authentication or TLS setting changes.
3. Apply the Bolt alias update with a graph-UID and complete-worker comparison.
   Synchronize the same argument list into the release's retained Helm values.
   The sole GB10 worker restarts to advertise its new model card; keep existing
   clients and the Qwen worker intact during this interval.
4. Allow at most 15 minutes for startup. Confirm the expected new worker image,
   model revision, Ready state, restart count and all three names in the actual
   frontend catalog. Use bounded internal API checks for the canonical ID and
   both aliases, including a structured tool round trip. Record the returned
   model name and verify the explicit Bolt identity in compatibility metadata;
   an echoed request alias is not evidence that Qwen weights were loaded.
   These are functional checks, not speed tests.
5. Update the existing legacy HTTPRoute backend and its alias annotations. Keep
   its UID, name, parent, model header match, timeout and port. Refresh only the
   catalog data and its deployment checksum. Do not replace the gateway base,
   API-key policy, TLS configuration or Service.
6. Move service consumers to the canonical Bolt endpoint and verify their normal
   application/API paths. Apply Qwen worker replicas zero using its current
   graph UID and complete-worker comparison, then retain zero in Helm values.
   Keep its frontend and all PVC/PV objects.
7. Recheck the single active inference worker, saved endpoint/authentication
   signatures and retained storage identities. Commit the compatible source
   settings so a later route render or stack upgrade cannot restore Qwen
   inference or send legacy requests to its retired worker.

If startup or functional checks fail, stop the transition and restore the saved
worker, route and catalog configuration. Give rollback a separate 15-minute
budget and verify the restored application/API paths. Before applying a reverse
patch, require the same graph UID and the expected transition component; refuse
to overwrite a concurrent owner's changes. Restore the corresponding Helm
values and source revision so rollback survives replay. Never delete caches or
change client authentication to make a rollback pass.
