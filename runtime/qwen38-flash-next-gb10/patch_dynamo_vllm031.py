#!/usr/bin/env python3
"""Adapt Dynamo 1.5.1 queue metrics to the vLLM 0.31 scheduler queues.

vLLM replaced skipped_waiting with kv_holding_waiting. Deferred requests
remain in those queues, so iterating deferred_waiting would double-count them.
This affects metrics collection after every engine step, even without Planner.
"""

import ast
import hashlib
from importlib.metadata import version
from pathlib import Path
import sys
import textwrap


NEW_METHOD = '''\
def _compute_queued(self) -> QueuedRequestMetrics:
    """Count each waiting request once across vLLM 0.31's disjoint queues."""
    prefill = WelfordAccumulator()
    decode_kv = WelfordAccumulator()
    for queue in (self.waiting, self.kv_holding_waiting):
        for request in queue:
            if request.status in (
                RequestStatus.PREEMPTED,
                RequestStatus.WAITING_FOR_REMOTE_KVS,
            ):
                decode_kv.add(request.num_computed_tokens)
            else:
                prefill.add(request.num_tokens)
    return QueuedRequestMetrics(
        num_prefill_requests=prefill.n,
        sum_prefill_tokens=prefill.s,
        var_prefill_length=prefill.variance(),
        num_decode_requests=decode_kv.n,
        sum_decode_kv_tokens=decode_kv.s,
        var_decode_kv_tokens=decode_kv.variance(),
    )
'''


def main() -> None:
    if version("ai-dynamo") != "1.5.1" or version("vllm").split("+")[0] != "0.31.0":
        raise SystemExit("Queue adapter requires Dynamo 1.5.1 and vLLM 0.31.0")
    path = Path(sys.argv[1]) / "dynamo/vllm/instrumented_scheduler.py"
    source = path.read_text()
    expected_sha256 = "89547cf5ccf65a5ddd2dab015d8fc35759a472cd53bc441918981eef48749c28"
    if hashlib.sha256(source.encode()).hexdigest() != expected_sha256:
        raise SystemExit("Dynamo scheduler source changed; revalidate the vLLM queue adapter")
    methods = [node for node in ast.walk(ast.parse(source))
               if isinstance(node, ast.FunctionDef) and node.name == "_compute_queued"]
    if len(methods) != 1:
        raise SystemExit("Expected exactly one Dynamo queue metrics method")
    method = methods[0]
    lines = source.splitlines(keepends=True)
    lines[method.lineno - 1:method.end_lineno] = [
        textwrap.indent(NEW_METHOD, " " * method.col_offset)
    ]
    patched = "".join(lines)
    ast.parse(patched)
    path.write_text(patched)
    print("Dynamo scheduler: vLLM 0.31 waiting/kv_holding_waiting adapter applied")


if __name__ == "__main__":
    main()
