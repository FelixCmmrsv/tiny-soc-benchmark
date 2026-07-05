# SOC Benchmark Harness

Automated benchmark for testing AI models on SOC (Security Operations Center)
incident-response scenarios against a live Elastic/Kibana stack. Any model —
Anthropic, OpenAI, xAI, or a local OpenAI-compatible server — runs through
the same harness with identical tools, so scores are comparable across
vendors, not confounded by different scaffolding.

## Why this exists

The naive way to run this kind of benchmark is a human pasting each question
into a chat one at a time, waiting for an answer, and manually checking it
against an answer key — slow, and the "did the model actually answer
question 7 or is that a correction to question 6" bookkeeping gets
unreliable fast once you're doing it across multiple models and dozens of
questions per scenario.

This harness replaces that with:
- **A proctor** the model talks to over MCP, one question at a time, that
  grades locally and never reveals correct/wrong mid-run.
- **One harness for every model.** Non-Anthropic and local models are routed
  through a per-run [claude-code-router](https://github.com/musistudio/claude-code-router)
  instance instead of a bespoke integration per vendor — same tools, same
  skills, only the backend LLM differs.
- **Hashed answer keys.** The distributed scenario file never contains a
  plaintext correct answer.
- **Full isolation per run.** Fresh config dirs, no session history or
  credentials carried between runs or exposed to the model under test.

## Architecture

```
run_benchmark.py (orchestrator)
  │
  ├─ 1. resets Elastic with this scenario's data, pinning one explicit
  │     "anchor" timestamp shared with the proctor (see "Timestamp
  │     shifting" below)
  │
  ├─ 2. creates harness/runs/<run_id>/ :
  │       workdir/            - the only directory the agent can touch
  │         └─ .claude/skills/   (copied from elastic_skills/)
  │       claude_config/      - fresh scrubbed CLAUDE_CONFIG_DIR
  │       proctor_state/      - where the proctor logs verdicts
  │       mcp_config.json     - tells the agent how to reach the proctor
  │
  ├─ 3. picks how to reach the model:
  │       native model   -> real Anthropic auth token, direct
  │       any other model -> spins up a per-run claude-code-router (ccr)
  │                          instance (own HOME, own port, own generated
  │                          config) pointed at that provider's API
  │
  ├─ 4. runs `claude -p ...` non-interactively. The agent loops on its own:
  │       get_current_step -> investigate Elasticsearch -> submit_answer
  │       (one shot, no retries, no correctness feedback) -> repeat
  │
  └─ 5. reads proctor_state/results.jsonl (the source of truth for scoring,
        never the chat transcript), tears down ccr if used, updates
        runs/scoreboard.{md,json}
```

## Repository layout

```
harness/
  scenarios/<id>/
    manifest.source.yaml   - plaintext questions + answers (private, gitignored)
    manifest.yaml          - compiled: answers replaced by salted hashes (this is what ships)
  proctor/
    proctor_server.py      - hand-rolled MCP stdio server (no SDK dependency)
    grading.py             - pure grading logic, no I/O
    system_prompt_addendum.txt
  orchestrator/
    run_benchmark.py       - CLI entry point
    isolation.py           - per-run ephemeral dirs, skill seeding
    elastic_lifecycle.py   - wraps elastic/telemetry/upload.sh
    ccr_manager.py         - per-run claude-code-router lifecycle
    scoreboard.py
    models.yaml            - model registry
  ccr_transformers/        - small JS fixes for provider-specific request quirks
  elastic_skills/          - Elastic's official agent-skill packs, bundled so
                             every model starts with the same capabilities
  elastic/                 - Docker Compose + loader scripts (bundled tooling;
                             the actual scenario telemetry is NOT bundled,
                             see elastic/README.md)
  templates/                (generated, gitignored — see Setup)
  runs/                     (generated, gitignored — run output)
  tools/
    seed_templates.py       - builds templates/ from your local claude/ config
    compile_manifest.py     - manifest.source.yaml -> manifest.yaml
```

## Prerequisites

- **Docker Desktop** — see `elastic/README.md` for install/setup (what
  images it pulls, resource sizing, generating the API key).
- **Python 3.9+** with `pip3 install -r requirements.txt`.
- **The `claude` CLI**, and separately **[claude-code-router](https://github.com/musistudio/claude-code-router)**
  (`ccr`) installed and on `PATH` if you'll test any non-Anthropic model.
- **Scenario telemetry data** for whatever scenario you're running — not
  bundled, see `elastic/README.md`.

**Note on scenario content:** the questions and telemetry themselves are not
this project's IP (in the reference deployment, they come from BiZone
Cyberpolygon) — make sure you have the right to whatever scenario data you
load and, separately, to distribute the question text baked into any
`manifest.yaml` you share (the *answers* are hashed and unrecoverable, but
the question text is plaintext by design, since the model needs to read it).

## Setup

```bash
pip3 install -r requirements.txt

# Elastic stack — see elastic/README.md for the full walkthrough (Docker
# install, resource sizing, generating the API key). Short version:
cd harness/elastic
cp .env.example .env   # fill in ELASTIC_PASSWORD / KIBANA_SYSTEM_PASSWORD
docker compose up -d --wait
# ... generate an API key (command in .env.example), add it as ES_API_KEY ...
# ... place your scenario telemetry JSON under telemetry/data/ ...
cd -

# Auth: this harness runs `claude` as a subprocess with a token, not your
# interactive login session (an OAuth session from `claude auth login` does
# not carry over into a scripted CLAUDE_CONFIG_DIR — confirmed empirically,
# not documented behavior). Run:
claude setup-token
# then save the resulting token:
mkdir -p harness/.secrets && chmod 700 harness/.secrets
echo "<token>" > harness/.secrets/claude_oauth_token.txt
chmod 600 harness/.secrets/claude_oauth_token.txt

# For every non-native model in models.yaml, save its API key the same way,
# named after that entry's api_key_env, e.g.:
echo "<your key>" > harness/.secrets/OPENAI_API_KEY.txt
echo "<your key>" > harness/.secrets/XAI_API_KEY.txt
chmod 600 harness/.secrets/*.txt

# Build the scrubbed config-dir template every run copies from:
python3 harness/tools/seed_templates.py

# Compile any scenario manifest you've authored/edited:
python3 harness/tools/compile_manifest.py --all
```

Re-run `seed_templates.py` any time you re-authenticate; re-run
`compile_manifest.py --all` any time you edit a `manifest.source.yaml`.

## Running it

```bash
# see what scenarios exist
python3 harness/orchestrator/run_benchmark.py --list-scenarios

# see the exact command that would run, without spending anything
python3 harness/orchestrator/run_benchmark.py \
  --scenario scenario1_ferrumfox --model claude-sonnet-5 --dry-run

# the real thing
python3 harness/orchestrator/run_benchmark.py \
  --scenario scenario1_ferrumfox --model claude-sonnet-5 gpt-5.5 \
  --max-budget-usd 10 --timeout-minutes 60
```

Useful flags: `--no-recreate-elastic` (skip the Elastic reload, e.g. for
repeat runs against data you know is already loaded), `--keep-workdir`
(keep the per-run scrubbed config dirs/workdir for debugging instead of
deleting them after grading — `proctor_state/results.jsonl`,
`transcript.jsonl`, and `result_summary.json` are always kept regardless).

### Output

Each run gets `harness/runs/<scenario>__<model>__<hash>/`:
- `proctor_state/results.jsonl` — one line per answered step (step, answer,
  verdict). This, not the chat transcript, is the source of truth for scoring.
- `transcript.jsonl` — the full `--output-format stream-json` transcript
  (mined only for token usage/cost, never for answers). For routed
  (non-native) models, Claude Code's own usage accounting reports zero — it
  has no visibility into what actually happened on the real provider's side;
  getting real cost data for those would mean querying that provider's own
  usage API separately.
- `result_summary.json` — score + usage summary for the run.

`harness/runs/scoreboard.md`/`.json` are regenerated after every invocation
from *all* runs recorded so far (latest run per scenario+model pair), as a
question × model verdict table.

## Adding a scenario

Author `harness/scenarios/<id>/manifest.source.yaml` in plaintext:

```yaml
scenario_id: my_scenario
title: "Human-readable title"
capability_profile: elastic-only
briefing: |
  Shown once, before the first question.
elastic:
  data_dir: telemetry/data_my_scenario   # relative to harness/elastic/
  index_pattern_id: my-scenario
  kibana_space: null                     # null = default space
  source_max_ts: "2024-01-01T00:00:00"   # true max @timestamp in the source data
artifacts: []
steps:
  - n: 1
    action_text: "Narrative context for this step."
    question: "The question shown to the model."
    format_hint: "12.34.56.78;host.domain.com"
    grading:
      type: composite
      delimiter: ";"
      parts: [{type: exact}, {type: exact_ci}]
      expected: "1.2.3.4;example.com"
```

Grading `type`s (composable — `composite`/`list` nest other rules):

| type | For | Notes |
|---|---|---|
| `exact` | case-sensitive literal | passwords, keys |
| `exact_ci` | case-insensitive text | names, domains, paths |
| `numeric` | int/float | optional `tolerance` |
| `hash` | hex strings | case-insensitive, whitespace-stripped |
| `timestamp` | dates | optional `tolerance_seconds`; see below for shifted data |
| `single_choice` | enumerated options | `options: [...]`, `case_sensitive: bool` |
| `list` | delimited values | `ordered: bool`, `item_rule: {...}` |
| `set` | unordered multi-select | |
| `set_ordered_alpha` | multi-select that must itself be alphabetical | `enforce_order: bool` |
| `composite` | delimited compound answers | `parts: [...]`, recursive |

A step can omit `expected` (or set it to `null`) for a not-yet-confirmed
answer — it grades as `"ungraded"`, never silently wrong.

**Timestamp-shifted data:** if your Elastic loader shifts timestamps so a
scenario always "ends now" (this repo's does, via `--anchor`), a question
whose correct answer is an absolute historical timestamp needs
`shift_from_source: true` on that `timestamp` rule — the proctor recomputes
the correct shifted value per run instead of grading against a stale one.
See `proctor_server.py`'s `materialize_shifted_timestamps` for exactly how.

Then compile it — **never hand-edit or distribute `manifest.yaml` directly**:

```bash
python3 harness/tools/compile_manifest.py harness/scenarios/my_scenario
```

## Security model

Two distinct threats, two distinct layers:

**The model under test, during its own run, must never see the answer
key.** The manifest lives outside any path the agent's sandbox can reach,
loaded solely by the proctor. The agent only ever sees one question at a
time via tool results; submission is one-shot (no retries, no correctness
feedback — nothing to fish for); every run gets a fresh scrubbed config dir
(no prior session transcripts, shell history, or task state carried over).
`isolation.assert_manifest_not_reachable()` refuses to start if it detects
the manifest path overlapping the agent's sandbox root — checked at
runtime, not just by convention. This is process-level isolation, not a
hard sandbox: nothing here stops a sufficiently exploratory agent from
reading arbitrary paths on the host via its shell tools. Full containment
(Docker, restricted network egress) is a known gap, not yet built.

**A third party who receives this harness + a scenario to self-host, and
might try to extract the answer key from the file itself** (e.g. to
fine-tune their own model on the answers instead of having it genuinely
solve the investigation). `manifest.yaml` stores only
`sha256(salt + normalized_answer)` per question — nobody, including the
person who authored the scenario, can recover the plaintext from it.
**This is a deliberate, partial trade-off, not full protection:** for
low-entropy answers (yes/no, a handful of enumerated options, short tool
names) a determined party with full local access can still brute-force
plausible candidates and compare hashes offline — hashing here stops
casual peeking (`cat manifest.yaml`), not a resourced attacker. A stronger
guarantee would require grading server-side (submit answers to a service
that never distributes the key at all) — a materially different, non-local
architecture.

Secrets (`.secrets/*.txt`) are read once per run and injected as process
environment variables only — never written into any per-run config
directory, never logged. ccr's own default file logger was found writing
provider API keys in plaintext during testing; it's disabled at the source
(`"LOG": false` in the generated per-run config) rather than just relocated.

## Known limitations

- Cost/usage tracking is accurate for native models only (see "Output" above).
- Process-level isolation only — see "Security model" above.
- No forensics-tooling capability profile yet for scenarios needing more
  than Elasticsearch queries (disk-image/binary analysis).
- `grok-4.3` routing is implemented but not live-tested end-to-end (needs an
  `XAI_API_KEY`); `gpt-5.5` is live-verified.
