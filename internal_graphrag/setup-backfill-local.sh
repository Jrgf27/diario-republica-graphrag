#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 YYYY-MM-DD YYYY-MM-DD" >&2
  exit 2
fi

python3 - "$1" "$2" <<'PY'
from datetime import date
import sys
start, end = map(date.fromisoformat, sys.argv[1:])
if start > end or end > date.today():
    raise SystemExit("Invalid backfill date range")
PY

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

docker compose --env-file .env.graphrag -f docker-compose.graphrag.yml \
  --profile archive build archive

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
cat > "$unit_dir/diario-republica-graphrag-backfill.service" <<EOF
[Unit]
Description=Resume local Diario da Republica historical backfill

[Service]
Type=simple
WorkingDirectory=$project_dir
ExecStart=$project_dir/internal_graphrag/run-archive-local.sh --from $1 --to $2
Restart=on-failure
RestartSec=10min
TimeoutStartSec=0

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now --no-block diario-republica-graphrag-backfill.service
echo "Local backfill started. Follow it with journalctl --user -fu diario-republica-graphrag-backfill.service"
