#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

if [[ ! -f .env.graphrag ]]; then
  echo "Missing .env.graphrag; run ./internal_graphrag/setup-local.sh first" >&2
  exit 1
fi

exec docker compose --env-file .env.graphrag -f docker-compose.graphrag.yml \
  --profile archive run --rm archive \
  python -u -m internal_graphrag.archive "$@"
