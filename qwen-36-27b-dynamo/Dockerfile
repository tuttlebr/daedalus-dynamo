# syntax=docker/dockerfile:1.10.0
ARG BASE_IMAGE="nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.0-dev.1-cuda13"

FROM ${BASE_IMAGE}
USER root

RUN pip install --no-cache-dir \
      blake3 librosa soundfile uvloop \
      av ftfy nvtx sentencepiece modelexpress cupy-cuda13x

ENTRYPOINT ["/bin/bash"]
