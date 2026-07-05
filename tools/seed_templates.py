#!/usr/bin/env python3
"""One-time (or refresh-on-demand) helper: builds a scrubbed CLAUDE_CONFIG_DIR
template under harness/templates/ from the real, already-authenticated shared
home at claude/. Every benchmark run gets a fresh copy of this template (see
orchestrator/isolation.py) -- the real shared home is never touched or read
from live during a run. Every model, native or routed through ccr, uses this
same Claude Code engine -- there is no separate per-vendor config dir anymore.

Kept vs dropped is deliberate: keep exactly what's needed to authenticate
(oauth account metadata), drop everything that could carry cross-run state
(prior session transcripts, shell/prompt history, task-tool state, caches).
Note auth ITSELF doesn't actually travel via this file in this environment --
see harness/README.md's setup section on CLAUDE_CODE_OAUTH_TOKEN -- this
template exists to avoid onboarding prompts and preserve harmless
preferences, not to carry the credential.

Re-run any time to refresh the template.

harness/templates/ (this script's output) and harness/runs/ (run output) are
both gitignored -- everything in them is either regeneratable or per-run
data, never something to commit.
"""
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS = REPO_ROOT / "harness"

CLAUDE_SRC = REPO_ROOT / "claude"
CLAUDE_DST = HARNESS / "templates" / "claude_config_template"
# Everything else in claude/ is dropped by default (allowlist, not denylist --
# safer default when a home directory can grow new cache/state dirs over time).
CLAUDE_KEEP = {
    ".claude.json",  # holds oauthAccount / auth
    "plugins",       # marketplace cache, harmless, speeds startup
}
CLAUDE_SETTINGS = {
    "skipDangerousModePermissionPrompt": True
    # deliberately no "model"/"effortLevel" -- the orchestrator passes
    # --model/--effort explicitly per job so nothing is silently inherited
    # from a stale template.
}


def seed(src, dst, keep, label):
    if not src.is_dir():
        print(f"[seed_templates] SKIP {label}: source {src} does not exist", file=sys.stderr)
        return
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    copied = []
    for name in keep:
        s = src / name
        if not s.exists():
            print(f"[seed_templates] WARN {label}: expected {s} not found, skipping", file=sys.stderr)
            continue
        d = dst / name
        if s.is_dir():
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)
        copied.append(name)
    print(f"[seed_templates] {label}: copied {copied} into {dst}")


def scrub_claude_json(path):
    """.claude.json's top-level "projects" dict caches per-cwd state from past
    runs (mcpServers, allowedTools, lastSessionId, token/cost stats). Every
    benchmark run uses a fresh runs/<id>/workdir/ cwd so it wouldn't even
    match, but strip it anyway -- no reason to carry any prior run's state
    into the template even if inert."""
    data = json.loads(path.read_text())
    removed = data.pop("projects", None)
    path.write_text(json.dumps(data, indent=2) + "\n")
    if removed:
        print(f"[seed_templates] claude: stripped 'projects' key ({list(removed.keys())}) from .claude.json")


def main():
    seed(CLAUDE_SRC, CLAUDE_DST, CLAUDE_KEEP, "claude")
    scrub_claude_json(CLAUDE_DST / ".claude.json")
    (CLAUDE_DST / "settings.json").write_text(json.dumps(CLAUDE_SETTINGS, indent=2) + "\n")
    print("[seed_templates] claude: wrote minimal settings.json")

    print("[seed_templates] done.")


if __name__ == "__main__":
    main()
