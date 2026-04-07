# AWS Managed EKS MCP Server -> Elastic Agent Builder

Bridge the AWS fully managed EKS MCP server (preview) to Elastic Agent Builder via an SSE/HTTP proxy running on EKS. This gives Elastic AI agents direct access to 20 EKS management, troubleshooting, and observability tools through natural language.

## Architecture

```
Elastic Cloud (Kibana 9.3+)            EKS Cluster (ap-south-1)                AWS Managed Service
+-----------------------+     HTTPS     +---------------------------+   SigV4   +---------------------+
| Agent Builder         |  + Bearer     | Pod: eks-mcp-bridge       |  signed   | EKS MCP Server      |
|   -> MCP Connector    | ----------->  |   mcp-proxy (SSE/HTTP)    | -------> | eks-mcp.region.     |
|                       |    Token      |     -> mcp-proxy-for-aws  |          |   api.aws/mcp       |
+-----------------------+               |          (stdio)          |          +---------------------+
                                        +---------------------------+
                                        | K8s Service (LoadBalancer)|
                                        | IRSA (IAM Role)          |
                                        +---------------------------+
```

**Why a bridge pod?** The managed EKS MCP server authenticates via AWS SigV4 through a stdio-based proxy (`mcp-proxy-for-aws`). Elastic's MCP connector requires an HTTP/SSE endpoint. The bridge pod runs `mcp-proxy` to expose the stdio proxy as an SSE/HTTP endpoint.

**Auth layers:**
- **Outer (Elastic -> Bridge):** Bearer token on the mcp-proxy SSE endpoint
- **Inner (Bridge -> AWS):** AWS SigV4 via mcp-proxy-for-aws, credentials from IRSA

## Prerequisites

