#!/usr/bin/env python3
import json
import sys


payload = json.load(sys.stdin)
seen = set()
models = []

for item in payload.get("items", []):
    annotations = item.get("metadata", {}).get("annotations", {})
    model_id = annotations.get("daedalus-dynamo/model-id")
    if not model_id or model_id in seen:
        continue
    seen.add(model_id)
    models.append(
        {
            "id": model_id,
            "object": "model",
            "created": 0,
            "owned_by": "daedalus",
        }
    )

print(json.dumps({"object": "list", "data": models}, separators=(",", ":")))
