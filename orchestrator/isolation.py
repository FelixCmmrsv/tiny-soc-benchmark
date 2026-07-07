"""Per-run isolation: fresh ephemeral run directory, scrubbed config-dir
copies, and the MCP config wiring the agent to its own proctor instance.
Nothing here ever touches the real shared claude/ home live -- only the
pre-scrubbed template under harness/templates/. Every model (native or
routed through ccr) uses this same Claude Code config -- there's no
separate per-vendor config dir.
"""
import json
import os
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
# identical capabilities, not just raw Bash+curl. Populate with
# tools/install_skills.sh (see elastic_skills/README.md). Override the
# location with HARNESS_ELASTIC_SKILLS_DIR to point at a skills dir you
# installed elsewhere (e.g. a project-level .claude/skills/ from
# `npx skills add`). Absent -> skills simply not seeded (soft dependency).
ELASTIC_SKILLS_SRC = Path(os.environ.get("HARNESS_ELASTIC_SKILLS_DIR", str(HARNESS / "elastic_skills")))


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

    mcp_config_path = _write_mcp_config(run_dir, manifest_path, run_id, state_dir, anchor, workdir)

    # Persist the anchor so a --resume of this run reuses the SAME shift the
    # Elastic data was loaded with (re-resetting with a new anchor would
    # break shift_from_source grading).
    (run_dir / "anchor.txt").write_text(anchor or "")

    return {
        "run_id": run_id,
        "run_dir": run_dir,
        "workdir": workdir,
        "state_dir": state_dir,
        "claude_config": claude_config,
        "mcp_config_path": mcp_config_path,
        "results_path": state_dir / "results.jsonl",
    }


def load_run(run_id):
    """Reconstruct the run dict for an existing run WITHOUT creating or
    cleaning anything -- used by --resume so the accumulated workdir (e.g. an
    extracted disk image) and proctor_state survive across restarts. Returns
    (run_dict, anchor)."""
    run_dir = RUNS_DIR / run_id
    if not run_dir.is_dir():
        raise IsolationError("cannot resume: no run dir at %s" % run_dir)
    workdir = run_dir / "workdir"
    state_dir = run_dir / "proctor_state"
    if not workdir.is_dir() or not state_dir.is_dir():
        raise IsolationError("cannot resume %s: workdir/proctor_state missing (was it cleaned? "
                             "resume needs --keep-workdir runs)" % run_id)
    anchor_file = run_dir / "anchor.txt"
    anchor = anchor_file.read_text().strip() if anchor_file.exists() else None
    run = {
        "run_id": run_id,
        "run_dir": run_dir,
        "workdir": workdir,
        "state_dir": state_dir,
        "claude_config": run_dir / "claude_config",
        "mcp_config_path": run_dir / "mcp_config.json",
        "results_path": state_dir / "results.jsonl",
    }
    return run, anchor


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


def _write_mcp_config(run_dir, manifest_path, run_id, state_dir, anchor, workdir=None):
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
    if workdir:
        args += ["--workdir", str(Path(workdir).resolve())]
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
