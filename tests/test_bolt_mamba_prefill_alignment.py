"""Reproduce the installed vLLM checkpoint failure without a GPU or cluster."""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "runtime/nemotron35bolt-gb10/patch_mamba_prefill_alignment.py"
SPEC = importlib.util.spec_from_file_location("bolt_mamba_prefill_patch", PATCH)
assert SPEC and SPEC.loader
patch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patch)
SOURCE = (ROOT / "tests/fixtures/vllm_mamba_prefill_alignment.py").read_text()


def functions(source: str) -> dict:
    namespace = {"cdiv": lambda a, b: (a + b - 1) // b}
    exec(compile(source, "vllm-prefill-fixture", "exec"), namespace)
    return namespace


def simulate(source: str, length: int, *, draft_block_size: int = 64,
             partial_hits: bool = True) -> dict:
    namespace = functions(source)
    mamba_block_size = 4496
    group_sizes = [mamba_block_size, draft_block_size]
    hash_size = math.gcd(*group_sizes)
    scheduler = SimpleNamespace(
        cache_config=SimpleNamespace(block_size=min(group_sizes), mamba_block_size=mamba_block_size),
        block_size=math.lcm(*group_sizes), hash_block_size=hash_size,
        mamba_partial_cache_hit=partial_hits and hash_size < mamba_block_size,
        use_eagle_block_drop=True, mamba_has_prefill_checkpoint_blocks=True,
        mamba_prefill_checkpoint_alignment=1, max_num_scheduled_tokens=8192,
        scheduler_config=SimpleNamespace(long_prefill_token_threshold=1024),
        mamba_shared_prefix_checkpoint=False,
    )
    request = SimpleNamespace(num_computed_tokens=0, num_tokens=length,
                              num_prompt_tokens=length, shared_prefix_boundary=0)
    alignment = hash_size if scheduler.mamba_partial_cache_hit else scheduler.block_size
    checkpoint = namespace["get_mamba_prefill_checkpoint_position"](length, alignment, True)
    ends = []
    exported = False
    while request.num_computed_tokens < length:
        start = request.num_computed_tokens
        count = namespace["_mamba_block_aligned_split"](
            scheduler, request, min(1024, length - start)
        )
        if not 0 < count <= min(1024, length - start):
            raise AssertionError(f"invalid prefill chunk {count}")
        request.num_computed_tokens += count
        end = request.num_computed_tokens
        ends.append(end)
        # The allocator/exporter checks the actual Mamba block size, not the
        # draft minimum or scheduler LCM, when exporting inside the last chunk.
        if end == length and namespace["is_mamba_prefill_checkpoint_valid"](
            query_start=start, query_end=end, checkpoint_position=checkpoint,
            hash_block_size=hash_size, mamba_block_size=mamba_block_size,
            checkpoint_alignment=1,
        ):
            exported = True
    return {"checkpoint": checkpoint, "ends": ends,
            "checkpoint_available": checkpoint > 0 and (checkpoint in ends or exported)}


class BoltMambaPrefillAlignmentTests(unittest.TestCase):
    def test_mixed_blocks_with_partial_hits_retain_checkpoints_after_fix(self) -> None:
        # Prompt lengths from the live diagnostic, with a compatible draft
        # manager. Live DFlash SWA disables this mode; see the fallback test.
        patched = patch.patch_source(SOURCE)
        for length in [2608, 5821, 9034, 12247, 15460]:
            with self.subTest(prompt_tokens=length):
                before = simulate(SOURCE, length)
                after = simulate(patched, length)
                self.assertFalse(before["checkpoint_available"], before)
                self.assertTrue(after["checkpoint_available"], after)
                self.assertEqual(after["ends"][-1], length)

    def test_sliding_window_fallback_still_cannot_reuse_sub_lcm_prompts(self) -> None:
        # SlidingWindowManager disables partial hits in the deployed runtime.
        # The scheduler correction cannot remove that independent restriction.
        for source in [SOURCE, patch.patch_source(SOURCE)]:
            for length in [2608, 5821, 9034, 12247, 15460]:
                with self.subTest(patched=source != SOURCE, prompt_tokens=length):
                    result = simulate(source, length, partial_hits=False)
                    self.assertEqual(result["checkpoint"], 0)
                    self.assertFalse(result["checkpoint_available"])

    def test_equal_group_block_sizes_keep_existing_prefill_scheduling(self) -> None:
        patched = patch.patch_source(SOURCE)
        for length in [2608, 9034, 15460]:
            with self.subTest(prompt_tokens=length):
                self.assertEqual(simulate(SOURCE, length, draft_block_size=4496),
                                 simulate(patched, length, draft_block_size=4496))

    def test_changed_source_requires_review(self) -> None:
        with self.assertRaises(ValueError):
            patch.patch_source(SOURCE.replace("block_size = self.cache_config.block_size", "block_size = 64"))


if __name__ == "__main__":
    unittest.main()
