"""Elastic data lifecycle for a benchmark run. Thin wrapper around
harness/elastic/telemetry/upload.sh (bundled infra tooling -- see that
directory's own notes on why the actual scenario telemetry data is NOT
bundled alongside it). upload.sh resolves its own PROJECT_ROOT from its own
script path and sources harness/elastic/.env itself, so this can be invoked
from any cwd.

Connection settings (host/port/auth) live in harness/elastic/.env; this
module reads that file to (a) tell the agent where Elasticsearch is and how
to authenticate, and (b) pass the resolved URLs through to upload.sh so the
data loader targets the exact same cluster the agent is told about.
"""
import shutil
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS = REPO_ROOT / "harness"
ELASTIC_DIR = HARNESS / "elastic"
UPLOAD_SH = ELASTIC_DIR / "telemetry" / "upload.sh"
ENV_FILE = ELASTIC_DIR / ".env"

DEFAULT_ES_URL = "http://localhost:9200"
DEFAULT_KIBANA_URL = "http://127.0.0.1:5601"


class ElasticLifecycleError(RuntimeError):
    pass


def load_env(path=ENV_FILE):
    """Parse harness/elastic/.env into a dict (KEY=VALUE lines, # comments
    ignored). Returns {} if absent. This is the single source of truth for
    the ES/Kibana connection + credentials across the harness."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def connection(cli_es_url=None, cli_kibana_url=None):
    """Resolve the effective ES/Kibana connection. Precedence: explicit CLI
    override > .env > built-in localhost default. Also surfaces the auth
    method so the agent prompt can tell the model how to authenticate."""
    env = load_env()
    es_url = cli_es_url or env.get("ELASTICSEARCH_URL") or env.get("ES_URL") or DEFAULT_ES_URL
    kibana_url = cli_kibana_url or env.get("KIBANA_URL") or DEFAULT_KIBANA_URL
    api_key = env.get("ES_API_KEY")
    user = env.get("ES_USER") or ("elastic" if env.get("ELASTIC_PASSWORD") else None)
    password = env.get("ES_PASSWORD") or env.get("ELASTIC_PASSWORD")
    if api_key:
        auth_method = "apikey"
    elif user and password:
        auth_method = "basic"
    else:
        auth_method = "none"
    return {
        "es_url": es_url,
        "kibana_url": kibana_url,
        "auth_method": auth_method,
        "es_api_key": api_key,
        "es_user": user,
        "es_password": password,
    }


def docker_available():
    if not shutil.which("docker"):
        return False, "docker CLI not found on PATH"
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=15)
    except Exception as e:
        return False, "docker info failed: %r" % e
    if r.returncode != 0:
        return False, "docker daemon not reachable (is Docker Desktop running?)"
    return True, ""


def reset_scenario_data(manifest, anchor, es_url=None, kibana_url=None, no_compose=False):
    """Reload the scenario's telemetry into Elasticsearch with --recreate, per
    the manifest's `elastic:` block. Raises ElasticLifecycleError on failure.

    es_url / kibana_url, when given, are passed through to upload.sh (as
    environment) so the loader targets the same cluster the agent is told
    about; when None, upload.sh falls back to .env / its localhost default.
    no_compose skips the local `docker compose up` for an existing/remote
    cluster.

    `anchor` is REQUIRED and must be the same ISO timestamp the caller also
    hands to the proctor (see proctor_server.py's materialize_shifted_timestamps)
    -- otherwise the data gets shifted by one delta while grading computes a
    different one, and any shift_from_source timestamp question grades wrong
    even though the agent answered correctly.
    """
    es_cfg = manifest.get("elastic")
    if not es_cfg:
        raise ElasticLifecycleError("manifest has no 'elastic' block: %s" % manifest.get("scenario_id"))
    if not anchor:
        raise ElasticLifecycleError("reset_scenario_data requires an explicit anchor")

    if not UPLOAD_SH.exists():
        raise ElasticLifecycleError("upload.sh not found at %s" % UPLOAD_SH)

    cmd = [str(UPLOAD_SH), "--data-dir", es_cfg["data_dir"], "--index-pattern-id", es_cfg["index_pattern_id"]]
    if es_cfg.get("kibana_space"):
        cmd += ["--kibana-space", es_cfg["kibana_space"]]
    if es_cfg.get("source_max_ts"):
        cmd += ["--source-max-ts", es_cfg["source_max_ts"]]
    cmd += ["--anchor", anchor]
    cmd += ["--recreate"]
    if no_compose:
        cmd += ["--no-compose"]

    import os
    sub_env = dict(os.environ)
    if es_url:
        sub_env["ELASTICSEARCH_URL"] = es_url
    if kibana_url:
        sub_env["KIBANA_URL"] = kibana_url

    print("[elastic_lifecycle] $ %s" % " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(ELASTIC_DIR), env=sub_env)
    if r.returncode != 0:
        raise ElasticLifecycleError("upload.sh exited %d" % r.returncode)


def es_reachable(es_url=DEFAULT_ES_URL, timeout=5):
    try:
        with urllib.request.urlopen(es_url, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except urllib.error.HTTPError as e:
        # ES answers 401 without auth on a secured cluster -- still "reachable"
        return e.code in (401, 403)
    except Exception:
        return False
