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

Each entry in `skills-lock.json` names a skill, its upstream
(`source: elastic/agent-skills`, `sourceType: github`), the path to its
`SKILL.md` within that repo, and a `computedHash` pinning the exact content.
Fetch the 35 skills listed there into per-skill subdirectories here:

```
elastic_skills/<skill-name>/SKILL.md   (plus that skill's scripts/, references/, etc.)
```

so the layout matches the `name:` in each lock entry (e.g.
`elastic_skills/security-alert-triage/SKILL.md`). Use whatever skill-install
tooling you normally use with the Elastic agent-skills repo; the lock file is
there so the exact versions are reproducible.
