#!/usr/bin/env python3
"""Orchestrator CLI for the BiZone SOC benchmark harness. Every model in
models.yaml runs through the same claude CLI + Claude Code engine (same
tools/skills) -- native models (claude-sonnet-5) authenticate directly,
everything else is routed through a fresh per-run claude-code-router
instance pointed at that provider's own API. See README.md for the full
design.

Usage:
  python3 harness/orchestrator/run_benchmark.py --list-scenarios
  python3 harness/orchestrator/run_benchmark.py --scenario scenario1_ferrumfox \\
      --model claude-sonnet-5 [--dry-run] [--max-budget-usd 5] \\
      [--timeout-minutes 45] [--no-recreate-elastic] [--keep-workdir]
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capability_profiles
import ccr_manager
import elastic_lifecycle
import isolation
import proctor_manager
import scoreboard

SANDBOX_IMAGE = "harness-sandbox:latest"


def _container_url(url):
    """Rewrite a host-localhost URL to what the agent's container must use to
    reach the same service on the host."""
    if not url:
        return url
    return url.replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS = REPO_ROOT / "harness"
SCENARIOS_DIR = HARNESS / "scenarios"
MODELS_YAML = HARNESS / "orchestrator" / "models.yaml"
SECRETS_DIR = HARNESS / ".secrets"
CLAUDE_OAUTH_TOKEN_FILE = SECRETS_DIR / "claude_oauth_token.txt"


def load_claude_oauth_token():
    """Long-lived auth token from `claude setup-token`. Not stored in any
    per-run copied config dir -- injected as an env var only, straight from a
    600-permission file outside any path an agent run can reach."""
    if not CLAUDE_OAUTH_TOKEN_FILE.exists():
        raise SystemExit(
            "Missing %s -- run `claude setup-token` and save the result there "
            "(chmod 600)." % CLAUDE_OAUTH_TOKEN_FILE
        )
    return CLAUDE_OAUTH_TOKEN_FILE.read_text().strip()


def load_manifest(scenario_id):
    path = SCENARIOS_DIR / scenario_id / "manifest.yaml"
    if not path.exists():
        raise SystemExit("Unknown scenario %r (no %s)" % (scenario_id, path))
    with open(path, "r", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    return manifest, path


def load_models():
    with open(MODELS_YAML, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def list_scenarios():
    return sorted(p.name for p in SCENARIOS_DIR.iterdir() if (p / "manifest.yaml").exists())


def print_models():
    """Show every configured model, its backend, the secret file it needs,
    and whether that secret is present. The generic 'what do I need to run
    this model' interface -- works for any vendor added to models.yaml."""
    models = load_models()
    if not models:
        print("No models configured in %s" % MODELS_YAML)
        return
    print("%-22s %-26s %-42s %s" % ("MODEL", "BACKEND", "SECRET FILE", "PRESENT"))
    for name, cfg in models.items():
        if cfg.get("native"):
            backend = "native (Anthropic)"
            secret = CLAUDE_OAUTH_TOKEN_FILE
        else:
            backend = "routed -> %s:%s" % (cfg.get("provider_name", "?"), cfg.get("model", "?"))
            secret = SECRETS_DIR / ((cfg.get("api_key_env") or "MISSING_api_key_env") + ".txt")
        present = "yes" if secret.exists() else "NO  <-- create it"
        try:
            secret_disp = str(secret.relative_to(REPO_ROOT))
        except ValueError:
            secret_disp = str(secret)
        print("%-22s %-26s %-42s %s" % (name, backend, secret_disp, present))
    print("\nAdd a model: edit %s (see its header), then drop the provider's API key\n"
          "at the SECRET FILE path above (chmod 600)." % str(MODELS_YAML.relative_to(REPO_ROOT)))


def build_initial_prompt(manifest, conn):
    if conn["auth_method"] == "apikey":
        auth_hint = (
            "Authenticate to Elasticsearch with header "
            "`Authorization: ApiKey $ES_API_KEY` (env var already set -- "
            "don't ask for it, don't print it)."
        )
    elif conn["auth_method"] == "basic":
        auth_hint = (
            "Authenticate to Elasticsearch with HTTP basic auth using "
            "`-u \"$ES_USER:$ES_PASSWORD\"` (env vars already set -- don't "
            "ask for them, don't print them)."
        )
    else:
        auth_hint = "Elasticsearch requires no authentication in this environment."
    return (
        "You are the on-call SOC analyst for this benchmark. Elasticsearch is at "
        "%s (Kibana at %s). %s "
        "Call the proctor's get_current_step tool now to receive your first task."
    ) % (conn["es_url"], conn["kibana_url"], auth_hint)


