"""Mock code-repository API behind Amazon API Gateway (HTTP API).

It only accepts requests that carry the bearer token stored in AgentCore Identity.
The agent never sees that token: AgentCore Gateway injects it on the way out.
"""
import json, os, time

TOKEN = os.environ["REPO_API_TOKEN"]


def _resp(code, body):
    return {"statusCode": code, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def lambda_handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if headers.get("authorization") != f"Bearer {TOKEN}":
        return _resp(401, {"error": "invalid or missing token"})
    method = event["requestContext"]["http"]["method"]
    parts = event["rawPath"].strip("/").split("/")      # repos/{repo}/...
    body = json.loads(event.get("body") or "{}")
    print(json.dumps({"call": f"{method} {event['rawPath']}", "body": body}))
    if method == "GET" and parts[2:] == ["pulls"]:
        return _resp(200, {"pulls": [{"number": 7, "title": "Fix typo", "state": "open"}]})
    if method == "POST" and parts[2:] == ["pulls"]:
        return _resp(201, {"number": int(time.time()) % 1000, "title": body.get("title"),
                           "head": body.get("head"), "base": body.get("base"), "state": "open"})
    if method == "PUT" and len(parts) == 5 and parts[4] == "merge":
        return _resp(200, {"number": int(parts[3]), "merged": True})
    if method == "DELETE" and len(parts) == 4 and parts[2] == "branches":
        return _resp(200, {"branch": parts[3], "deleted": True})
    return _resp(404, {"error": "not found"})
