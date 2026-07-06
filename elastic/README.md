# Elastic infrastructure

Docker Compose stack + loader scripts for the Elasticsearch/Kibana backend
scenarios run against. This is generic tooling (works with any scenario's
telemetry), not specific content — bundled directly in this repo.

**What's NOT here:** the actual scenario telemetry (`telemetry/data*/*.json`)
and the real `.env`. Both are gitignored. The telemetry is scenario content
(in the reference deployment, sourced from BiZone Cyberpolygon) — this repo
ships the tooling to load and serve it, not the data itself. Supply your own
per scenario, matching the `data_dir` your `manifest.source.yaml` points at.

All connection settings (host, port, auth) live in one file, `.env` (copy it
from `.env.example`). Both the loader scripts and the benchmark orchestrator
read it, so you configure the cluster in exactly one place.

## Option A — local Docker stack (default, simplest)

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/),
   start it, wait for "Engine running". In Settings → Resources give it at
   least ~4 GB RAM for Elasticsearch.
2. `cp .env.example .env` and set `ELASTIC_PASSWORD` / `KIBANA_SYSTEM_PASSWORD`
   to any strong values (these initialize the local cluster). Leave
   `ES_API_KEY` blank for now; leave the `ELASTICSEARCH_URL` / `KIBANA_URL`
   lines commented (localhost defaults apply).
3. Bring the stack up once, to create an API key:
   ```bash
   docker compose up -d --wait
   ```
   Pulls `docker.elastic.co/elasticsearch/elasticsearch:9.3.5` and
   `docker.elastic.co/kibana/kibana:9.3.5` (public images, no registry login
   needed) — a few hundred MB, one-time.
4. Create the API key the loader uses (command is in `.env.example`), and put
   its `encoded` value in `.env` as `ES_API_KEY`.
5. Drop your scenario telemetry JSON under `telemetry/data/` (or wherever your
   manifest's `data_dir` points).

After that, `run_benchmark.py` starts the stack, reloads data, shifts
timestamps, and creates the index pattern automatically each run — you won't
touch `upload.sh` directly except to debug.

## Option B — existing / remote cluster

Point the harness at a cluster you already run (self-managed, Elastic Cloud,
a shared dev cluster) instead of the local Docker stack:

1. In `.env`, set the URLs and auth:
   ```
   ELASTICSEARCH_URL=https://es.your-host:9200
   KIBANA_URL=https://kibana.your-host:5601
   ES_API_KEY=<encoded key with cluster+index+kibana privileges>
   ```
   (or basic auth via `ES_USER` / `ES_PASSWORD` — see `.env.example`).
2. Run the benchmark with `--no-compose` so it doesn't try to start local
   containers:
   ```bash
   python3 orchestrator/run_benchmark.py --scenario <id> --model <name> --no-compose
   ```
   You can also override per run without editing `.env`:
   `--es-url https://... --kibana-url https://...`.

Precedence for the URLs is: `--es-url`/`--kibana-url` flags → `.env` →
built-in localhost default.

## Authentication methods

Configured entirely in `.env`; the loader and the agent both pick it up:

- **API key** (recommended): set `ES_API_KEY`. Sent as `Authorization: ApiKey …`.
- **HTTP basic auth**: leave `ES_API_KEY` blank, set `ES_USER` / `ES_PASSWORD`
  (for the local stack, `ES_USER` defaults to `elastic` with `ELASTIC_PASSWORD`,
  so you usually need neither).

The agent's task prompt is generated to match whichever method is configured,
so the model is told the right way to authenticate to Elasticsearch.

## Manual invocation (debugging)

```bash
# local stack:
./telemetry/upload.sh --data-dir telemetry/data --index-pattern-id cyberpolygon --recreate

# external cluster (URLs from .env or exported):
ELASTICSEARCH_URL=https://es.your-host:9200 KIBANA_URL=https://kibana.your-host:5601 \
  ./telemetry/upload.sh --no-compose --data-dir telemetry/data --index-pattern-id cyberpolygon --recreate
```

Kibana UI: your `KIBANA_URL` → Discover → your index pattern.

## Troubleshooting

- **`docker daemon not reachable`** — Docker Desktop isn't running, or you
  meant to target an external cluster: start Docker, or pass `--no-compose`.
- **Elasticsearch reachable but 401/403** — wrong or missing `ES_API_KEY` /
  credentials in `.env`, or the key lacks privileges (it needs cluster + index
  access, plus the Kibana `applications` privilege block for the index-pattern
  step; see `.env.example`).
- **Kibana 500s on every page (local stack)** — a known Kibana 9.4.x bug with
  security disabled; this stack pins 9.3.5 to avoid it. If you bump the image
  tag and hit it, roll back to 9.3.5.
- **Index-pattern step fails but data loaded** — usually the API key is
  missing the Kibana `applications` privilege; recreate it with the full
  `role_descriptors` from `.env.example`.
