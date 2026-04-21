#!/usr/bin/env bash
# Source from the repository root:  . ./scripts/load-env.sh
# Loads .env and exports a default EKS_MCP_BRIDGE_IMAGE when unset.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "Source this file from the repository root, e.g.:  . ./scripts/load-env.sh" >&2
  exit 1
fi
set -euo pipefail
LoadEnv__ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "${LoadEnv__ROOT}/.env" ]]; then
  echo "load-env: missing ${LoadEnv__ROOT}/.env (copy env.example to .env and set values)" >&2
  return 1 2>/dev/null || exit 1
fi
set -a
# shellcheck source=/dev/null
source "${LoadEnv__ROOT}/.env"
set +a
: "${AWS_REGION:?set AWS_REGION in .env}"
: "${AWS_ACCOUNT_ID:?set AWS_ACCOUNT_ID in .env}"
export EKS_MCP_BRIDGE_IMAGE="${EKS_MCP_BRIDGE_IMAGE:-${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/eks-mcp-bridge:latest}"
unset LoadEnv__ROOT
