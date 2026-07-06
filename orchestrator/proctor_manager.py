"""Runs the proctor as an HTTP service ON THE HOST for a contained run. This
is the crux of the isolation: the proctor holds the answer key, and it stays
on the host, outside the agent's container -- the container reaches it over
the network (host.docker.internal) and can only call the two MCP tools, one
question at a time. The manifest / answer key never enters the container.
"""
import socket
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS = REPO_ROOT / "harness"
PROCTOR_SCRIPT = HARNESS / "proctor" / "proctor_server.py"


class ProctorError(RuntimeError):
    pass


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start(manifest_path, run_id, state_dir, workdir, anchor, timeout=15):
    """Spawn the HTTP proctor on the host, bound to 0.0.0.0 so the agent's
    container can reach it via host.docker.internal. Returns a handle dict."""
    port = _free_port()
    args = [
        "python3", str(PROCTOR_SCRIPT),
        "--manifest", str(Path(manifest_path).resolve()),
        "--run-id", run_id,
        "--state-dir", str(Path(state_dir).resolve()),
        "--workdir", str(Path(workdir).resolve()),
        "--transport", "http", "--host", "0.0.0.0", "--port", str(port),
    ]
    if anchor:
        args += ["--anchor", anchor]

    log_path = Path(state_dir) / "proctor.log"
    log_f = open(log_path, "wb")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=log_f)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            log_f.close()
            raise ProctorError("proctor exited early (code %s) -- see %s" % (proc.returncode, log_path))
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return {"proc": proc, "port": port, "log_f": log_f}
        except OSError:
            time.sleep(0.3)
    stop({"proc": proc, "log_f": log_f})
    raise ProctorError("proctor did not open port %d within %ds -- see %s" % (port, timeout, log_path))


def stop(handle):
    proc = handle.get("proc")
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    log_f = handle.get("log_f")
    if log_f and not log_f.closed:
        log_f.close()
