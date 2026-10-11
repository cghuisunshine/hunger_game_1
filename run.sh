#!/usr/bin/env bash
set -euo pipefail

python3 -m tools.hunger_reader_pipeline all "$@"
