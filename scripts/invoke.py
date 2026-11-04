"""Talk to the deployed Codex runtime.

  ask  "<prompt>" [--new]   InvokeAgentRuntime; keeps session + thread in .session.json
  exec "<command>"          InvokeAgentRuntimeCommand in the same session (no model)
  stop                      StopRuntimeSession (the microVM goes away; files stay)
"""
import json, os, shlex, sys, time, uuid
import boto3

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root
STATE = json.load(open(os.path.join(HERE, ".state.json")))
SESSION_FILE = os.path.join(HERE, ".session.json")
dp = boto3.client("bedrock-agentcore", region_name=STATE["region"])
ARN = STATE["runtime_arn"]


def load_session(new=False):
    if not new and os.path.exists(SESSION_FILE):
        return json.load(open(SESSION_FILE))
    s = {"session_id": f"codex-demo-{uuid.uuid4()}", "thread_id": None}   # >= 33 chars
    json.dump(s, open(SESSION_FILE, "w"))
    return s


def ask(prompt, sess):
    payload = {"prompt": prompt}
    if sess.get("thread_id"):
        payload["thread_id"] = sess["thread_id"]
    t0 = time.time()
    r = dp.invoke_agent_runtime(agentRuntimeArn=ARN, runtimeSessionId=sess["session_id"],
                                payload=json.dumps(payload).encode())
    out = json.loads(r["response"].read())
    out["seconds"] = round(time.time() - t0, 1)
    if out.get("thread_id"):
        sess["thread_id"] = out["thread_id"]
        json.dump(sess, open(SESSION_FILE, "w"))
    return out


def run(command, sess, timeout=120):
    """Commands are not run through a shell, so wrap them in /bin/sh -c."""
    r = dp.invoke_agent_runtime_command(agentRuntimeArn=ARN, runtimeSessionId=sess["session_id"],
                                        body={"command": "/bin/sh -c " + shlex.quote(command), "timeout": timeout})
    text, exit_code = "", None
    for ev in r["stream"]:
        chunk = ev.get("chunk", {})
        if "contentDelta" in chunk:
            text += chunk["contentDelta"].get("stdout", "") + chunk["contentDelta"].get("stderr", "")
        if "contentStop" in chunk:
            exit_code = chunk["contentStop"]["exitCode"]
        for k, v in ev.items():               # stream errors arrive as members, not exceptions
            if k.endswith("Exception"):
                raise RuntimeError(f"{k}: {v}")
    return text, exit_code


def stop(sess):
    dp.stop_runtime_session(agentRuntimeArn=ARN, runtimeSessionId=sess["session_id"])


def show(out):
    for m in out.get("messages", []):
        print(m.strip(), end="\n\n")
    for c in out.get("tool_calls", []):
        if c.get("server") == "gateway" and c.get("tool", "").startswith("repo___"):
            print(f"  [tool] {c['tool']} -> {c['status']}")
    if out.get("error"):
        print("  [error]", out["error"])
    print(f"  (thread {out.get('thread_id')}, rollout from {out.get('rollout_source', 'new')}, {out['seconds']}s)")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("ask", "exec", "stop"):
        sys.exit(__doc__)
    cmd, rest = sys.argv[1], sys.argv[2:]
    new = "--new" in rest
    rest = [a for a in rest if a != "--new"]
    sess = load_session(new)
    if cmd == "ask":
        show(ask(" ".join(rest), sess))
    elif cmd == "exec":
        text, code = run(" ".join(rest), sess)
        print(text, end="")
        print(f"  (exit {code})")
        sys.exit(code or 0)
    else:
        stop(sess)
        print("session stopped:", sess["session_id"])
