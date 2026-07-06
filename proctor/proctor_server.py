#!/usr/bin/env python3
"""Hand-rolled stdio MCP server for the BiZone SOC benchmark harness.

Exposes exactly two tools to the agent under test: get_current_step and
submit_answer. Holds the scenario manifest + answer key + this run's progress
entirely in this process's memory and a private, append-only results.jsonl log
under --state-dir. Never reads or writes anything under the agent's own
sandbox/workdir, and never reveals correct/wrong or the expected answer to the
agent -- that would leak the key through the one channel the agent can see.

Hand-rolled (stdlib json/sys only) instead of the official `mcp` Python SDK
because the host python3 (3.9) predates the SDK's minimum (3.10), and a
2-tool server doesn't need the SDK's resources/prompts/sampling machinery
anyway -- a smaller surface is easier to audit for "does this leak the key."

Protocol: JSON-RPC 2.0 over stdio, one message per line (MCP stdio transport).
"""
import sys
import os
import json
import time
import argparse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from grading import grade, parse_timestamp_to_epoch

PROTOCOL_VERSION = "2024-11-05"
HARNESS = Path(__file__).resolve().parent.parent
# Large scenario artifacts (disk images, pcaps -- can be many GB) are never
# copied into the repo or the run's config dirs; the operator supplies them
# once here, and the proctor symlinks the relevant one into the agent's
# workdir only once the manifest says it's unlocked (see harness/scenario_artifacts/README.md).
SCENARIO_ARTIFACTS_ROOT = HARNESS / "scenario_artifacts"

TOOLS = [
    {
        "name": "get_current_step",
        "description": (
            "Get the benchmark step you currently need to work on (briefing, "
            "action narrative, question, and answer format hint). Call this "
            "first, and again any time you need to re-orient."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "submit_answer",
        "description": (
            "Submit your final answer for the CURRENT step. One shot only: "
            "once submitted you cannot revise it, and the response will not "
            "reveal whether it was correct. Returns the next step's question, "
            "or signals the scenario is complete."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": "Your final answer for the current step, formatted per its format_hint.",
                }
            },
            "required": ["answer"],
            "additionalProperties": False,
        },
    },
]


