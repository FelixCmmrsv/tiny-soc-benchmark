#!/bin/bash
# Brings up Elasticsearch + Kibana (docker-compose.yml at repo root) and loads
# an incident scenario into them, shifting every timestamp so the scenario
# ends "now" (see import_data.py). Re-run any time to refresh dates.
#
# Defaults to the FerrumFox scenario (telemetry/data, index pattern
# "cyberpolygon", default Kibana space). Pass options to load a different
# scenario into a different space, e.g.:
#   ./telemetry/upload.sh --data-dir telemetry/data_supply_chain \
#     --index-pattern-id cyberpolygon-2021 --kibana-space practice_2 \
#     --source-max-ts 2021-06-24T17:54:56
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ELASTICSEARCH_URL="${ELASTICSEARCH_URL:-http://localhost:9200}"
KIBANA_URL="${KIBANA_URL:-http://127.0.0.1:5601}"
DATA_DIR="$SCRIPT_DIR/data"
INDEX_PATTERN_ID="cyberpolygon"
KIBANA_SPACE=""
EXTRA_IMPORT_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --index-pattern-id) INDEX_PATTERN_ID="$2"; shift 2 ;;
    --kibana-space) KIBANA_SPACE="$2"; shift 2 ;;
    --source-max-ts) EXTRA_IMPORT_ARGS+=(--source-max-ts "$2"); shift 2 ;;
    --anchor) EXTRA_IMPORT_ARGS+=(--anchor "$2"); shift 2 ;;
    --recreate) EXTRA_IMPORT_ARGS+=(--recreate); shift 1 ;;
    --total-fields-limit) EXTRA_IMPORT_ARGS+=(--total-fields-limit "$2"); shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker was not found on PATH. Install Docker Desktop first: https://www.docker.com/products/docker-desktop/" >&2
  exit 1
fi

# Pick up ELASTIC_PASSWORD (and anything else) from .env at the repo root so
# import_data.py / index_pattern_manage.py can authenticate without the caller
# having to export it manually.
if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$PROJECT_ROOT/.env"
  set +a
fi

echo "==> Starting Elasticsearch + Kibana (docker compose)..."
(cd "$PROJECT_ROOT" && docker compose up -d --wait)

echo "==> Loading scenario data with shifted (recent) timestamps from $DATA_DIR ..."
python3 "$SCRIPT_DIR/import_data.py" --es-url "$ELASTICSEARCH_URL" --data-dir "$DATA_DIR" "${EXTRA_IMPORT_ARGS[@]}"

KIBANA_TARGET_URL="$KIBANA_URL"
if [ -n "$KIBANA_SPACE" ]; then
  KIBANA_TARGET_URL="$KIBANA_URL/s/$KIBANA_SPACE"
fi

echo "==> Creating/refreshing Kibana index pattern '$INDEX_PATTERN_ID' (space: ${KIBANA_SPACE:-default})..."
python3 "$SCRIPT_DIR/index_pattern_manage.py" --url "$KIBANA_TARGET_URL" -i "$INDEX_PATTERN_ID"

echo "==> Done. Open Kibana at $KIBANA_URL"