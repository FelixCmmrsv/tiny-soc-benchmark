"""Elastic data lifecycle for a benchmark run. Thin wrapper around
harness/elastic/telemetry/upload.sh (bundled infra tooling -- see that
directory's own notes on why the actual scenario telemetry data is NOT
bundled alongside it). upload.sh resolves its own PROJECT_ROOT from its own
script path and sources harness/elastic/.env itself, so this can be invoked
from any cwd.
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


class ElasticLifecycleError(RuntimeError):
    pass


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


def reset_scenario_data(manifest, anchor, es_url="http://localhost:9200", kibana_url="http://127.0.0.1:5601"):
    """Reload the scenario's telemetry into Elasticsearch with --recreate, per
    the manifest's `elastic:` block. Raises ElasticLifecycleError on failure.

    `anchor` is REQUIRED and must be the same ISO timestamp the caller also
    hands to the proctor (see proctor_server.py's materialize_shifted_timestamps)
    -- otherwise the data gets shifted by one delta while grading computes a
    different one, and any shift_from_source timestamp question grades wrong
    even though the agent answered correctly. Don't let upload.sh silently
    default this to its own "now" -- that "now" and this process's "now" can
    differ by however long Elastic reset took to run.
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

    print("[elastic_lifecycle] $ %s" % " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(ELASTIC_DIR))
    if r.returncode != 0:
        raise ElasticLifecycleError("upload.sh exited %d" % r.returncode)


def es_reachable(es_url="http://localhost:9200", timeout=5):
    try:
        with urllib.request.urlopen(es_url, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except urllib.error.HTTPError as e:
        # ES answers 401 without auth on a secured cluster -- still "reachable"
        return e.code in (401, 403)
    except Exception:
        return False
