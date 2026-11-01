"""Populate exact draft snapshots before vLLM opens their local paths."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PinnedModel:
    id: str
    revision: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[\w-]+/[\w.-]+", self.id, flags=re.ASCII):
            raise ValueError("Expected a Hugging Face repository ID")
        if not re.fullmatch(r"[0-9a-f]{40}", self.revision):
            raise ValueError("Draft revision must be a full commit, never a mutable ref")

    def snapshot(self, cache: Path) -> Path:
        return cache / ("models--" + self.id.replace("/", "--")) / "snapshots" / self.revision


def prefetch(models: list[PinnedModel], cache: Path) -> None:
    from huggingface_hub import snapshot_download

    for model in models:
        # Always let HF verify download completeness; a directory alone can be
        # left behind by an interrupted download. Credentials come from env.
        downloaded = Path(snapshot_download(repo_id=model.id, revision=model.revision, cache_dir=str(cache)))
        if downloaded.resolve() != model.snapshot(cache).resolve() or not downloaded.is_dir():
            raise RuntimeError(f"Unexpected draft snapshot for {model.id}")
        print(f"Draft snapshot ready: {model.id}@{model.revision}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("models_json")
    args = parser.parse_args()
    prefetch([PinnedModel(**item) for item in json.loads(args.models_json)], args.cache_dir)
