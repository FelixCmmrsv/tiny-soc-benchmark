"""Per-run isolation: fresh ephemeral run directory, scrubbed config-dir
copies, and the MCP config wiring the agent to its own proctor instance.
Nothing here ever touches the real shared claude/ home live -- only the
pre-scrubbed template under harness/templates/. Every model (native or
routed through ccr) uses this same Claude Code config -- there's no
separate per-vendor config dir.
"""
import json
import shutil
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS = REPO_ROOT / "harness"
RUNS_DIR = HARNESS / "runs"
TEMPLATES = HARNESS / "templates"
PROCTOR_SCRIPT = HARNESS / "proctor" / "proctor_server.py"
# Elastic's official agent-skill packs, copied into every run's workdir so
# every model -- regardless of which backend answers via ccr -- starts with
# identical capabilities, not just raw Bash+curl. Bundled in-repo (not
# sourced from an external scratch directory) so the harness is self-contained.
ELASTIC_SKILLS_SRC = HARNESS / "elastic_skills"


class IsolationError(RuntimeError):
    pass


def new_run_id(scenario_id, model_name):
    return "%s__%s__%s" % (scenario_id, model_name, uuid.uuid4().hex[:8])


def assert_manifest_not_reachable(manifest_path, sandbox_root):
    """Refuse to proceed if the scenario manifest (which holds the answer
    key) is anywhere under the sandbox root the agent gets --add-dir'd into,
    or vice versa. This is the hard boundary the whole "no leak" design
    depends on -- checked at runtime, not just by convention."""
    m = manifest_path.resolve()
    s = sandbox_root.resolve()
    if m == s or s in m.parents or m in s.parents:
        raise IsolationError(
            "REFUSING TO RUN: manifest path %s and sandbox root %s overlap -- "
            "this would let the agent read the answer key." % (m, s)
        )


def create_run(scenario_id, model_name, manifest_path, anchor):
    run_id = new_run_id(scenario_id, model_name)
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    workdir = run_dir / "workdir"
    workdir.mkdir()
    _seed_skills(workdir)

    assert_manifest_not_reachable(manifest_path, workdir)

    state_dir = run_dir / "proctor_state"
    state_dir.mkdir()

    claude_config = _materialize(TEMPLATES / "claude_config_template", run_dir / "claude_config")

    mcp_config_path = _write_mcp_config(run_dir, manifest_path, run_id, state_dir, anchor)

    return {
        "run_id": run_id,
        "run_dir": run_dir,
        "workdir": workdir,
        "state_dir": state_dir,
        "claude_config": claude_config,
        "mcp_config_path": mcp_config_path,
        "results_path": state_dir / "results.jsonl",
    }


def _seed_skills(workdir):
    if not ELASTIC_SKILLS_SRC.is_dir():
        return
    dest = workdir / ".claude" / "skills"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ELASTIC_SKILLS_SRC, dest)


def _materialize(template_dir, dest_dir):
    if not template_dir.exists():
        raise IsolationError(
            "template %s missing -- run harness/tools/seed_templates.py first" % template_dir
        )
    shutil.copytree(template_dir, dest_dir)
    return dest_dir


def _write_mcp_config(run_dir, manifest_path, run_id, state_dir, anchor):
    args = [
        str(PROCTOR_SCRIPT),
        "--manifest",
        str(manifest_path.resolve()),
        "--run-id",
        run_id,
        "--state-dir",
        str(state_dir.resolve()),
    ]
    if anchor:
        args += ["--anchor", anchor]
    cfg = {
        "mcpServers": {
            "proctor": {
                "command": "python3",
                "args": args,
            }
        }
    }
    path = run_dir / "mcp_config.json"
    path.write_text(json.dumps(cfg, indent=2))
    return path


def cleanup_run(run_dir, keep=False):
    if keep:
        return
    shutil.rmtree(run_dir, ignore_errors=True)
