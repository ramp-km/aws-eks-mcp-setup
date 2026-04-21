#!/bin/sh
set -e
if [ -z "${AWS_REGION}" ]; then
  echo "ERROR: AWS_REGION must be set (e.g. in the pod env in kubernetes/manifests.envsubst.yaml)." >&2
  exit 1
fi
exec mcp-proxy \
  --port 8888 --host 0.0.0.0 \
  --sse-port 8888 --sse-host 0.0.0.0 \
  --pass-environment \
  -- \
  mcp-proxy-for-aws \
  "https://eks-mcp.${AWS_REGION}.api.aws/mcp" \
  --service eks-mcp \
  --region "${AWS_REGION}"
