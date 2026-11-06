# syntax=docker/dockerfile:1
# Preserve the audited Dynamo/vLLM pair on both frontend and GPU architectures.
FROM nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.6.1@sha256:5899e7bdd3528ab09a17ec87e2abe1949f8a0386a0a49f3b24dbd52d8211dc5d
USER root

COPY patch_tool_handoff.py /opt/muse-glimmer/patch_tool_handoff.py
RUN python3 /opt/muse-glimmer/patch_tool_handoff.py
COPY test_tool_handoff.py /opt/muse-glimmer/test_tool_handoff.py

LABEL io.daedalus.muse-glimmer.fix="dynamo-1.6.1-streaming-tool-handoff-v1"
USER dynamo
