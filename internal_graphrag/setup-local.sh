#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

if [[ ! -f .env.graphrag ]]; then
  umask 077
  python3 - <<'PY' > .env.graphrag
import secrets
print("NEO4J_PASSWORD=" + secrets.token_urlsafe(32))
PY
fi
chmod 600 .env.graphrag

docker volume create diario-republica-graphrag_graphrag_neo4j >/dev/null
docker volume create diario-republica-graphrag_graphrag_snapshots >/dev/null

docker compose --env-file .env.graphrag -f docker-compose.graphrag.yml build ingest
docker compose --env-file .env.graphrag -f docker-compose.graphrag.yml up -d neo4j

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
cat > "$unit_dir/diario-republica-graphrag.service" <<EOF
[Unit]
Description=Ingest Diario da Republica into local GraphRAG

[Service]
Type=oneshot
WorkingDirectory=$project_dir
ExecStart=$project_dir/internal_graphrag/run-local.sh
EOF
cat > "$unit_dir/diario-republica-graphrag.timer" <<'EOF'
[Unit]
Description=Daily local Diario da Republica ingestion

[Timer]
OnCalendar=*-*-* 20:30:00 Europe/Lisbon
Persistent=true
Unit=diario-republica-graphrag.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now diario-republica-graphrag.timer
echo "Local GraphRAG started and daily timer enabled."
