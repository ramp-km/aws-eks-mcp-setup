#!/usr/bin/env python3
"""
Load .env from repo root, apply defaults, substitute ${VAR} in templates, write outputs.

No gettext/envsubst required. Templates use ${UPPER_SNAKE} place holders only.
"""
from __future__ import annotations

import argparse
import re
import secrets
import string
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV = REPO / ".env"
MCP_TEMPLATE = REPO / ".cursor" / "mcp.json.template"
MANIFEST_TEMPLATE = REPO / "kubernetes" / "manifests.envsubst.yaml"


def load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        out[k] = v
    return out


def expand(template: str, env: dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in env or str(env.get(key, "")).strip() == "":
            raise KeyError(
                f"Missing or empty environment variable: {key} "
                f"(set it in {ENV} or export it before running this script)"
            )
        return str(env[key])

    return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", repl, template)


def main() -> int:
    p = argparse.ArgumentParser(description="Render mcp.json and K8s manifests from .env")
    p.add_argument(
        "target",
        nargs="?",
        choices=("all", "mcp", "manifests"),
        default="all",
        help="What to render (default: all)",
    )
    args = p.parse_args()
    do_mcp = args.target in ("all", "mcp")
    do_m = args.target in ("all", "manifests")

    env: dict[str, str] = {k: v for k, v in __import__("os").environ.items() if v is not None}
    file_env = load_dotenv(ENV)
    for k, v in file_env.items():
        if k not in env or not str(env.get(k, "")).strip():
            env[k] = v

    for required in ("AWS_REGION", "AWS_ACCOUNT_ID"):
        if not env.get(required, "").strip():
            print(
                f"Error: {required} must be set in {ENV} (see env.example).",
                file=sys.stderr,
            )
            return 1

    account = env["AWS_ACCOUNT_ID"].strip()
    region = env["AWS_REGION"].strip()
    ecr_default = f"{account}.dkr.ecr.{region}.amazonaws.com/eks-mcp-bridge:latest"
    if not env.get("EKS_MCP_BRIDGE_IMAGE", "").strip():
        env["EKS_MCP_BRIDGE_IMAGE"] = ecr_default
    if do_m and not env.get("API_ACCESS_TOKEN", "").strip():
        alphabet = string.ascii_letters + string.digits
        token = "".join(secrets.choice(alphabet) for _ in range(48))
        print(
            f"API_ACCESS_TOKEN not set; using a generated value (set API_ACCESS_TOKEN in {ENV} to pin it).",
            file=sys.stderr,
        )
        env["API_ACCESS_TOKEN"] = token

    if do_mcp:
        if not MCP_TEMPLATE.is_file():
            print(f"Missing {MCP_TEMPLATE}", file=sys.stderr)
            return 1
        t = MCP_TEMPLATE.read_text()
        out = expand(t, env)
        mcp_path = REPO / ".cursor" / "mcp.json"
        mcp_path.write_text(out, encoding="utf-8")
        print(f"Wrote {mcp_path}", file=sys.stderr)

    if do_m:
        if not MANIFEST_TEMPLATE.is_file():
            print(f"Missing {MANIFEST_TEMPLATE}", file=sys.stderr)
            return 1
        t = MANIFEST_TEMPLATE.read_text()
        out = expand(t, env)
        out_path = REPO / "kubernetes" / "manifests.rendered.yaml"
        out_path.write_text(out, encoding="utf-8")
        print(f"Wrote {out_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
