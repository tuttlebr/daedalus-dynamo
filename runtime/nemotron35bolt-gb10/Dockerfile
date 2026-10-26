# syntax=docker/dockerfile:1
# NVIDIA's Bolt model-card minimum, pinned to its multi-architecture index.
FROM vllm/vllm-openai:nightly-77154ca6116ae34cbfe314beef1016bde9b63989@sha256:a7deff2c216def98c193352aca50fb03a2ee29231355b3033dc3789009f6d23e
USER root

# Preserve the model-capable vLLM/Torch stack instead of installing [vllm].
RUN pip install --no-cache-dir \
      ai-dynamo==1.6.0.dev20261019 \
      b12x==1.5.1 \
      blake3==1.0.8 uvloop==0.22.1 \
 && python3 -c 'import b12x; from importlib.metadata import version; print("vllm", version("vllm")); print("dynamo", version("ai-dynamo"))'

# AI Simulate's unbounded grpcio-tools dependency otherwise upgrades protobuf
# beyond the version range required by this base's telemetry and LMCache.
RUN pip install --no-cache-dir protobuf==6.33.6 grpcio-tools==1.76.0 grpcio==1.84.0 \
 && python3 -c 'from importlib.metadata import version; assert version("torch") == "2.13.0+cu130"; assert version("vllm") == "0.32.0rc1.dev100+g77154ca61"'

# DFlash adds a 64-token cache group alongside 4,496-token Mamba state blocks.
# The scheduler must use the state block size to decide where it can export
# a reusable checkpoint, matching the allocator and model runner.
COPY patch_mamba_prefill_alignment.py /tmp/patch_mamba_prefill_alignment.py
RUN python3 /tmp/patch_mamba_prefill_alignment.py \
 && rm /tmp/patch_mamba_prefill_alignment.py

WORKDIR /workspace
ENTRYPOINT ["/bin/bash"]
