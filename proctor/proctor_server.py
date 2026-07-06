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
        """Make every artifact unlocked as of the current step available in
        the agent's workdir, if not already there. Returns the mount_as names
        unlocked so far (earlier artifacts stay available once unlocked).

        HARDLINK (not symlink) is deliberate and load-bearing for isolation:
        a symlink would let the agent `readlink`/`realpath` it and discover
        the real source path -- which sits inside the project tree next to the
        plaintext answer key. A hardlink is just another directory entry for
        the same inode with NO back-reference to the source's location, so it
        exposes nothing. It also works across a Docker bind-mount (the agent
        sees a real file, not a dangling host symlink). Falls back to a copy
        only across filesystems, where hardlinks aren't possible."""
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
            src = (scenario_root / a["source_path"]).resolve()  # resolve() collapses any operator symlink to the real file
            if not src.exists():
                log_err("proctor: WARNING artifact source missing, not unlocked: %s" % src)
                continue
            try:
                os.link(src, dst)
                log_err("proctor: unlocked artifact %s (hardlink)" % mount_as)
            except OSError:
                import shutil as _sh
                _sh.copy2(src, dst)
                log_err("proctor: unlocked artifact %s (copy -- cross-filesystem)" % mount_as)
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


def process_message(msg, state):
    """Pure JSON-RPC handler: returns the response dict, or None for
    notifications / messages that expect no reply. Transport-agnostic -- used
    by both the stdio loop and the HTTP server."""
    method = msg.get("method")
    has_id = "id" in msg
    msg_id = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "bizone-proctor", "version": "0.1.0"},
            },
        }

    if method == "notifications/initialized":
        return None  # notification, no response

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}

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
                return {"jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": -32601, "message": "Unknown tool: %s" % name}}
        except Exception as e:  # never let a bug crash the whole proctor mid-run
            log_err("proctor tool error:", repr(e))
            return {"jsonrpc": "2.0", "id": msg_id,
                    "result": {"content": [{"type": "text", "text": "internal proctor error: %s" % e}],
                               "isError": True}}
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}}

    if has_id:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": "Method not found: %s" % method}}
    return None


def run_stdio(state):
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
            resp = process_message(msg, state)
            if resp is not None:
                send(resp)
        except Exception as e:
            log_err("proctor: unhandled error:", repr(e))


def run_http(state, host, port):
    """Serve MCP over Streamable HTTP so the proctor can run on the HOST,
    holding the answer key, while a containerized agent calls it over the
    network -- the manifest never enters the agent's container. Minimal
    JSON-response implementation (no server-initiated SSE needed for a
    tools-only server)."""
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, status, obj=None):
            body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            if obj is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # Advertise a session id -- some MCP HTTP clients require the header.
            self.send_header("Mcp-Session-Id", state.run_id)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                msg = json.loads(raw.decode("utf-8"))
            except Exception:
                self._reply(400, {"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": "Parse error"}})
                return
            # A JSON-RPC notification (no id) -> 202 Accepted, empty body.
            resp = process_message(msg, state)
            if resp is None:
                self._reply(202, None)
            else:
                self._reply(200, resp)

        def do_GET(self):
            # Clients may open a GET for a server->client SSE stream; we don't
            # push, so decline cleanly.
            self._reply(405, {"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32000, "message": "SSE stream not supported"}})

        def log_message(self, *a):
            pass  # keep the proctor quiet; it logs to stderr via log_err

    httpd = http.server.ThreadingHTTPServer((host, port), Handler)
    log_err("proctor: HTTP transport listening on %s:%d (path: any; POST JSON-RPC)" % (host, port))
    httpd.serve_forever()


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
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                     help="stdio (default; proctor is a subprocess of the agent CLI) or http "
                          "(proctor runs on the host as a network service so a containerized "
                          "agent can call it without the manifest ever entering the container).")
    ap.add_argument("--host", default="0.0.0.0", help="HTTP bind host (http transport only).")
    ap.add_argument("--port", type=int, default=0, help="HTTP bind port (http transport only).")
    args = ap.parse_args()

    manifest = load_manifest(args.manifest)
    materialize_shifted_timestamps(manifest, args.anchor)
    state = ProctorState(manifest, args.run_id, args.state_dir, args.workdir)
    log_err(
        "proctor: loaded scenario=%s steps=%d run_id=%s transport=%s"
        % (manifest["scenario_id"], len(manifest["steps"]), args.run_id, args.transport)
    )

    if args.transport == "http":
        run_http(state, args.host, args.port)
    else:
        run_stdio(state)


if __name__ == "__main__":
    main()