class ProctorState:
    def __init__(self, manifest, run_id, state_dir, workdir=None):
        self.manifest = manifest
        self.run_id = run_id
        self.state_dir = state_dir
        self.workdir = Path(workdir) if workdir else None
        self.steps = manifest["steps"]
        self.idx = 0
        self.started = False
        self.complete = False
        os.makedirs(state_dir, exist_ok=True)
        self.results_path = os.path.join(state_dir, "results.jsonl")

    def _sync_artifacts(self):
        """Symlink (not copy -- some of these are many GB) every artifact
        unlocked as of the current step into the agent's workdir, if not
        already there. Returns the mount_as names unlocked so far (earlier
        artifacts stay listed/available once unlocked, matching how the
        original scenario worked)."""
        if not self.workdir:
            return []
        unlocked = []
        scenario_root = SCENARIO_ARTIFACTS_ROOT / self.manifest["scenario_id"]
        for a in self.manifest.get("artifacts", []):
            if a["unlock_at_step"] > self.idx + 1:
                continue
            mount_as = a.get("mount_as") or Path(a["source_path"]).name
            unlocked.append(mount_as)
            dst = self.workdir / mount_as
            if dst.exists() or dst.is_symlink():
                continue
            src = scenario_root / a["source_path"]
            if not src.exists():
                log_err("proctor: WARNING artifact source missing, not unlocked: %s" % src)
                continue
            os.symlink(src.resolve(), dst)
            log_err("proctor: unlocked artifact %s -> %s" % (mount_as, src))
        return unlocked

    def current_step_public(self):
        base = {
            "scenario_id": self.manifest["scenario_id"],
            "run_id": self.run_id,
            "step_count": len(self.steps),
        }
        if self.complete:
            base.update(
                status="complete",
                step_index=len(self.steps),
                question_text=None,
                action_text=None,
                format_hint=None,
                unlocked_artifacts=self._sync_artifacts(),
                briefing=None,
            )
            return base
        step = self.steps[self.idx]
        base.update(
            status="in_progress" if self.started else "not_started",
            step_index=self.idx + 1,
            question_text=step["question"],
            action_text=step.get("action_text", ""),
            format_hint=step.get("format_hint"),
            unlocked_artifacts=self._sync_artifacts(),
            briefing=self.manifest.get("briefing") if not self.started else None,
        )
        return base

    def submit(self, answer):
        if self.complete:
            return {
                "accepted": False,
                "step_index": len(self.steps),
                "message": "Scenario already complete. No further steps.",
                "status": "scenario_complete",
                "next": None,
            }
        if not answer or not answer.strip():
            return {
                "accepted": False,
                "step_index": self.idx + 1,
                "message": "Empty answer rejected -- step not consumed. Provide a non-empty answer.",
                "status": "next_step",
                "next": self.current_step_public(),
            }
        self.started = True
        step = self.steps[self.idx]
        verdict = grade(answer, step["grading"])
        self._log_result(step["n"], answer, verdict)
        self.idx += 1
        step_n = step["n"]
        if self.idx >= len(self.steps):
            self.complete = True
            return {
                "accepted": True,
                "step_index": step_n,
                "message": "Recorded. Scenario complete -- no further steps.",
                "status": "scenario_complete",
                "next": None,
            }
        return {
            "accepted": True,
            "step_index": step_n,
            "message": "Recorded. Advancing to next step.",
            "status": "next_step",
            "next": self.current_step_public(),
        }

    def _log_result(self, n, answer, verdict):
        rec = {
            "step": n,
            "ts": time.time(),
            "answer": answer,
            "verdict": verdict,
        }
        with open(self.results_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_manifest(path):
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def materialize_shifted_timestamps(manifest, anchor_iso):
    """import_data.py shifts EVERY timestamp in the Elasticsearch data by a
    constant delta = anchor - source_max_ts, so that the scenario always
    "ends now". A manifest question whose correct answer is an absolute
    timestamp from the original dataset (grading.type == "timestamp" with
    shift_from_source: true) must be graded against that SAME shift, or it
    will only ever be correct on the exact day the manifest was authored.

    Only top-level step.grading rules are shifted (not nested inside
    composite/list) -- no current question needs that, and composite parts
    only get their per-part "expected" at grade time by splitting the
    top-level string, so there's no natural load-time hook for them anyway.
    """
    es_cfg = manifest.get("elastic")
    if not es_cfg or not anchor_iso:
        return
    source_max_ts = es_cfg.get("source_max_ts")
    if not source_max_ts:
        return
    anchor_epoch = parse_timestamp_to_epoch(anchor_iso)
    source_epoch = parse_timestamp_to_epoch(source_max_ts)
    if anchor_epoch is None or source_epoch is None:
        log_err("proctor: WARNING could not parse anchor/source_max_ts for timestamp shifting -- "
                "any shift_from_source questions will grade against UN-shifted (likely wrong) values")
        return
    shift_seconds = anchor_epoch - source_epoch

    for step in manifest.get("steps", []):
        rule = step.get("grading") or {}
        if rule.get("type") == "timestamp" and rule.get("shift_from_source"):
            original_epoch = parse_timestamp_to_epoch(rule.get("expected"))
            if original_epoch is None:
                log_err("proctor: WARNING step %s has shift_from_source but unparseable expected %r"
                        % (step.get("n"), rule.get("expected")))
                continue
            rule["expected"] = original_epoch + shift_seconds
            log_err("proctor: shifted step %s expected timestamp by %.0fs" % (step.get("n"), shift_seconds))


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log_err(*a):
    print(*a, file=sys.stderr, flush=True)


def handle_message(msg, state):
    method = msg.get("method")
    has_id = "id" in msg
    msg_id = msg.get("id")

    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "bizone-proctor", "version": "0.1.0"},
                },
            }
        )
        return

    if method == "notifications/initialized":
        return  # notification, no response

    if method == "ping":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        return

    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        return

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            if name == "get_current_step":
                result = state.current_step_public()
            elif name == "submit_answer":
                result = state.submit(arguments.get("answer", ""))
            else:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": "Unknown tool: %s" % name},
                    }
                )
                return
        except Exception as e:  # never let a bug crash the whole proctor mid-run
            log_err("proctor tool error:", repr(e))
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": "internal proctor error: %s" % e}],
                        "isError": True,
                    },
                }
            )
            return
        send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]},
            }
        )
        return

    # unknown method: error only if it expected a reply
    if has_id:
        send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "Method not found: %s" % method}})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--workdir", default=None,
                     help="Agent's sandbox workdir -- required only if the manifest has an "
                          "'artifacts' list, so unlocked artifacts can be symlinked in.")
    ap.add_argument("--anchor", default=None,
                     help="ISO timestamp used as the scenario's 'ends now' anchor for this run "
                          "(same value passed to upload.sh --anchor) -- needed to grade any "
                          "shift_from_source timestamp questions correctly.")
    args = ap.parse_args()

    manifest = load_manifest(args.manifest)
    materialize_shifted_timestamps(manifest, args.anchor)
    state = ProctorState(manifest, args.run_id, args.state_dir, args.workdir)
    log_err(
        "proctor: loaded scenario=%s steps=%d run_id=%s state_dir=%s"
        % (manifest["scenario_id"], len(manifest["steps"]), args.run_id, args.state_dir)
    )

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            log_err("proctor: bad JSON line, ignored:", repr(e))
            continue
        try:
            handle_message(msg, state)
        except Exception as e:
            log_err("proctor: unhandled error:", repr(e))


if __name__ == "__main__":
    main()
