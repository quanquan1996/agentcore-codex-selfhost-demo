"""End-to-end check of the deployed demo (uses its own session, not .session.json).

1. Codex writes code                          InvokeAgentRuntime
2. Run the tests without the model            InvokeAgentRuntimeCommand
3. Repo tools through Gateway                 Identity injects the token, Policy allows/denies
4. Stop the session, then resume              session storage
5. New Runtime version resets session storage; resume from the S3 Files archive
Each step prints PASS/FAIL; the exit code is the number of failures.
"""
import json, os, sys, time, uuid
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import invoke   # noqa: E402

st = invoke.STATE
cp = boto3.client("bedrock-agentcore-control", region_name=st["region"])
sess = {"session_id": f"codex-e2e-{uuid.uuid4()}", "thread_id": None}
fails = 0


def check(name, ok, detail=""):
    global fails
    fails += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def gateway_call(tool, args):
    """Call the Gateway directly, bypassing the agent, signed with your own credentials."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}).encode()
    req = AWSRequest("POST", st["gateway_url"], data=body,
                     headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    SigV4Auth(boto3.Session().get_credentials(), "bedrock-agentcore", st["region"]).add_auth(req)
    r = urllib.request.urlopen(urllib.request.Request(st["gateway_url"], data=body, headers=dict(req.headers)))
    return json.loads(r.read())


print("session:", sess["session_id"])

out = invoke.ask("Create calc.py with a function add(a, b), and test_calc.py with unittest tests for it. "
                 "Do not run the tests. Reply with the file names only.", sess)
invoke.show(out)
check("1 codex writes code", out.get("exit_code") == 0 and "calc.py" in " ".join(out["messages"]), f"{out['seconds']}s")

text, code = invoke.run("cd /mnt/workspace/project && python3.12 -m unittest -v 2>&1", sess)
print(text)
check("2 tests run without the model", code == 0 and "OK" in text, f"exit={code}")

out = invoke.ask("Using the gateway repo tools on repository 'demo': (1) list pull requests, "
                 "(2) open a pull request titled 'Add calc' from 'feature/calc' to 'main', "
                 "(3) merge that pull request, (4) delete branch 'feature/calc'. "
                 "Call each tool once and report the exact result or error of each call.", sess)
invoke.show(out)
called = {c["tool"] for c in out["tool_calls"] if c.get("status") == "completed"}
check("3a list + create PR allowed", {"repo___listPullRequests", "repo___createPullRequest"} <= called)
check("3b merge / delete never called", not ({"repo___mergePullRequest", "repo___deleteBranch"} & called))
err = gateway_call("repo___mergePullRequest", {"repo": "demo", "number": 1}).get("error", {})
check("3c direct merge call denied by policy", err.get("code") == -32002, err.get("message", "")[:90])

invoke.stop(sess)
time.sleep(20)
out = invoke.ask("Without reading any files: which files did you create earlier?", sess)
invoke.show(out)
check("4 resume after StopRuntimeSession", out.get("rollout_source") == "local" and "calc" in " ".join(out["messages"]))

rt = cp.get_agent_runtime(agentRuntimeId=st["runtime_id"])
net = rt["networkConfiguration"]
net.get("networkModeConfig", {}).pop("requireServiceS3Endpoint", None)   # read-only after create
env = dict(rt.get("environmentVariables") or {}, DEPLOY_MARK=str(int(time.time())))
cp.update_agent_runtime(agentRuntimeId=st["runtime_id"], roleArn=rt["roleArn"],
                        agentRuntimeArtifact=rt["agentRuntimeArtifact"], networkConfiguration=net,
                        filesystemConfigurations=rt["filesystemConfigurations"], environmentVariables=env,
                        lifecycleConfiguration=rt["lifecycleConfiguration"])
time.sleep(5)
while cp.get_agent_runtime(agentRuntimeId=st["runtime_id"])["status"] != "READY":
    time.sleep(10)
print("runtime version:", cp.get_agent_runtime(agentRuntimeId=st["runtime_id"])["agentRuntimeVersion"])
try:
    invoke.stop(sess)
except Exception as e:      # the old microVM may already be gone
    print("stop:", type(e).__name__)
time.sleep(20)
text, _ = invoke.run("ls -A /mnt/workspace/project | wc -l", sess)
out = invoke.ask("Without reading any files: what was the title of the pull request you opened?", sess)
invoke.show(out)
check("5 resume after a new Runtime version", text.strip() == "0" and out.get("rollout_source") == "archive"
      and "Add calc" in " ".join(out["messages"]), f"project files left: {text.strip()}")

print(f"\n{fails} failure(s)")
sys.exit(fails)
