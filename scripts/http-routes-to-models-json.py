#!/usr/bin/env python3
import argparse
import json
import re
import sys

from model_compatibility import catalog_metadata, load_policy

parser = argparse.ArgumentParser(description="Build the gateway catalog from model HTTPRoutes")
parser.add_argument("--include-backends", action="store_true", help="Include private runtime discovery configuration")
args = parser.parse_args()
payload = json.load(sys.stdin)
policy = load_policy()
seen = set()
models = []
backends = {}

for item in payload.get("items", []):
    annotations = item.get("metadata", {}).get("annotations", {})
    model_id = annotations.get("daedalus-dynamo/model-id")
    if not model_id:
        continue
    metadata = catalog_metadata(model_id, annotations, policy)
    if args.include_backends:
        urls = backends.setdefault(model_id, set())
        for rule in item.get("spec", {}).get("rules", []):
            for backend in rule.get("backendRefs", []):
                if backend.get("weight", 1) == 0:
                    continue
                if backend.get("kind", "Service") != "Service" or backend.get("group", ""):
                    raise ValueError("Context discovery requires Service backends")
                name = backend.get("name", "")
                namespace = backend.get("namespace", item.get("metadata", {}).get("namespace", ""))
                port = backend.get("port")
                for label in (name, namespace) if namespace else (name,):
                    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label):
                        raise ValueError("Invalid backend Service name or namespace")
                if type(port) is not int or not 1 <= port <= 65535:
                    raise ValueError("Invalid backend Service port")
                host = f"{name}.{namespace}.svc" if namespace else name
                urls.add(f"http://{host}:{port}/v1/models")
    if model_id in seen:
        continue
    seen.add(model_id)
    model = {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "daedalus",
    }
    model.update(metadata)
    models.append(model)

catalog = {"object": "list", "data": models}
if args.include_backends:
    catalog["backends"] = {model: sorted(urls) for model, urls in backends.items()}
print(json.dumps(catalog, separators=(",", ":")))
