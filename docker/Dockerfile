FROM python:3.12-slim

RUN pip install uv && \
    uv pip install --system mcp-proxy mcp-proxy-for-aws

EXPOSE 8888

ENTRYPOINT ["mcp-proxy", \
            "--port", "8888", "--host", "0.0.0.0", \
            "--sse-port", "8888", "--sse-host", "0.0.0.0", \
            "--pass-environment", \
            "--", \
            "mcp-proxy-for-aws", \
            "https://eks-mcp.<aws-region>.api.aws/mcp", \
            "--service", "eks-mcp", \
            "--region", "<aws-region>"]
