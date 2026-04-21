# AWS Managed EKS MCP Server -> Elastic Agent Builder

Bridge the AWS fully managed EKS MCP server (preview) to Elastic Agent Builder via an SSE/HTTP proxy running on EKS. This gives Elastic AI agents direct access to 20 EKS management, troubleshooting, and observability tools through natural language.

## Architecture

```
Elastic Cloud (Kibana 9.3+)            EKS Cluster (<your-aws-region>)                AWS Managed Service
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

## Configure AWS region and account (set once per shell)

1. In the **repository root**, copy the sample file and set your values in `.env` (this file is gitignored):

```bash
cp env.example .env
```

2. Set at least `AWS_REGION` and `AWS_ACCOUNT_ID` (you can fill `AWS_ACCOUNT_ID` with `export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)`). Optionally set `EKS_CLUSTER_NAME` for the commands in Step 3 and Step 5. Set `API_ACCESS_TOKEN` before rendering manifests in Step 5, or let the render script generate one (see below).

3. In each terminal where you run the commands in this guide, **load the variables** from the repository root. Either:

```bash
set -a && source .env && set +a
```

or (Bash only):

```bash
. ./scripts/load-env.sh
```

The helper script also exports a default `EKS_MCP_BRIDGE_IMAGE` of `${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/eks-mcp-bridge:latest` if unset.

4. **Templated config files** read the same values so you do not have to re-type region and account in JSON or YAML. From the repository root, after your `.env` is loaded, run when you need to refresh generated files:

```bash
python3 scripts/render_from_env.py
```

- Writes **`.cursor/mcp.json`** from [`.cursor/mcp.json.template`](.cursor/mcp.json.template) (for optional local validation with Cursor).
- Writes **`kubernetes/manifests.rendered.yaml`** from [`kubernetes/manifests.envsubst.yaml`](kubernetes/manifests.envsubst.yaml) (for Step 5; file is gitignored). If `API_ACCESS_TOKEN` is empty in `.env`, the script generates a random value and logs a warning; set a stable token in `.env` for production (for example with `openssl rand -base64 32`).

You can render only the MCP or only the manifests: `python3 scripts/render_from_env.py mcp` or `python3 scripts/render_from_env.py manifests`.

The committed [`.cursor/mcp.json`](.cursor/mcp.json) defaults to `us-east-1` so a clone works before you configure `.env`; re-run the render step after you change `AWS_REGION` to keep it in sync.

## Optional: Local validation (Cursor)

Validate the managed service works locally before deploying the bridge. After configuring `.env` and running `python3 scripts/render_from_env.py mcp` so `.cursor/mcp.json` matches your region, open this repo in Cursor, then in agent chat ask:

- "What EKS MCP tools are available?"
- "List all EKS clusters in <your-aws-region>"

If you use a different AWS profile, configure it for the CLI; the MCP process uses the same default credential chain.

## Step 1: Build and Push Docker Image

With `.env` loaded in your shell (see [Configure AWS region and account](#configure-aws-region-and-account-set-once-per-shell)):

```bash
cd docker/

# Build for amd64 (EKS nodes; image uses AWS_REGION at runtime, not at build)
docker buildx build --platform linux/amd64 -t eks-mcp-bridge:latest .

# Local test (mount AWS creds; pass the same region as in .env)
docker run -p 8888:8888 \
  -e API_ACCESS_TOKEN="test-token" \
  -e AWS_REGION="${AWS_REGION}" \
  -v ~/.aws:/root/.aws:ro \
  eks-mcp-bridge:latest
```

**Verify (in another terminal):**

```bash
curl -s -m 15 -X POST http://localhost:8888/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

**Push to ECR** (from the `docker/` directory, with `.env` still loaded in the shell):

```bash
aws ecr create-repository --repository-name eks-mcp-bridge --region "${AWS_REGION}" 2>/dev/null
aws ecr get-login-password --region "${AWS_REGION}" | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

docker buildx build --platform linux/amd64 \
  -t "${EKS_MCP_BRIDGE_IMAGE}" \
  --push .
```

If you have not used `load-env.sh`, set `EKS_MCP_BRIDGE_IMAGE` yourself or use the long form: `${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/eks-mcp-bridge:latest`.

## Step 2: Create IAM policies (read-only and privileged add-on)

The bridge IRSA identity (created in Step 3) should use the AWS managed policy **`AmazonEKSMCPReadOnlyAccess`**, which grants `eks-mcp:InvokeMcp` and `eks-mcp:CallReadOnlyTool` for the managed EKS MCP server.

Privileged MCP tools (`manage_k8s_resource`, `apply_yaml`) additionally require **`eks-mcp:CallPrivilegedTool`** and **`eks:AccessKubernetesApi`**. For that, create the customer-managed policy **`EksMcpPrivilegedK8sWritesAddon`** from this repo. Step 3 attaches both the managed read-only policy and this add-on to the new IRSA role via `eksctl --attach-policy-arn`; this step only **creates** the add-on policy document in IAM (it does not attach policies to any role).

From the **repository root**, with `.env` loaded:

```bash
cd /path/to/aws-eks-mcp-setup

aws iam create-policy \
  --policy-name EksMcpPrivilegedK8sWritesAddon \
  --policy-document file://iam/eks-mcp-privileged-addon-policy.json \
  --description "EKS MCP privileged tools + K8s API for bridge IRSA"
```

For **`manage_eks_stacks`**, **`add_inline_policy`**, or full cluster provisioning, use the broader example in [AWS Getting Started Step 2](https://docs.aws.amazon.com/eks/latest/userguide/eks-mcp-getting-started.html) instead of the trimmed policy above.

**Verify** (with `.env` loaded; these checks match what Step 2 and Step 3 rely on: the managed read-only policy, and the customer-managed add-on you just created if you use privileged K8s tools):

```bash
# 1) Managed policy used in Step 3 for read-only MCP tools (no create step; confirm it is visible in IAM)
aws iam get-policy \
  --policy-arn arn:aws:iam::aws:policy/AmazonEKSMCPReadOnlyAccess \
  --query 'Policy.{Arn:Arn,DefaultVersionId:DefaultVersionId}' \
  --output table

# 2) Customer policy created in the command block above (required before attaching it in Step 3)
aws iam get-policy \
  --policy-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:policy/EksMcpPrivilegedK8sWritesAddon" \
  --output table
```

The second command should return the policy’s ARN, default version, and update time. If you are **read-only only** and did **not** run `aws iam create-policy`, expect that command to fail; skip it and do not add the second `--attach-policy-arn` in Step 3.

**Optional (confirm the add-on’s JSON has the expected privileges):**

```bash
ADDON_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:policy/EksMcpPrivilegedK8sWritesAddon"
VER=$(aws iam get-policy --policy-arn "$ADDON_ARN" --query Policy.DefaultVersionId --output text)
aws iam get-policy-version --policy-arn "$ADDON_ARN" --version-id "$VER" \
  --query 'PolicyVersion.Document' \
  --output json
```

Compare the output to [iam/eks-mcp-privileged-addon-policy.json](iam/eks-mcp-privileged-addon-policy.json) (or confirm `Statement` includes `eks-mcp:CallPrivilegedTool` and `eks:AccessKubernetesApi` for your target cluster path).

## Step 3: Create IRSA Service Account

The bridge pod needs AWS credentials to sign SigV4 requests to the managed endpoint. Create an IRSA-enabled service account. `eksctl` attaches **`AmazonEKSMCPReadOnlyAccess`** and, when listed, **`EksMcpPrivilegedK8sWritesAddon`** to the new role—ensure the add-on policy exists (Step 2) before running with both ARNs.

With `.env` loaded and `EKS_CLUSTER_NAME` set in `.env` (or export it in the shell for this command):

```bash
kubectl create namespace eks-mcp-bridge

eksctl create iamserviceaccount \
  --name eks-mcp-bridge-sa \
  --namespace eks-mcp-bridge \
  --cluster "${EKS_CLUSTER_NAME}" \
  --region "${AWS_REGION}" \
  --attach-policy-arn arn:aws:iam::aws:policy/AmazonEKSMCPReadOnlyAccess \
  --attach-policy-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:policy/EksMcpPrivilegedK8sWritesAddon" \
  --approve
```

Omit the second `--attach-policy-arn` if you want read-only MCP tools only (no privileged K8s writes).

Verify the annotation:

```bash
kubectl get sa eks-mcp-bridge-sa -n eks-mcp-bridge -o yaml | grep eks.amazonaws.com/role-arn
```

## Step 4: Map IRSA Role in aws-auth and Apply RBAC

The managed EKS MCP server uses the caller's IAM identity to make Kubernetes API calls on the target cluster. The IRSA role from Step 3 must be mapped to a Kubernetes identity with read permissions, otherwise K8s API calls (e.g. `get_pod_logs`) will fail with 401 Unauthorized.

### 4a. Add the IRSA role to the aws-auth ConfigMap

Get the IRSA role ARN:

```bash
kubectl get sa eks-mcp-bridge-sa -n eks-mcp-bridge -o yaml | grep eks.amazonaws.com/role-arn
```

Edit the `aws-auth` ConfigMap to add the role:

```bash
kubectl edit configmap aws-auth -n kube-system
```

Add this entry under `mapRoles` (replace the `rolearn` with your actual value):

```yaml
    - rolearn: <irsa-role-arn>
      username: eks-mcp-bridge-sa
      groups:
      - eks-mcp-readers
```

### 4b. Apply the RBAC ClusterRole and ClusterRoleBinding

```bash
kubectl apply -f kubernetes/rbac.yaml
```

This creates a `eks-mcp-reader` ClusterRole with read-only access to pods, logs, events, deployments, services, nodes, and more, and binds it to the `eks-mcp-readers` group.

### 4c. Writer RBAC (patch, rollout restart, apply YAML)

For **`manage_k8s_resource`** and **`apply_yaml`**, apply the writer ClusterRole (same `eks-mcp-readers` group; permissions merge with the reader binding):

```bash
kubectl apply -f kubernetes/rbac-writer.yaml
```

Narrow this to a namespace by replacing the ClusterRole/Binding with a `Role` and `RoleBinding` if the agent must not mutate cluster-wide objects.

## Step 5: Deploy to EKS

### 5a. Cluster API endpoint (required for write tools per AWS)

The [EKS MCP tools reference](https://docs.aws.amazon.com/eks/latest/userguide/eks-mcp-tools.html) documents that **write** Kubernetes operations from the managed server expect a **public** cluster endpoint (`endpointPublicAccess=true`). Confirm (with `EKS_CLUSTER_NAME` in your environment from `.env`):

```bash
aws eks describe-cluster --name "${EKS_CLUSTER_NAME}" --region "${AWS_REGION}" \
  --query 'cluster.resourcesVpcConfig' \
  --output json
```

Confirm `endpointPublicAccess` is `true` (AWS documents this as a requirement for managed write tools).

If the cluster is private-only, plan for access that satisfies AWS’s current constraints for full-access tools, or restrict the agent to read-only tools.

### 5b. Apply manifests and verify

Set `API_ACCESS_TOKEN` in `.env` to a strong value (`openssl rand -base64 32`) unless you accept the one generated by the render script. The image and region come from the same `.env` as above.

From the **repository root**, with `.env` loaded:

```bash
python3 scripts/render_from_env.py manifests
kubectl apply -f kubernetes/manifests.rendered.yaml
kubectl get pods -n eks-mcp-bridge
kubectl get svc -n eks-mcp-bridge
```

`kubernetes/manifests.envsubst.yaml` is the source template; `kubernetes/manifests.rendered.yaml` is the generated file and is listed in `.gitignore` so secrets are not committed.

**IAM note:** You do not need a pod restart in Step 5b for normal policy work. If you add or change IAM **policies** on the **same** role that IRSA already uses, AWS authorizes each API call against the current policy; new pods are not required. Schedule new pods (for example `kubectl rollout restart deployment eks-mcp-bridge -n eks-mcp-bridge`) mainly when the **ServiceAccount**’s `eks.amazonaws.com/role-arn` changes to a **different** role, or as a last resort if you still see `403` after a policy change once IAM has finished propagating.

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

## Step 6: Connect to Elastic Agent Builder

### 6a. Create MCP Connector in Kibana

1. Go to **Stack Management > Connectors > Create connector**
2. Select **MCP** connector type
3. Configure:
   - **Name:** `AWS EKS MCP (Managed)`
   - **Server URL:** `http://<LoadBalancer-hostname>:8888/mcp`
   - **HTTP Headers** (secret type): Key=`Authorization`, Value=`Bearer <API_ACCESS_TOKEN from Step 5>` (the same value you set in `.env` or the rendered manifest secret)
4. Click **Test** to verify the connection

### 6b. Bulk Import MCP Tools

1. Go to **Agent Builder > Tools**
2. Click **Manage MCP > Bulk import MCP tools**
3. Select the `AWS EKS MCP (Managed)` connector
4. Select tools to import. For **read-only** troubleshooting, import the read tools you need. For **write** operations (patch deployments, rollout restart, apply manifests), include at least:
   - **`manage_k8s_resource`**
   - **`apply_yaml`**
5. Set namespace prefix: `eks`

### 6c. Test End-to-End

Create or edit an agent, add the imported EKS tools, then test in chat:

- "List all EKS clusters in <your-aws-region>"
- "Show me pods in the default namespace"
- "What events are happening in my cluster?"
- "Search the EKS troubleshooting guide for pod networking issues"

With write tools enabled (IAM + RBAC + public endpoint per AWS docs), you can also test:

- "Perform a rollout restart of deployment `<name>` in namespace `<ns>` on cluster `<cluster>`"

## Available Tools (20)

| Category | Tools |
|---|---|
| Cluster Management | `list_eks_resources`, `describe_eks_resource`, `manage_eks_stacks`, `get_eks_insights`, `get_eks_vpc_config` |
| Kubernetes Resources | `list_k8s_resources`, `read_k8s_resource`, `manage_k8s_resource`, `apply_yaml`, `generate_app_manifest`, `list_api_versions` |
| Troubleshooting | `get_pod_logs`, `get_k8s_events`, `get_cloudwatch_logs`, `get_cloudwatch_metrics`, `get_eks_metrics_guidance` |
| Documentation | `search_eks_documentation`, `search_eks_troubleshooting_guide` |
| IAM / Security | `get_policies_for_role`, `add_inline_policy` |

## Optional: test write path from your laptop

With `kubectl` pointed at the cluster and the bridge LoadBalancer up:

```bash
python3 scripts/test_mcp_write.py
```

This calls `manage_k8s_resource` with a rollout-style patch on `Deployment/cart` in `default` (edit the script to change cluster name or workload). Expect `Successfully patched Deployment` and `exit=0`.

## Testing Checklist

- [ ] `.env` present with `AWS_REGION` and `AWS_ACCOUNT_ID` (and `EKS_CLUSTER_NAME` for cluster commands)
- [ ] `python3 scripts/render_from_env.py` run when Cursor or manifest templates change
- [ ] Docker image builds and runs -- `docker run` + `curl` test passes (Step 1)
- [ ] ECR image pushed -- `docker buildx ... --push` succeeds (Step 1)
- [ ] IAM add-on policy created -- `aws iam get-policy` for `EksMcpPrivilegedK8sWritesAddon` succeeds when using write tools; managed `AmazonEKSMCPReadOnlyAccess` visible via `aws iam get-policy` (Step 2)
- [ ] Managed EKS MCP server works locally (optional) -- Cursor agent lists clusters and tools
- [ ] IRSA service account created -- annotation shows role ARN; role has `AmazonEKSMCPReadOnlyAccess` (+ add-on if used) (Step 3)
- [ ] IRSA role mapped in aws-auth -- `kubectl get cm aws-auth -n kube-system` shows the role (Step 4)
- [ ] RBAC applied -- `kubectl get clusterrole eks-mcp-reader` exists (Step 4)
- [ ] Writer RBAC applied (if using write tools) -- `kubectl get clusterrole eks-mcp-writer` exists (Step 4)
- [ ] Cluster endpoint -- `endpointPublicAccess=true` for write tools (per AWS docs) (Step 5)
- [ ] K8s deployment healthy -- pod running, port-forward test passes (Step 5)
- [ ] LoadBalancer reachable -- `curl` to external endpoint works (Step 5)
- [ ] Elastic MCP connector connects -- "Test connection" in Kibana succeeds (Step 6)
- [ ] Tools are discovered -- `listTools` returns 20 EKS MCP tools (Step 6)
- [ ] Write tools imported -- `manage_k8s_resource` and `apply_yaml` enabled on the agent when needed (Step 6)
- [ ] Agent chat works -- Agent Builder can query EKS cluster (Step 6)

## Security Considerations

- **`.env` and generated manifests:** Do not commit `.env` or `kubernetes/manifests.rendered.yaml`. Keep `API_ACCESS_TOKEN` only in `.env` and in the cluster Secret.
- **Bearer token:** Generate with `openssl rand -base64 32`. Store in K8s Secret. Rotate periodically.
- **Network:** Restrict LoadBalancer security group to Elastic Cloud IP ranges only.
- **TLS:** For production, add an Ingress with TLS termination (ACM cert + ALB Ingress Controller).
- **IRSA:** Prefer `AmazonEKSMCPReadOnlyAccess` plus [iam/eks-mcp-privileged-addon-policy.json](iam/eks-mcp-privileged-addon-policy.json) only if you need privileged MCP tools; Step 2 creates the add-on policy, Step 3 attaches policies when creating the service account.
- **K8s RBAC:** [kubernetes/rbac.yaml](kubernetes/rbac.yaml) is read-only; [kubernetes/rbac-writer.yaml](kubernetes/rbac-writer.yaml) adds create/update/patch/delete for common workload types. Narrow or split groups if read and write identities should differ.
- **Read-only mode:** To block privileged tools, add `--read-only` to `mcp-proxy-for-aws` in [docker/entrypoint.sh](docker/entrypoint.sh) **and** omit the privileged add-on at IRSA creation **and** do not apply writer RBAC.
- **CloudTrail:** The managed service automatically logs all tool calls for auditing.
