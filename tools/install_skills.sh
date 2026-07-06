#!/bin/bash
# Convenience wrapper that populates harness/elastic_skills/ with Elastic's
# official agent skills, using the `skills` CLI (https://www.npmjs.com/package/skills):
#
#   npx skills add elastic/agent-skills --all
#
# The `skills` CLI installs into an agent's conventional directory
# (.claude/skills/) and writes a skills-lock.json pinning each skill by
# content hash. This script runs that install in a scratch directory, then
# copies the 35 skills and the refreshed lock file into harness/elastic_skills/
# where the harness reads them (see elastic_skills/README.md). Re-run any time
# to update to the latest upstream skills.
#
# Requires: node/npx on PATH, network access to github.com/elastic/agent-skills.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEST="$HARNESS_DIR/elastic_skills"

if ! command -v npx >/dev/null 2>&1; then
  echo "npx not found on PATH. Install Node.js first: https://nodejs.org/" >&2
  exit 1
fi

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

echo "==> Fetching Elastic agent skills into a scratch dir ..."
# --skill '*' --agent claude-code --copy is equivalent to `--all` scoped to a
# single agent (avoids fanning the same 35 skills out to every agent dir on
# disk). --copy materializes real files rather than symlinks.
( cd "$SCRATCH" && npx --yes skills@latest add elastic/agent-skills \
    --skill '*' --agent claude-code --yes --copy )

SRC="$SCRATCH/.claude/skills"
if [ ! -d "$SRC" ]; then
  echo "Expected skills at $SRC but the install produced nothing there." >&2
  echo "The 'skills' CLI layout may have changed; inspect $SCRATCH manually." >&2
  exit 1
fi

echo "==> Copying $(ls -d "$SRC"/*/ | wc -l | tr -d ' ') skills into $DEST ..."
mkdir -p "$DEST"
# Replace the skill payloads but keep our README in place.
find "$DEST" -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +
cp -R "$SRC"/. "$DEST"/
[ -f "$SCRATCH/skills-lock.json" ] && cp "$SCRATCH/skills-lock.json" "$DEST/skills-lock.json"

echo "==> Done. $DEST now holds $(ls -d "$DEST"/*/ | wc -l | tr -d ' ') skills; lock file refreshed."
