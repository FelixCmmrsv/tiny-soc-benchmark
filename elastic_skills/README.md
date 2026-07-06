# Elastic agent skills (optional, fetched dependency)

Elastic's official agent-skill packs (`author: elastic`, from the
[`elastic/agent-skills`](https://github.com/elastic/agent-skills) repository).
When present, they're copied into every run's workdir so every model —
whichever backend answers via `ccr` — starts with the same higher-level
Elasticsearch/Kibana capabilities, not just raw Bash + `curl`.

**Not bundled in this repo.** These are Elastic's content, not ours (same
reasoning as the scenario telemetry and disk-image artifacts): we ship the
pinned `skills-lock.json` — which records exactly which skills, from which
upstream, at which content hash — but not the skill payloads themselves.

**Optional.** The harness soft-fails if this directory is empty: models
still get shell access and can query Elasticsearch/Kibana directly with
`curl`. Populating it just gives them the pre-built skills on top.

## Populating it

**Easiest — one command:**

```
bash tools/install_skills.sh
```

This wraps the [`skills`](https://www.npmjs.com/package/skills) CLI
(`npx skills add elastic/agent-skills --all`), fetches the 35 skills into
this directory, and refreshes `skills-lock.json`. Requires `node`/`npx` and
network access to `github.com/elastic/agent-skills`.

**Or run the `skills` CLI directly** if you already use it — e.g.

```
npx skills add elastic/agent-skills --all
```

installs into your project's `.claude/skills/`; point the harness at it with
`HARNESS_ELASTIC_SKILLS_DIR=/path/to/.claude/skills` instead of copying into
`elastic_skills/`.

**Layout.** Whatever route you use, the harness expects one directory per
skill, matching the `name:` in each `skills-lock.json` entry:

```
elastic_skills/<skill-name>/SKILL.md   (plus that skill's scripts/, references/, etc.)
```

Each lock entry names the skill, its upstream (`source: elastic/agent-skills`,
`sourceType: github`), the path to its `SKILL.md` in that repo, and a
`computedHash` pinning the exact content — so versions are reproducible.
