#!/usr/bin/env python3
"""Call manage_k8s_resource patch via bridge LoadBalancer."""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone


def curl_session_and_call(base: str, call_body: dict) -> dict:
    import subprocess as sp

    init = sp.check_output(
        [
            "curl",
            "-s",
            "-m",
            "25",
            "-D",
            "-",
            "-X",
            "POST",
            base,
            "-H",
            "Content-Type: application/json",
            "-H",
            "Accept: application/json, text/event-stream",
            "-d",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test-mcp-write", "version": "1.0"},
                    },
                }
            ),
        ],
        text=True,
    )
    sid = ""
    for line in init.splitlines():
        if line.lower().startswith("mcp-session-id:"):
            sid = line.split(":", 1)[1].strip()
            break
    if not sid:
        print("No session id", file=sys.stderr)
        print(init[:800], file=sys.stderr)
        raise SystemExit(1)

    out = sp.check_output(
        [
            "curl",
            "-s",
            "-m",
            "120",
            "-X",
            "POST",
            base,
            "-H",
            "Content-Type: application/json",
            "-H",
            "Accept: application/json, text/event-stream",
            "-H",
            f"Mcp-Session-Id: {sid}",
            "-d",
            json.dumps(call_body),
        ],
        text=True,
    )
    clean = re.sub(r"[\x00-\x1f\x7f]", "", out)
    return json.loads(clean)


def main() -> int:
    lb = subprocess.check_output(
        [
            "kubectl",
            "get",
            "svc",
            "eks-mcp-bridge",
            "-n",
            "eks-mcp-bridge",
            "-o",
            "jsonpath={.status.loadBalancer.ingress[0].hostname}",
        ],
        text=True,
    ).strip()
    base = f"http://{lb}:8888/mcp"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"kubectl.kubernetes.io/restartedAt": ts},
                }
            }
        }
    }
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "manage_k8s_resource",
            "arguments": {
                "operation": "patch",
                "cluster_name": "ram-eks",
                "kind": "Deployment",
                "api_version": "apps/v1",
                "namespace": "default",
                "name": "cart",
                "body": json.dumps(patch),
            },
        },
    }
    result = curl_session_and_call(base, body)
    print(json.dumps(result, indent=2)[:6000])

    if result.get("result", {}).get("isError") or "error" in result:
        return 2

    ann = subprocess.check_output(
        [
            "kubectl",
            "get",
            "deploy",
            "cart",
            "-n",
            "default",
            "-o",
            "jsonpath={.spec.template.metadata.annotations.kubectl\\.kubernetes\\.io/restartedAt}",
        ],
        text=True,
    ).strip()
    print(f"\nOK: cart deployment restartedAt = {ann}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
