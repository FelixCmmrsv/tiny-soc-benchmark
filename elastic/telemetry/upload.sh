#!/bin/bash
# Loads an incident scenario into Elasticsearch + Kibana, shifting every
# timestamp so the scenario ends "now" (see import_data.py). By default it
# also brings the local Docker stack up first; pass --no-compose to skip that
# and target an already-running / remote cluster instead. Re-run any time to
# refresh dates.
#
# Connection (host/port/auth) is configured via .env and/or environment, NOT
# via flags here -- see .env.example. Precedence for the URLs: caller-set
# environment (e.g. exported by the orchestrator) > .env > built-in localhost
# default. Auth (ES_API_KEY, or ES_USER/ES_PASSWORD/ELASTIC_PASSWORD) is read
# from .env / environment by import_data.py and index_pattern_manage.py.
#
# Examples:
#   ./telemetry/upload.sh --data-dir telemetry/data_supply_chain \
#     --index-pattern-id cyberpolygon-2021 --kibana-space practice_2 \
#     --source-max-ts 2021-06-24T17:54:56
#   ELASTICSEARCH_URL=https://es.internal:9200 KIBANA_URL=https://kibana.internal:5601 \
#     ./telemetry/upload.sh --no-compose --recreate
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Capture any caller-provided URLs before sourcing .env, so an explicit
# override (from the orchestrator, or `ELASTICSEARCH_URL=... ./upload.sh`)
# wins over whatever .env happens to set.
_CALLER_ES_URL="${ELASTICSEARCH_URL:-}"
_CALLER_KIBANA_URL="${KIBANA_URL:-}"

DATA_DIR="$SCRIPT_DIR/data"
INDEX_PATTERN_ID="cyberpolygon"
KIBANA_SPACE=""
NO_COMPOSE=0
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
    --no-compose) NO_COMPOSE=1; shift 1 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# Pick up connection URLs, ELASTIC_PASSWORD, ES_API_KEY, etc. from .env so
# import_data.py / index_pattern_manage.py can authenticate without the caller
# having to export anything manually.
if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$PROJECT_ROOT/.env"
  set +a
fi

# Final URLs: caller env > .env > built-in local default.
ELASTICSEARCH_URL="${_CALLER_ES_URL:-${ELASTICSEARCH_URL:-http://localhost:9200}}"
KIBANA_URL="${_CALLER_KIBANA_URL:-${KIBANA_URL:-http://127.0.0.1:5601}}"

if [ "$NO_COMPOSE" -eq 0 ]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker was not found on PATH. Install Docker Desktop first (https://www.docker.com/products/docker-desktop/), or pass --no-compose to target an already-running cluster." >&2
    exit 1
  fi
  echo "==> Starting Elasticsearch + Kibana (docker compose)..."
  (cd "$PROJECT_ROOT" && docker compose up -d --wait)
else
  echo "==> --no-compose: using existing Elasticsearch at $ELASTICSEARCH_URL (Kibana at $KIBANA_URL)"
fi

echo "==> Loading scenario data with shifted (recent) timestamps from $DATA_DIR ..."
python3 "$SCRIPT_DIR/import_data.py" --es-url "$ELASTICSEARCH_URL" --data-dir "$DATA_DIR" "${EXTRA_IMPORT_ARGS[@]}"

KIBANA_TARGET_URL="$KIBANA_URL"
if [ -n "$KIBANA_SPACE" ]; then
  KIBANA_TARGET_URL="$KIBANA_URL/s/$KIBANA_SPACE"
fi

echo "==> Creating/refreshing Kibana index pattern '$INDEX_PATTERN_ID' (space: ${KIBANA_SPACE:-default})..."
python3 "$SCRIPT_DIR/index_pattern_manage.py" --url "$KIBANA_TARGET_URL" -i "$INDEX_PATTERN_ID"

echo "==> Done. Open Kibana at $KIBANA_URL"