- AWS CLI configured with credentials
- Python 3.10+ and [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Docker
- kubectl configured for your EKS cluster
- eksctl
- Elastic Cloud with Kibana 9.3+ (for MCP connector support)

## Step 1: IAM Permissions (For testing / local validation only)

Attach the AWS managed policy for read-only access to the managed EKS MCP server:

```bash
aws iam attach-role-policy \
  --role-name <your-iam-role> \
  --policy-arn arn:aws:iam::aws:policy/AmazonEKSMCPReadOnlyAccess
```

This grants `eks-mcp:InvokeMcp` (initialization + tool discovery) and `eks-mcp:CallReadOnlyTool`.

For write access (cluster creation, resource management), create a custom policy with `eks-mcp:CallPrivilegedTool` plus EKS, EC2, IAM, and CloudFormation permissions. See the [AWS docs](https://docs.aws.amazon.com/eks/latest/userguide/eks-mcp-getting-started.html) for the full write policy.

**Verify:**

```bash
aws sts get-caller-identity
aws eks list-clusters --region ap-south-1
```

## Step 2: Local Validation (Cursor) (For testing / local validation only)

Validate the managed service works locally before deploying the bridge. The `.cursor/mcp.json` in this repo is pre-configured to work with ap-south-1 region. Modify it to use the AWS region of your choice. Open this repo in Cursor, then in agent chat ask:

- "What EKS MCP tools are available?"
- "List all EKS clusters in ap-south-1"

If using a different region or AWS profile, edit `.cursor/mcp.json` accordingly.

## Step 3: Build and Push Docker Image

```bash
cd docker/

# Build for amd64 (EKS nodes)
docker buildx build --platform linux/amd64 -t eks-mcp-bridge:latest .

# Local test (mount AWS creds)
docker run -p 8888:8888 \
  -e API_ACCESS_TOKEN="test-token" \
  -e AWS_REGION="ap-south-1" \
  -v ~/.aws:/root/.aws:ro \
  eks-mcp-bridge:latest

# Verify (in another terminal)
curl -s -m 15 -X POST http://localhost:8888/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

Push to ECR:

```bash
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION="ap-south-1"

aws ecr create-repository --repository-name eks-mcp-bridge --region $AWS_REGION 2>/dev/null
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

docker buildx build --platform linux/amd64 \
  -t ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/eks-mcp-bridge:latest \
  --push .
```

## Step 4: Create IRSA Service Account

The bridge pod needs AWS credentials to sign SigV4 requests to the managed endpoint. Create an IRSA-enabled service account:

```bash
kubectl create namespace eks-mcp-bridge

eksctl create iamserviceaccount \
  --name eks-mcp-bridge-sa \
  --namespace eks-mcp-bridge \
  --cluster <your-cluster-name> \
  --region ap-south-1 \
  --attach-policy-arn arn:aws:iam::aws:policy/AmazonEKSMCPReadOnlyAccess \
  --approve
```

Verify the annotation:

```bash
kubectl get sa eks-mcp-bridge-sa -n eks-mcp-bridge -o yaml | grep eks.amazonaws.com/role-arn
```

## Step 5: Map IRSA Role in aws-auth and Apply RBAC

The managed EKS MCP server uses the caller's IAM identity to make Kubernetes API calls on the target cluster. The IRSA role from Step 4 must be mapped to a Kubernetes identity with read permissions, otherwise K8s API calls (e.g. `get_pod_logs`) will fail with 401 Unauthorized.

### 5a. Add the IRSA role to the aws-auth ConfigMap

Get the IRSA role ARN:

```bash
kubectl get sa eks-mcp-bridge-sa -n eks-mcp-bridge \
  -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}'
```

Edit the `aws-auth` ConfigMap to add the role:

```bash
kubectl edit configmap aws-auth -n kube-system
```

Add this entry under `mapRoles` (replace the `rolearn` with your actual value):

```yaml
- groups:
  - eks-mcp-readers
  rolearn: arn:aws:iam::<account-id>:role/<irsa-role-name>
  username: eks-mcp-bridge
```

### 5b. Apply the RBAC ClusterRole and ClusterRoleBinding

```bash
kubectl apply -f kubernetes/rbac.yaml
```

This creates a `eks-mcp-reader` ClusterRole with read-only access to pods, logs, events, deployments, services, nodes, and more, and binds it to the `eks-mcp-readers` group.

## Step 6: Deploy to EKS

Before applying, update `kubernetes/manifests.yaml`:
- Replace the `image:` field with your ECR image URI
- Replace the `API_ACCESS_TOKEN` value with a strong token (`openssl rand -base64 32`)

```bash
kubectl apply -f kubernetes/manifests.yaml
kubectl get pods -n eks-mcp-bridge
kubectl get svc -n eks-mcp-bridge
```

**Test via port-forward:**

```bash
kubectl port-forward -n eks-mcp-bridge svc/eks-mcp-bridge 8888:8888

# In another terminal
curl -s -m 15 -X POST http://localhost:8888/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

**Test via LoadBalancer:**

```bash
LB_HOST=$(kubectl get svc eks-mcp-bridge -n eks-mcp-bridge -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')

curl -s -m 15 -X POST "http://${LB_HOST}:8888/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

## Step 7: Connect to Elastic Agent Builder

### 7a. Create MCP Connector in Kibana

1. Go to **Stack Management > Connectors > Create connector**
2. Select **MCP** connector type
3. Configure:
   - **Name:** `AWS EKS MCP (Managed)`
   - **Server URL:** `http://<LoadBalancer-hostname>:8888/mcp`
   - **HTTP Headers** (secret type): Key=`Authorization`, Value=`Bearer <your-bearer-token>`
4. Click **Test** to verify the connection

### 7b. Bulk Import MCP Tools

1. Go to **Agent Builder > Tools**
2. Click **Manage MCP > Bulk import MCP tools**
3. Select the `AWS EKS MCP (Managed)` connector
4. Select the tools to import
5. Set namespace prefix: `eks`

### 7c. Test End-to-End

Create or edit an agent, add the imported EKS tools, then test in chat:

- "List all EKS clusters in ap-south-1"
- "Show me pods in the default namespace"
- "What events are happening in my cluster?"
- "Search the EKS troubleshooting guide for pod networking issues"

## Available Tools (20)

| Category | Tools |
|---|---|
| Cluster Management | `list_eks_resources`, `describe_eks_resource`, `manage_eks_stacks`, `get_eks_insights`, `get_eks_vpc_config` |
| Kubernetes Resources | `list_k8s_resources`, `read_k8s_resource`, `manage_k8s_resource`, `apply_yaml`, `generate_app_manifest`, `list_api_versions` |
| Troubleshooting | `get_pod_logs`, `get_k8s_events`, `get_cloudwatch_logs`, `get_cloudwatch_metrics`, `get_eks_metrics_guidance` |
| Documentation | `search_eks_documentation`, `search_eks_troubleshooting_guide` |
| IAM / Security | `get_policies_for_role`, `add_inline_policy` |

## Testing Checklist

- [ ] IAM permissions verified -- `aws eks list-clusters` succeeds
- [ ] Managed EKS MCP server works locally -- Cursor agent lists clusters and tools
- [ ] Docker image builds and runs -- `docker run` + `curl` test passes
- [ ] IRSA service account created -- annotation shows role ARN
- [ ] IRSA role mapped in aws-auth -- `kubectl get cm aws-auth -n kube-system` shows the role
- [ ] RBAC applied -- `kubectl get clusterrole eks-mcp-reader` exists
- [ ] K8s deployment healthy -- pod running, port-forward test passes
- [ ] LoadBalancer reachable -- `curl` to external endpoint works
- [ ] Elastic MCP connector connects -- "Test connection" in Kibana succeeds
- [ ] Tools are discovered -- `listTools` returns 20 EKS MCP tools
- [ ] Agent chat works -- Agent Builder can query EKS cluster

## Security Considerations

- **Bearer token:** Generate with `openssl rand -base64 32`. Store in K8s Secret. Rotate periodically.
- **Network:** Restrict LoadBalancer security group to Elastic Cloud IP ranges only.
- **TLS:** For production, add an Ingress with TLS termination (ACM cert + ALB Ingress Controller).
- **IRSA:** Least-privilege -- only `AmazonEKSMCPReadOnlyAccess` unless write tools are needed.
- **K8s RBAC:** The `eks-mcp-reader` ClusterRole grants read-only access. For write tools, extend the role or create a separate one with write verbs.
- **Read-only mode:** Add `--read-only` flag to `mcp-proxy-for-aws` in the Dockerfile to restrict to read-only tools.
- **CloudTrail:** The managed service automatically logs all tool calls for auditing.
