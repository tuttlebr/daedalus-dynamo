# syntax=docker/dockerfile:1
# ARM64 image index; includes vLLM's Qwen4Exp implementation and SM121 kernels.
FROM vllm/vllm-openai:v0.31.0@sha256:c1c9f6fd5c109ba7f0546a59f5b2f15fb87f64c77782e90a27b648b42a8e67c3
USER root
ARG SP=/usr/local/lib/python3.12/dist-packages
ARG PKG=${SP}/vllm/models/qwen4_exp/nvidia

# Keep the pinned engine and GB10 patches supplied by this build. Dynamo 1.6.1
# already supports vLLM 0.31 scheduler queues; no local Dynamo adapter is needed.
# Keep AI Simulate's gRPC tooling within the base telemetry's protobuf < 7 bound.
RUN pip install --no-cache-dir ai-dynamo==1.6.1 modelexpress==0.7.0 blake3 uvloop \
      protobuf==6.33.6 grpcio-tools==1.76.0 grpcio==1.84.0 \
 && python3 -c 'from importlib.metadata import version; assert version("vllm").split("+")[0] == "0.31.0"'

COPY vllm_ple_mmap.py ${SP}/vllm_ple_mmap.py
RUN printf '\nfrom vllm_ple_mmap import apply as _ple_mmap_apply\n_ple_mmap_apply(Qwen4ExpNGramEmbedding)\n' >> ${PKG}/ngram_embedding.py

# Fix GB10 shared-memory sizing and the FLA two-warp race. Fail if the pinned
# source layout changes instead of silently producing an unpatched runtime.
RUN sed -i 's/DEFAULT = 102400/DEFAULT = 101376  # spark-fla-shmem/' ${SP}/vllm/third_party/flash_linear_attention/ops/utils.py \
 && grep -q spark-fla-shmem ${SP}/vllm/third_party/flash_linear_attention/ops/utils.py \
 && sed -i 's/for num_warps in \[2, 4\]/for num_warps in [2]  # spark-fla-warps/' ${SP}/vllm/third_party/flash_linear_attention/ops/chunk_delta_h.py \
 && grep -q spark-fla-warps ${SP}/vllm/third_party/flash_linear_attention/ops/chunk_delta_h.py

COPY patch_*.py /tmp/gb10-patches/
RUN python3 /tmp/gb10-patches/patch_mamba_block_size.py ${SP} \
 && python3 /tmp/gb10-patches/patch_qsa_exact_topk.py ${PKG}/ops/qsa_indexer.py \
 && python3 /tmp/gb10-patches/patch_moe_load_clone.py ${SP} \
 && python3 /tmp/gb10-patches/patch_load_pread.py ${SP} \
 && python3 /tmp/gb10-patches/patch_embed_chunked_copy.py ${SP} \
 && python3 /tmp/gb10-patches/patch_mtp_name_prefilter.py ${SP} \
 && rm -r /tmp/gb10-patches

LABEL io.daedalus.gb10.patch-source="https://github.com/blazux/qwen3.8-flash-DGX" \
      io.daedalus.gb10.patch-revision="a9ec989cdaaa813fcd3755ba87ff6c43ed7c2156"
ENTRYPOINT ["/bin/bash"]