def build_claude_command(run, model_cfg, manifest, max_budget_usd, conn, ccr_handle=None):
    """Every model -- native Anthropic or routed through ccr to any other
    provider -- goes through this SAME claude CLI invocation shape (same
    tools, same skills, same MCP proctor). Only auth/model-selection differs:
    native uses CLAUDE_CODE_OAUTH_TOKEN + the model's own name; routed models
    point ANTHROPIC_BASE_URL at this run's per-run ccr instance and use
    ccr's "<provider>,<model>" router key as --model instead.
    """
    system_addendum = (HARNESS / "proctor" / "system_prompt_addendum.txt").read_text()
    prompt = build_initial_prompt(manifest, conn)

    model_arg = ccr_handle["router_key"] if ccr_handle else model_cfg["model"]

    cmd = [
        "claude", "-p",
        "--mcp-config", str(run["mcp_config_path"]),
        "--strict-mcp-config",
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model_arg,
        "--max-budget-usd", str(max_budget_usd),
        "--add-dir", str(run["workdir"]),
        "--disallowedTools", "WebSearch,WebFetch",
        "--append-system-prompt", system_addendum,
    ]
    if model_cfg.get("effort"):
        cmd += ["--effort", model_cfg["effort"]]
    cmd += [prompt]

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(run["claude_config"])
    if ccr_handle:
        env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:%s" % ccr_handle["port"]
        env["ANTHROPIC_AUTH_TOKEN"] = "routed-via-ccr-placeholder"  # ccr swaps in the real provider key itself
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    else:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = load_claude_oauth_token()

    # Give the agent the ES credentials its prompt references. Whichever auth
    # method is configured, expose both the raw values it needs -- ES_API_KEY
    # for the apikey path, ES_USER/ES_PASSWORD for basic auth.
    if conn["es_api_key"]:
        env["ES_API_KEY"] = conn["es_api_key"]
    if conn["es_user"]:
        env["ES_USER"] = conn["es_user"]
    if conn["es_password"]:
        env["ES_PASSWORD"] = conn["es_password"]

    return cmd, env


def build_docker_command(run, model_cfg, manifest, max_budget_usd, conn, proctor_port, ccr_handle=None, resuming=False):
    """Contained variant: the agent runs inside the sandbox container with ONLY
    its workdir mounted -- no host filesystem, so no plaintext answer key to
    find. It reaches the proctor / Elasticsearch / ccr on the host via
    host.docker.internal. The manifest (answer key) never enters the container.
    """
    system_addendum = (HARNESS / "proctor" / "system_prompt_addendum.txt").read_text()
    # ES/Kibana URLs the CONTAINER must use (host.docker.internal, not localhost).
    c_conn = dict(conn)
    c_conn["es_url"] = _container_url(conn["es_url"])
    c_conn["kibana_url"] = _container_url(conn["kibana_url"])
    prompt = build_initial_prompt(manifest, c_conn)
    if resuming:
        prompt += (" NOTE: this is a resumed session -- your working directory "
                   "/work may already contain files and extracted artifacts (e.g. a "
                   "mounted/unpacked disk image) from earlier analysis of this same "
                   "scenario. Reuse them; you do not need to re-download or re-extract.")
    model_arg = ccr_handle["router_key"] if ccr_handle else model_cfg["model"]

    # MCP config the container reads -- points at the host proctor over HTTP.
    mcp_cfg = {"mcpServers": {"proctor": {"type": "http",
               "url": "http://host.docker.internal:%d/mcp" % proctor_port}}}
    (run["workdir"] / "mcp_config.json").write_text(json.dumps(mcp_cfg, indent=2))

    docker = [
        "docker", "run", "--rm", "--name", "harness_%s" % run["run_id"],
        "-v", "%s:/work" % run["workdir"].resolve(),
        "-w", "/work",
        # Bound the container's memory so a forensic spike (e.g. hashcat, or
        # a big image load) can't OOM-kill the whole Docker VM and take
        # Elasticsearch down with it. Swap headroom lets tools that briefly
        # exceed RAM continue instead of dying outright.
        "--memory", os.environ.get("HARNESS_SANDBOX_MEM", "4g"),
        "--memory-swap", os.environ.get("HARNESS_SANDBOX_MEMSWAP", "8g"),
    ]
    # Env into the container.
    denv = {}
    if ccr_handle:
        denv["ANTHROPIC_BASE_URL"] = "http://host.docker.internal:%s" % ccr_handle["port"]
        denv["ANTHROPIC_AUTH_TOKEN"] = "routed-via-ccr-placeholder"
    else:
        denv["CLAUDE_CODE_OAUTH_TOKEN"] = load_claude_oauth_token()
    if conn["es_api_key"]:
        denv["ES_API_KEY"] = conn["es_api_key"]
    if conn["es_user"]:
        denv["ES_USER"] = conn["es_user"]
    if conn["es_password"]:
        denv["ES_PASSWORD"] = conn["es_password"]
    for k, v in denv.items():
        docker += ["-e", "%s=%s" % (k, v)]
    docker += [SANDBOX_IMAGE]

    claude = [
        "claude", "-p",
        "--mcp-config", "/work/mcp_config.json", "--strict-mcp-config",
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--output-format", "stream-json", "--verbose",
        "--model", model_arg,
        "--max-budget-usd", str(max_budget_usd),
        "--add-dir", "/work",
        "--disallowedTools", "WebSearch,WebFetch",
        "--append-system-prompt", system_addendum,
    ]
    if model_cfg.get("effort"):
        claude += ["--effort", model_cfg["effort"]]
    claude += [prompt]

    return docker + claude, dict(os.environ)


