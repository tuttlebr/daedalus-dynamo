#!/usr/bin/env bash
# Convenience wrapper for the integrated repo-level deploy script.

set -euo pipefail

cd "$(dirname "$0")/.."
exec ./deploy.sh "$@"
