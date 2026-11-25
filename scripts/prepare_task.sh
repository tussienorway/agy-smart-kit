#!/usr/bin/env bash
set -euo pipefail

if [[ "${AGY_PIPELINE_ACTIVE:-0}" == "1" ]]; then
  exit 0
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -eq 0 ]]; then
  exec python3 "$DIR/pipeline.py" --print
fi

printf '%s' "$*" | exec python3 "$DIR/pipeline.py" --print
