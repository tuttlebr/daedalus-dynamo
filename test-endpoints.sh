#!/bin/bash

set -euo pipefail

mkdir -p artifacts/aiperf
docker compose -f benchmarks/aiperf/docker-compose.yaml build
AIPERF_UID="$(id -u)" AIPERF_GID="$(id -g)" \
    docker compose -f benchmarks/aiperf/docker-compose.yaml run --rm aiperf
