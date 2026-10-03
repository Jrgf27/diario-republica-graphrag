#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ! -f $1 ]]; then
  echo "Usage: $0 path/to/official-pdf-urls.txt" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
urls=()
count=0
while IFS= read -r line || [[ -n $line ]]; do
  line="${line%$'\r'}"
  [[ -z ${line//[[:space:]]/} || $line == \#* ]] && continue
  urls+=(--url "$line")
  count=$((count + 1))
  if (( count == 25 )); then
    "$script_dir/run-local.sh" python -m internal_graphrag.cli ingest "${urls[@]}" --limit 25
    urls=()
    count=0
  fi
done < "$1"

if (( count > 0 )); then
  "$script_dir/run-local.sh" python -m internal_graphrag.cli ingest "${urls[@]}" --limit 25
fi
