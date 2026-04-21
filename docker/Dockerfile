FROM python:3.12-slim

RUN pip install uv && \
    uv pip install --system mcp-proxy mcp-proxy-for-aws

# Do not pass --read-only to mcp-proxy-for-aws here; that blocks CallPrivilegedTool (manage_k8s_resource, apply_yaml, etc.).
# Region comes from the container environment (AWS_REGION); one image works in any region.
EXPOSE 8888

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
