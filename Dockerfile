ARG BASE_IMAGE="nvcr.io/nvidia/ai-dynamo/vllm-runtime-nightly:latest"

FROM ${BASE_IMAGE}
USER root

RUN pip install -U --no-cache-dir \
      blake3 \
      cupy-cuda13x \
      ftfy \
      librosa \
      modelexpress \
      nvtx \
      sentencepiece \
      soundfile \
      uvloop

ENTRYPOINT ["/bin/bash"]
