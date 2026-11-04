"""AgentCore Runtime HTTP server that runs Codex CLI (`codex exec`).

POST /invocations  {"prompt": "...", "thread_id": "<optional, to resume>"}
GET  /ping         Healthy / HealthyBusy (AgentCore HTTP protocol contract)

Storage layout
  /mnt/workspace  AgentCore managed session storage (per session, 14-day idle expiry,
                  reset when the runtime version changes): project files + Codex rollouts
  /mnt/archive    optional Amazon S3 Files access point (shared, durable): rollout archive
  CODEX_HOME      local disk: Codex SQLite state; only CODEX_HOME/sessions is a symlink
"""
import glob, json, os, shutil, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WORKSPACE = os.environ.get("WORKSPACE", "/mnt/workspace")
ARCHIVE = os.environ.get("ARCHIVE", "/mnt/archive")
CODEX_HOME = os.environ.get("CODEX_HOME", "/home/agent/.codex")
MODEL = os.environ.get("CODEX_MODEL", "global.openai.gpt-6.1-sol")
REGION = os.environ.get("AWS_REGION", "us-east-1")
SESSIONS = os.path.join(WORKSPACE, ".codex-sessions")
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

_busy = 0
_state_lock = threading.Lock()
_thread_locks = {}
_last_change = int(time.time())


def _set_busy(delta):
    global _busy, _last_change
    with _state_lock:
        before = _busy > 0
        _busy += delta
        if (_busy > 0) != before:          # only on a real status change
            _last_change = int(time.time())


def _thread_lock(thread_id):
    with _state_lock:
        return _thread_locks.setdefault(thread_id or "new", threading.Lock())


def prepare_dirs():
    """Persist only Codex rollouts; keep SQLite state on local disk."""
    os.makedirs(os.path.join(WORKSPACE, "project"), exist_ok=True)
    os.makedirs(SESSIONS, exist_ok=True)
    os.makedirs(CODEX_HOME, exist_ok=True)
    link = os.path.join(CODEX_HOME, "sessions")
    if not os.path.islink(link):
        os.symlink(SESSIONS, link)


def _archive_dir(session_id):
    if not os.path.isdir(ARCHIVE):
        return None
    d = os.path.join(ARCHIVE, "rollouts", session_id or "default")
    os.makedirs(d, exist_ok=True)
    return d


def restore_rollout(thread_id, session_id):
    """If session storage was reset, bring the rollout back from the S3 Files archive."""
    if glob.glob(os.path.join(SESSIONS, "**", f"*{thread_id}.jsonl"), recursive=True):
        return "local"
    adir = _archive_dir(session_id)
    for src in glob.glob(os.path.join(adir or "/nonexistent", "**", f"*{thread_id}.jsonl"), recursive=True):
        dst = os.path.join(SESSIONS, os.path.relpath(src, adir))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        return "archive"
    return "missing"


def archive_rollout(thread_id, session_id):
    adir = _archive_dir(session_id)
    if not adir or not thread_id:
        return False
    for src in glob.glob(os.path.join(SESSIONS, "**", f"*{thread_id}.jsonl"), recursive=True):
        dst = os.path.join(adir, os.path.relpath(src, SESSIONS))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        return True
    return False


def run_codex(prompt, thread_id=None, session_id=None):
    result = {"thread_id": thread_id, "messages": [], "tool_calls": [], "usage": None}
    cmd = ["codex", "exec"]
    if thread_id:
        result["rollout_source"] = restore_rollout(thread_id, session_id)
        cmd += ["resume"]
    cmd += ["--json", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model", MODEL,
            "-c", 'model_provider="amazon-bedrock-runtime"',
            "-c", f'model_providers.amazon-bedrock-runtime.aws.region="{REGION}"']
    if thread_id:
        cmd += [thread_id]
    cmd += ["--", prompt]
    env = dict(os.environ, CODEX_HOME=CODEX_HOME)
    # stdin must be closed, otherwise `codex exec` waits for input forever
    proc = subprocess.run(cmd, cwd=os.path.join(WORKSPACE, "project"), env=env,
                          stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=3600)
    result["exit_code"] = proc.returncode
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item", {})
        if ev.get("type") == "thread.started":
            result["thread_id"] = ev["thread_id"]
        elif ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            result["messages"].append(item["text"])
        elif ev.get("type") == "item.completed" and item.get("type") == "mcp_tool_call":
            result["tool_calls"].append({k: item.get(k) for k in ("server", "tool", "status", "error")})
        elif ev.get("type") == "turn.completed":
            result["usage"] = ev.get("usage")
        elif ev.get("type") == "turn.failed":
            result["error"] = ev.get("error", {}).get("message")
    if proc.returncode != 0:
        result["stderr"] = proc.stderr[-2000:]
    result["archived"] = archive_rollout(result["thread_id"], session_id)
    return result


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/ping":
            status = "HealthyBusy" if _busy > 0 else "Healthy"
            self._send(200, {"status": status, "time_of_last_update": _last_change})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/invocations":
            return self._send(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if not body.get("prompt"):
            return self._send(400, {"error": "prompt is required"})
        thread_id = body.get("thread_id")
        _set_busy(+1)
        try:
            with _thread_lock(thread_id):      # never resume one thread twice at once
                self._send(200, run_codex(body["prompt"], thread_id, self.headers.get(SESSION_HEADER)))
        finally:
            _set_busy(-1)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    prepare_dirs()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
