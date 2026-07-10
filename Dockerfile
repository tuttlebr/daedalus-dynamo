ARG BASE_IMAGE="nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.0-dev.1-cuda13"

FROM ${BASE_IMAGE}
USER root

RUN pip install --no-cache-dir \
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
