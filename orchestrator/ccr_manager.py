"""Per-run claude-code-router (ccr) instance: every non-native model runs
through the SAME `claude` CLI + Claude Code engine as claude-sonnet-5 -- ccr
only swaps which backend API answers the request -- so every model under
test gets identical tools/skills, not a different harness per vendor.

Confirmed live (not assumed): `ccr start` runs in the FOREGROUND (doesn't
self-daemonize) and resolves its config from `$HOME/.claude-code-router/
config.json`, so a per-run HOME override gives each run its own isolated
proxy/port/config, launched as a background subprocess by this module and
torn down after the run.
"""
import json
import os
import socket
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS = REPO_ROOT / "harness"
SECRETS_DIR = HARNESS / ".secrets"
PROVIDER_API_KEY_ENV = "HARNESS_PROVIDER_API_KEY"  # placeholder name written into generated config.json;
                                                     # the real value only ever exists in this process's env,
                                                     # never on disk in the per-run ccr config.


class CcrError(RuntimeError):
    pass


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _load_api_key(model_cfg):
    key_file = SECRETS_DIR / (model_cfg["api_key_env"] + ".txt")
    if not key_file.exists():
        raise CcrError(
            "Missing %s -- save the API key for provider %r there (chmod 600)."
            % (key_file, model_cfg.get("provider_name"))
        )
    return key_file.read_text().strip()


def build_config(model_cfg, port):
    provider = {
        "name": model_cfg["provider_name"],
        "api_base_url": model_cfg["api_base_url"],
        "api_key": "$%s" % PROVIDER_API_KEY_ENV,
        "models": [model_cfg["model"]],
    }
    transformer = model_cfg.get("transformer")
    if transformer:
        use_list = transformer if isinstance(transformer, list) else [transformer]
        provider["transformer"] = {"use": use_list}
    router_key = "%s,%s" % (model_cfg["provider_name"], model_cfg["model"])
    cfg = {
        "PORT": port,
        # ccr's default file logger (level "debug") writes request details --
        # including the resolved real API key -- to ./logs/ccr-*.log under
        # this run's HOME. Disabled outright rather than just relocated,
        # since a plaintext credential in a log file is a real leak even for
        # the duration of a single run.
        "LOG": False,
        "Providers": [provider],
        "Router": {"default": router_key, "background": router_key, "think": router_key,
                   "longContext": router_key, "webSearch": router_key},
    }
    transformer_path = model_cfg.get("transformer_path")
    if transformer_path:
        # models.yaml stores this relative to harness/ so the repo is portable
        # (no machine-specific absolute paths); ccr itself needs an absolute
        # path since it runs with a different cwd.
        cfg["transformers"] = [{"path": str((HARNESS / transformer_path).resolve())}]
    return cfg, router_key


def start(model_cfg, run_ccr_home, timeout=20):
    """Returns a handle dict: {proc, port, home, router_key, log_f}. Caller
    must eventually call stop(handle)."""
    run_ccr_home.mkdir(parents=True, exist_ok=True)
    ccr_config_dir = run_ccr_home / ".claude-code-router"
    ccr_config_dir.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    api_key_value = _load_api_key(model_cfg)
    config, router_key = build_config(model_cfg, port)
    (ccr_config_dir / "config.json").write_text(json.dumps(config, indent=2))

    env = dict(os.environ)
    env["HOME"] = str(run_ccr_home)
    env[PROVIDER_API_KEY_ENV] = api_key_value

    log_path = run_ccr_home / "ccr.log"
    log_f = open(log_path, "wb")
    proc = subprocess.Popen(["ccr", "start"], env=env, stdout=log_f, stderr=subprocess.STDOUT,
                            cwd=str(run_ccr_home))

    deadline = time.time() + timeout
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            log_f.close()
            raise CcrError("ccr start exited early (code %s) -- see %s" % (proc.returncode, log_path))
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                ready = True
                break
        except OSError:
            time.sleep(0.5)
    if not ready:
        stop({"proc": proc, "home": run_ccr_home, "log_f": log_f})
        raise CcrError("ccr did not open port %d within %ds -- see %s" % (port, timeout, log_path))

    return {"proc": proc, "port": port, "home": run_ccr_home, "router_key": router_key, "log_f": log_f}


def stop(handle):
    env = dict(os.environ)
    env["HOME"] = str(handle["home"])
    try:
        subprocess.run(["ccr", "stop"], env=env, capture_output=True, timeout=10)
    except Exception:
        pass
    proc = handle.get("proc")
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    log_f = handle.get("log_f")
    if log_f and not log_f.closed:
        log_f.close()