def run_job(scenario_id, manifest, manifest_path, model_name, model_cfg, args, conn, resume_run_id=None):
    print("\n=== Job: scenario=%s model=%s%s ==="
          % (scenario_id, model_name, " (RESUME)" if resume_run_id else ""))

    resuming = bool(resume_run_id)
    if resuming:
        # Reuse the existing run (accumulated workdir + proctor_state) and the
        # anchor it was loaded with -- NO Elastic reset (re-shifting with a new
        # anchor would break shift_from_source grading against already-loaded
        # data).
        run, anchor = isolation.load_run(resume_run_id)
        already = 0
        if run["results_path"].exists():
            already = sum(1 for l in run["results_path"].read_text().splitlines() if l.strip())
        print("[run_benchmark] resuming run_id=%s: %d/%d already answered, anchor=%s"
              % (resume_run_id, already, len(manifest["steps"]), anchor))
    else:
        # One anchor for this whole job, shared by the Elastic reset and the
        # proctor's timestamp-shift grading -- computed once here so both sides
        # agree exactly, instead of each independently defaulting to "now" at
        # slightly different wall-clock moments.
        anchor = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    if args.dry_run:
        pass  # dry-run is side-effect-free: no Elastic reset, no ccr, no agent
    elif resuming:
        print("[run_benchmark] resume: skipping Elastic reset (reusing loaded data + anchor)")
    elif not args.no_recreate_elastic:
        elastic_lifecycle.reset_scenario_data(
            manifest, anchor, es_url=conn["es_url"], kibana_url=conn["kibana_url"],
            no_compose=args.no_compose)
    else:
        print("[run_benchmark] skipping Elastic reset (--no-recreate-elastic)")

    if not resuming:
        run = isolation.create_run(scenario_id, model_name, manifest_path, anchor)
    print("[run_benchmark] run_id=%s run_dir=%s" % (run["run_id"], run["run_dir"]))

    if args.dry_run:
        # Zero side effects: no ccr process, no requirement that API key
        # files already exist -- just show the shape of what would run.
        router_key = "%s,%s" % (model_cfg["provider_name"], model_cfg["model"]) if not model_cfg.get("native") else None
        fake_ccr_handle = {"port": "<ccr-port>", "router_key": router_key} if router_key else None
        if args.sandbox:
            cmd, _ = build_docker_command(run, model_cfg, manifest, args.max_budget_usd,
                                          conn, 0, fake_ccr_handle)
            print("[dry-run] CONTAINED -- would start host HTTP proctor + execute:")
        else:
            cmd, env = build_claude_command(run, model_cfg, manifest, args.max_budget_usd,
                                            conn, fake_ccr_handle)
            print("[dry-run] would execute:")
        print(" ".join(_shell_quote(c) for c in cmd))
        if fake_ccr_handle:
            print("[dry-run] would start a per-run ccr instance for provider %s"
                  % model_cfg["provider_name"])
        isolation.cleanup_run(run["run_dir"], keep=True)
        return None

    ccr_handle = None
    proctor_handle = None
    try:
        if not model_cfg.get("native"):
            print("[run_benchmark] starting per-run ccr instance for provider %s..." % model_cfg["provider_name"])
            ccr_handle = ccr_manager.start(model_cfg, run["run_dir"] / "ccr_home")
            print("[run_benchmark] ccr ready on port %d (router key %s)"
                  % (ccr_handle["port"], ccr_handle["router_key"]))

        if args.sandbox:
            print("[run_benchmark] CONTAINED run: starting host HTTP proctor + docker sandbox...")
            proctor_handle = proctor_manager.start(manifest_path, run["run_id"],
                                                   run["state_dir"], run["workdir"], anchor,
                                                   resume=resuming)
            print("[run_benchmark] proctor (host) on port %d; agent runs in %s"
                  % (proctor_handle["port"], SANDBOX_IMAGE))
            cmd, env = build_docker_command(run, model_cfg, manifest, args.max_budget_usd,
                                            conn, proctor_handle["port"], ccr_handle, resuming=resuming)
            cwd = None
        else:
            cmd, env = build_claude_command(run, model_cfg, manifest, args.max_budget_usd,
                                            conn, ccr_handle)
            cwd = str(run["workdir"])

        transcript_path = run["run_dir"] / "transcript.jsonl"
        stderr_path = run["run_dir"] / "stderr.log"
        t0 = time.time()
        timed_out = False
        with open(transcript_path, "wb") as out_f, open(stderr_path, "wb") as err_f:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out_f, stderr=err_f)
            try:
                proc.wait(timeout=args.timeout_minutes * 60)
            except subprocess.TimeoutExpired:
                timed_out = True
                print("[run_benchmark] TIMEOUT after %d min -- terminating" % args.timeout_minutes)
                if args.sandbox:
                    # killing the `docker run` client doesn't stop the container
                    subprocess.run(["docker", "kill", "harness_%s" % run["run_id"]],
                                   capture_output=True)
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        duration_s = time.time() - t0
    finally:
        if proctor_handle:
            proctor_manager.stop(proctor_handle)
        if ccr_handle:
            ccr_manager.stop(ccr_handle)

    summary = summarize_run(scenario_id, model_name, run, manifest, duration_s, timed_out, proc.returncode)
    (run["run_dir"] / "result_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    # A resumable run must keep its workdir (the accumulated / extracted state)
    # for the next window; only a genuinely finished run may be cleaned.
    finished = summarize_run(scenario_id, model_name, run, manifest, 0, False, 0)["steps_answered"] >= len(manifest["steps"])
    if not args.keep_workdir and (finished or not resuming):
        # keep proctor_state/results.jsonl + summary + transcript; only drop the
        # scrubbed config-dir copies and scratch workdir (nothing worth keeping
        # once the run is graded, and they're the largest part of the run dir).
        isolation.cleanup_run(run["claude_config"], keep=False)
        isolation.cleanup_run(run["workdir"], keep=False)
        isolation.cleanup_run(run["run_dir"] / "ccr_home", keep=False)

    return summary


def summarize_run(scenario_id, model_name, run, manifest, duration_s, timed_out, returncode):
    results = []
    if run["results_path"].exists():
        for line in run["results_path"].read_text().splitlines():
            if line.strip():
                results.append(json.loads(line))

    graded = [r for r in results if r["verdict"] in ("correct", "wrong")]
    correct = [r for r in results if r["verdict"] == "correct"]
    total_steps = len(manifest["steps"])

    usage = extract_usage(run["run_dir"] / "transcript.jsonl")

    return {
        "scenario_id": scenario_id,
        "model": model_name,
        "run_id": run["run_id"],
        "total_steps": total_steps,
        "steps_answered": len(results),
        "steps_graded": len(graded),
        "steps_correct": len(correct),
        "score": "%d/%d" % (len(correct), total_steps),
        "timed_out": timed_out,
        "returncode": returncode,
        "duration_seconds": round(duration_s, 1),
        "usage": usage,
        "per_step": {r["step"]: r["verdict"] for r in results},
    }


def extract_usage(transcript_path):
    """Best-effort token usage extraction from the --output-format stream-json
    transcript. Defensive: the exact event shape is verified in the Phase 1
    spike, not assumed -- unrecognized lines are skipped, never fatal."""
    total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    if not transcript_path.exists():
        return total
    for line in transcript_path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        u = (o.get("message") or {}).get("usage") or o.get("usage")
        if not u:
            continue
        total["input"] += u.get("input_tokens", 0) or 0
        total["output"] += u.get("output_tokens", 0) or 0
        total["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
        total["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
    return total


def _shell_quote(s):
    if re.fullmatch(r"[A-Za-z0-9_./=,:@-]+", s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", action="append", default=[])
    ap.add_argument("--model", nargs="+", default=[])
    ap.add_argument("--max-budget-usd", type=float, default=5.0)
    ap.add_argument("--timeout-minutes", type=float, default=45)
    ap.add_argument("--no-recreate-elastic", action="store_true")
    ap.add_argument("--no-compose", action="store_true",
                     help="Don't run the local `docker compose up`; target an already-running "
                          "or remote Elasticsearch/Kibana (configure its URL/auth in elastic/.env "
                          "or via --es-url/--kibana-url).")
    ap.add_argument("--sandbox", action="store_true",
                     help="Run the agent inside the Docker sandbox (harness-sandbox:latest) with "
                          "NO host filesystem access -- the proctor runs on the host over HTTP so "
                          "the answer key can't be read off disk. The only leak-safe mode; required "
                          "for a trustworthy score. Build the image once: "
                          "docker build -t harness-sandbox:latest harness/sandbox/")
    ap.add_argument("--resume", default=None, metavar="RUN_ID",
                     help="Continue an interrupted contained run: reuse its workdir (extracted "
                          "state) + anchor, skip the Elastic reset, and pick up at the next "
                          "unanswered question. Requires --sandbox and a --keep-workdir'd run. "
                          "Scenario/model are taken from the run id.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-workdir", action="store_true")
    ap.add_argument("--list-scenarios", action="store_true")
    ap.add_argument("--list-models", action="store_true",
                     help="Show configured models, their backend, and which .secrets/ key file "
                          "each needs (and whether it's present), then exit.")
    ap.add_argument("--es-url", default=None,
                     help="Override the Elasticsearch URL (else elastic/.env's ELASTICSEARCH_URL, "
                          "else http://localhost:9200).")
    ap.add_argument("--kibana-url", default=None,
                     help="Override the Kibana URL (else elastic/.env's KIBANA_URL, else "
                          "http://127.0.0.1:5601).")
    args = ap.parse_args()

    if args.list_scenarios:
        for s in list_scenarios():
            print(s)
        return

    if args.list_models:
        print_models()
        return

    models = load_models()

    if args.resume:
        # Resume derives scenario + model from the run id (scenario__model__hash).
        if not args.sandbox:
            ap.error("--resume requires --sandbox (resume only applies to contained runs).")
        parts = args.resume.split("__")
        if len(parts) < 3:
            ap.error("--resume expects a run id like scenario__model__hash, got %r" % args.resume)
        args.scenario = [parts[0]]
        args.model = [parts[1]]

    if not args.scenario or not args.model:
        ap.error("--scenario and --model are required (or use --list-scenarios / --list-models / --resume)")

    for m in args.model:
        if m not in models:
            ap.error("Unknown model %r. Known: %s" % (m, ", ".join(models)))
        cfg = models[m]
        required = ["model"] if cfg.get("native") else ["provider_name", "api_base_url", "api_key_env", "model"]
        missing = [k for k in required if k not in cfg]
        if missing:
            ap.error("models.yaml entry %r missing required field(s): %s" % (m, missing))

    # Resolve the ES/Kibana connection once (CLI > elastic/.env > default) and
    # reuse it for the data reload, the agent prompt, and credential injection.
    conn = elastic_lifecycle.connection(args.es_url, args.kibana_url)
    print("[run_benchmark] Elasticsearch=%s Kibana=%s auth=%s"
          % (conn["es_url"], conn["kibana_url"], conn["auth_method"]))

    if not args.no_compose:
        ok, msg = elastic_lifecycle.docker_available()
        if not ok:
            ap.error("Docker preflight failed: %s (pass --no-compose to target an existing cluster)" % msg)

    manifests = {}
    for scenario_id in args.scenario:
        manifest, manifest_path = load_manifest(scenario_id)
        manifests[scenario_id] = (manifest, manifest_path)
        if not args.sandbox:
            # For --sandbox the tools live in the image, not the host.
            try:
                capability_profiles.require(manifest.get("capability_profile", "elastic-only"))
            except capability_profiles.CapabilityError as e:
                ap.error(str(e))

    summaries = []
    for scenario_id in args.scenario:
        manifest, manifest_path = manifests[scenario_id]
        for model_name in args.model:
            model_cfg = models[model_name]
            summary = run_job(scenario_id, manifest, manifest_path, model_name, model_cfg, args, conn,
                              resume_run_id=args.resume)
            if summary:
                summaries.append(summary)
                print("[run_benchmark] %s x %s -> score %s (%d timed out=%s)"
                      % (scenario_id, model_name, summary["score"], summary["returncode"], summary["timed_out"]))

    if summaries:
        scoreboard.render(summaries)


if __name__ == "__main__":
    main()
