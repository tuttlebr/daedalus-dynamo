# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# Extracted unchanged from vLLM 77154ca6116ae34cbfe314beef1016bde9b63989:
# v1/core/sched/scheduler.py and v1/kv_cache_interface.py.
# The installed prefill method and checkpoint predicates reproduce the defect
# without loading vLLM, CUDA, model weights, or a running cluster.
from __future__ import annotations

def get_mamba_prefill_checkpoint_position(
    num_tokens: int,
    hash_block_size: int,
    drop_eagle_block: bool,
) -> int:
    """Return the reusable Mamba checkpoint boundary for a prefill."""
    # Without EAGLE, leave one token to recompute on resend.
    # EAGLE's block drop already leaves tokens to recompute.
    proof_limit = num_tokens if drop_eagle_block else num_tokens - 1
    checkpoint_position = proof_limit // hash_block_size * hash_block_size
    if drop_eagle_block:
        checkpoint_position -= hash_block_size
    return max(checkpoint_position, 0)

def is_mamba_prefill_checkpoint_valid(
    query_start: int,
    query_end: int,
    checkpoint_position: int,
    hash_block_size: int,
    mamba_block_size: int,
    checkpoint_alignment: int | None,
) -> bool:
    """Whether a backend can export the checkpoint in this query."""
    if checkpoint_alignment is None:
        return False
    assert checkpoint_alignment > 0

    initial_state_col = (query_start - 1) // mamba_block_size
    checkpoint_col = cdiv(query_end, mamba_block_size) - 2
    return (
        query_start % hash_block_size == 0
        and checkpoint_col > initial_state_col
        and query_start + hash_block_size <= checkpoint_position
        and query_start < checkpoint_position < query_end
        and (checkpoint_position - query_start) % checkpoint_alignment == 0
    )

def _mamba_block_aligned_split(
        self,
        request: Request,
        num_new_tokens: int,
        num_new_local_computed_tokens: int = 0,
        num_external_computed_tokens: int = 0,
    ) -> int:
        """Clip a prefill chunk so it ends where Mamba state must be cached.

        In "align" cache mode reusable SSM states are materialized at block
        boundaries, plus mandatory early stops (the prompt's partial-tail hash
        boundary, a detected shared-prefix junction). If a block is larger
        than the configured prefill chunk limit, intermediate chunks keep
        private running state until they reach the next cacheable position.
        """
        start = (
            request.num_computed_tokens
            + num_new_local_computed_tokens
            + num_external_computed_tokens
        )
        # Split only during prefill: `request.num_tokens - 1` extends this to
        # resumed requests replaying their output tokens.
        prefill_end = max(request.num_prompt_tokens, request.num_tokens - 1)
        if start >= prefill_end:
            return num_new_tokens

        block_size = self.cache_config.block_size
        # The last block-aligned position whose state can be cached. With
        # Eagle, FullAttn prunes the last matching block, so back off one
        # block to avoid a Mamba cache miss.
        ckpt_block_size = (
            self.hash_block_size if self.mamba_partial_cache_hit else self.block_size
        )
        last_cache_position = get_mamba_prefill_checkpoint_position(
            request.num_tokens,
            block_size,
            self.use_eagle_block_drop,
        )

        end = start + num_new_tokens
        checkpoint_position = get_mamba_prefill_checkpoint_position(
            prefill_end,
            ckpt_block_size,
            drop_eagle_block=self.use_eagle_block_drop,
        )
        use_internal_checkpoint = (
            self.mamba_has_prefill_checkpoint_blocks
            and end >= prefill_end
            and is_mamba_prefill_checkpoint_valid(
                query_start=start,
                query_end=end,
                checkpoint_position=checkpoint_position,
                hash_block_size=self.hash_block_size,
                mamba_block_size=block_size,
                checkpoint_alignment=self.mamba_prefill_checkpoint_alignment,
            )
        )
        if use_internal_checkpoint:
            last_cache_position = 0
        # Invariant: slot p holds the state after exactly (p + 1) * block_size
        # tokens. State is written at chunk ends, so chunk ends must be block
        # aligned. Exempt: the prompt's last chunk, whose slot decode advances
        # to the boundary. A block too wide for one chunk advances sub-block
        # and re-aligns at the next boundary.
        if end < prefill_end:
            max_prefill_tokens = self.max_num_scheduled_tokens
            long_prefill_threshold = self.scheduler_config.long_prefill_token_threshold
            if long_prefill_threshold > 0:
                max_prefill_tokens = min(max_prefill_tokens, long_prefill_threshold)
            aligned_end = end // block_size * block_size
            if aligned_end > start or block_size <= max_prefill_tokens:
                end = aligned_end

        next_block_boundary = (start // block_size + 1) * block_size
        tail_boundary = (
            get_mamba_prefill_checkpoint_position(
                request.num_prompt_tokens,
                self.hash_block_size,
                self.use_eagle_block_drop,
            )
            if self.mamba_partial_cache_hit and not use_internal_checkpoint
            else 0
        )
        junction = request.shared_prefix_boundary
        # Block-floored: a sub-block junction's state is not separately cacheable.
        block_floored = start + (junction - start) // block_size * block_size
        # Past the prompt the manager writes nothing, so fall back to the
        # block-floored stop rather than dropping it: a resumed request replaying
        # output tokens can still observe a junction there.
        junction_stop = (
            junction
            if self.mamba_shared_prefix_checkpoint
            and junction <= request.num_prompt_tokens
            else block_floored
        )
        stops = (
            # Same invariant: a chunk starting mid-block stops at the boundary
            # rather than running past it.
            next_block_boundary
            if start % block_size != 0 and not use_internal_checkpoint
            else 0,
            # Never run past the last cacheable block boundary mid-chunk.
            last_cache_position,
            # Stop at the prompt checkpoint unless the backend saves it internally.
            tail_boundary
            if last_cache_position < tail_boundary < request.num_prompt_tokens
            else 0,
            # Marconi shared-prefix junction: cache its state so sibling
            # requests sharing the prefix can reuse it.
            junction_stop if start < junction < end else 0,
        )
        # Stop at the earliest mandatory position strictly inside the chunk.
        end = min((s for s in stops if start < s < end), default=end)
        return max(end - start, 0)
