FROM ghcr.io/astral-sh/uv:0.10.4-python3.12-trixie-slim

LABEL org.opencontainers.image.title="Daedalus Dynamo AIPerf client" \
      org.opencontainers.image.description="Multi-turn prefix-reuse benchmark for inference-gateway models" \
      org.opencontainers.image.version="0.13.0" \
      io.daedalus.aiperf.source-commit="794f8bb75f8582f22e412d7e650fc71ca2a3d21a"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PATH="/opt/aiperf/.venv/bin:${PATH}" \
    HOME=/tmp \
    HF_HOME=/tmp/huggingface

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates coreutils curl jq \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/aiperf
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chmod=0755 run-model-benchmarks.sh ./run-model-benchmarks.sh
COPY --chmod=0755 run-kubernetes-container.sh ./run-kubernetes-container.sh
COPY agentic-code-config.json ./agentic-code-config.json
COPY prepare-agentic-trace.py ./prepare-agentic-trace.py

RUN mkdir -p /artifacts /tmp/huggingface \
    && chown -R 65532:65532 /artifacts /tmp/huggingface

USER 65532:65532
VOLUME ["/artifacts"]
ENTRYPOINT ["/opt/aiperf/run-model-benchmarks.sh"]
