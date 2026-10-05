#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

if [[ ! -f .env.graphrag ]]; then
  echo "Missing .env.graphrag; run ./internal_graphrag/setup-local.sh first" >&2
  exit 1
fi

docker compose --env-file .env.graphrag -f docker-compose.graphrag.yml \
  --profile web --profile api up --build -d web api
echo "Diário Graph is available at http://127.0.0.1:18501"
echo "Agent API is available at http://127.0.0.1:18502"
