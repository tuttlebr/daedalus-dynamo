#!/usr/bin/env python3
"""Spread fresh agentic sessions across a time window and report reuse potential."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def prepare_trace(rows: list[dict], duration_seconds: int) -> tuple[list[dict], dict]:
    if duration_seconds < 0:
        raise ValueError("duration_seconds must be nonnegative")
    sessions: dict[str, list[dict]] = {}
    for row in rows:
        sessions.setdefault(row["session_id"], []).append(dict(row))
    if not sessions:
        raise ValueError("trace must contain at least one session")
    if duration_seconds and len(sessions) < 2:
        raise ValueError("a timed trace needs at least two sessions; use duration 0 for a single-session smoke test")

    scheduled: list[dict] = []
    seen_prefixes: set[tuple[int, ...]] = set()
    total_prompt_tokens = reusable_tokens = within_session_tokens = 0
    max_prompt_tokens = 0
    for index, turns in enumerate(sessions.values()):
        first = turns[0]
        if "timestamp" not in first:
            raise ValueError("each session must start with a timestamp")
        # Put the final session at the duration boundary, not just before it.
        # Fixed-schedule replay must still issue it and complete its turns.
        first["timestamp"] = (
            round(index * duration_seconds * 1000 / (len(sessions) - 1), 3)
            if len(sessions) > 1 else 0
        )
        prefix = tuple(first["hash_ids"])
        shared_blocks = 0
        for length in range(1, len(prefix) + 1):
            if prefix[:length] not in seen_prefixes:
                break
            shared_blocks = length
        reusable_tokens += min(first["input_length"], shared_blocks * 512)
        for length in range(1, len(prefix) + 1):
            seen_prefixes.add(prefix[:length])

        history = 0
        for turn_index, turn in enumerate(turns):
            if turn_index and "timestamp" in turn:
                raise ValueError("continuation turns must use relative delays")
            prompt_tokens = history + turn["input_length"]
            total_prompt_tokens += prompt_tokens
            max_prompt_tokens = max(max_prompt_tokens, prompt_tokens)
            reusable_tokens += history
            within_session_tokens += history
            history = prompt_tokens + turn["output_length"]
            scheduled.append(turn)

    report = {
        "schema_version": 1,
        "sessions": len(sessions),
        "requests": len(scheduled),
        "session_start_span_seconds": duration_seconds,
        "target_prefix_reuse_pct": 70.0,
        "estimated_prefix_reuse_pct": 100 * reusable_tokens / total_prompt_tokens,
        "within_session_reuse_pct": 100 * within_session_tokens / total_prompt_tokens,
        "total_prompt_tokens": total_prompt_tokens,
        "estimated_reusable_prompt_tokens": reusable_tokens,
        "max_prompt_tokens": max_prompt_tokens,
        "assumptions": [
            "Token-weighted estimate from incremental Mooncake inputs and configured output lengths.",
            "Earlier prompts and generated output remain cached for later turns.",
            "Repeated first-turn prefixes are resident; first occurrences are cold.",
            "Chat-template overhead, eviction, routing, and actual output lengths can change observed hits.",
        ],
        "actual_cache_hits": "requires server telemetry",
    }
    return scheduled, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.input.read_text().splitlines() if line.strip()]
    try:
        scheduled, report = prepare_trace(rows, args.duration_seconds)
    except ValueError as error:
        parser.error(str(error))
    args.output.write_text("".join(json.dumps(row) + "\n" for row in scheduled))
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"Scheduled {report['sessions']} fresh sessions across {args.duration_seconds}s; "
        f"estimated reusable prompt tokens: {report['estimated_prefix_reuse_pct']:.2f}% "
        "(default target: about 70%; actual hits require server telemetry)."
    )


if __name__ == "__main__":
    main()
