# Elastic infrastructure

Docker Compose stack + loader scripts for the Elasticsearch/Kibana backend
scenarios run against. This is generic tooling (works with any scenario's
telemetry), not specific content — bundled directly in this repo.

**What's NOT here:** the actual scenario telemetry (`telemetry/data*/*.json`)
and the real `.env`. Both are gitignored. The telemetry is scenario content
(in the reference deployment, sourced from BiZone Cyberpolygon) — this repo
ships the tooling to load and serve it, not the data itself. Supply your own
per scenario, matching the `data_dir` your `manifest.source.yaml` points at.

## Setup

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/),
   start it, wait for the "Engine running" indicator. In Settings →
   Resources, give it at least ~4GB RAM for Elasticsearch.
2. `cp .env.example .env` and fill in `ELASTIC_PASSWORD`/`KIBANA_SYSTEM_PASSWORD`
   (any strong values — these just initialize the local cluster). Leave
   `ES_API_KEY` blank for now.
3. Bring the stack up once by hand to generate the API key:
   ```bash
   docker compose up -d --wait
   ```
   This pulls `docker.elastic.co/elasticsearch/elasticsearch:9.3.5` and
   `docker.elastic.co/kibana/kibana:9.3.5` (public images, no registry auth
   needed) — a few hundred MB, one-time.
4. Create the API key `import_data.py`/`index_pattern_manage.py` authenticate
   with (see the command in `.env.example`'s comment), put it in `.env` as
   `ES_API_KEY`.
5. Put your scenario telemetry JSON under `telemetry/data/` (or wherever
   your manifest's `data_dir` points).

After that, `harness/orchestrator/run_benchmark.py` drives everything else
(reload, timestamp shifting, index pattern creation) automatically — you
won't need to touch `upload.sh` directly except to debug.

## Manual invocation (for debugging)

```bash
./telemetry/upload.sh --data-dir telemetry/data --index-pattern-id cyberpolygon --recreate
```

Kibana: http://localhost:5601 → Discover → your index pattern. Elasticsearch:
http://localhost:9200 (basic auth, user `elastic`).
