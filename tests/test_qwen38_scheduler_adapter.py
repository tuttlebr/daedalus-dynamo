from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "runtime/qwen38-flash-next-gb10/patch_dynamo_vllm031.py"
spec = importlib.util.spec_from_file_location("qwen38_scheduler_patch", PATCH)
assert spec and spec.loader
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


class Accumulator:
    def __init__(self) -> None:
        self.values: list[int] = []

    def add(self, value: int) -> None:
        self.values.append(value)

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def s(self) -> int:
        return sum(self.values)

    def variance(self) -> float:
        if not self.values:
            return 0.0
        mean = self.s / self.n
        return sum((x - mean) ** 2 for x in self.values) / self.n


STATUS = SimpleNamespace(PREEMPTED="preempted", WAITING_FOR_REMOTE_KVS="remote")
namespace = {"QueuedRequestMetrics": SimpleNamespace,
             "WelfordAccumulator": Accumulator, "RequestStatus": STATUS}
exec(compile(patch.NEW_METHOD, str(PATCH), "exec"), namespace)
compute_queued = namespace["_compute_queued"]


class Qwen38SchedulerAdapterTests(unittest.TestCase):
    def test_empty_vllm_031_queues_need_no_legacy_skipped_queue(self) -> None:
        scheduler = SimpleNamespace(waiting=[], kv_holding_waiting=[])
        result = compute_queued(scheduler)
        self.assertEqual(result.num_prefill_requests, 0)
        self.assertEqual(result.num_decode_requests, 0)

    def test_counts_preemption_remote_kv_and_deferred_requests_once(self) -> None:
        grammar = SimpleNamespace(status="grammar", num_tokens=40)
        scheduler = SimpleNamespace(
            waiting=[SimpleNamespace(status="waiting", num_tokens=20), grammar],
            kv_holding_waiting=[
                SimpleNamespace(status=STATUS.PREEMPTED, num_computed_tokens=30),
                SimpleNamespace(status=STATUS.WAITING_FOR_REMOTE_KVS, num_computed_tokens=50),
            ],
            deferred_waiting=[grammar],
        )
        result = compute_queued(scheduler)
        self.assertEqual(result.num_prefill_requests, 2)
        self.assertEqual(result.sum_prefill_tokens, 60)
        self.assertEqual(result.var_prefill_length, 100)
        self.assertEqual(result.num_decode_requests, 2)
        self.assertEqual(result.sum_decode_kv_tokens, 80)
        self.assertEqual(result.var_decode_kv_tokens, 100)

    def test_classifies_by_request_state_in_either_queue(self) -> None:
        scheduler = SimpleNamespace(
            waiting=[SimpleNamespace(status=STATUS.WAITING_FOR_REMOTE_KVS,
                                     num_computed_tokens=64)],
            kv_holding_waiting=[SimpleNamespace(status="waiting", num_tokens=128)],
        )
        result = compute_queued(scheduler)
        self.assertEqual(result.num_decode_requests, 1)
        self.assertEqual(result.sum_decode_kv_tokens, 64)
        self.assertEqual(result.num_prefill_requests, 1)
        self.assertEqual(result.sum_prefill_tokens, 128)
