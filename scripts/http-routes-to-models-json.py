#!/usr/bin/env python3
import json
import sys

from model_compatibility import catalog_metadata, load_policy


payload = json.load(sys.stdin)
policy = load_policy()
seen = set()
models = []

for item in payload.get("items", []):
    annotations = item.get("metadata", {}).get("annotations", {})
    model_id = annotations.get("daedalus-dynamo/model-id")
    if not model_id or model_id in seen:
        continue
    seen.add(model_id)
    model = {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "daedalus",
    }
    model.update(catalog_metadata(model_id, annotations, policy))
    models.append(model)

print(json.dumps({"object": "list", "data": models}, separators=(",", ":")))
