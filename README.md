# OpenBrain for k3s

Kubernetes manifests for running OpenBrain in the existing k3s cluster.

## What This Deploys

- OpenBrain MCP server as a `Deployment`
- OpenBrain service as a `NodePort`
- PostgreSQL bootstrap `Job` that creates the `openbrain` database and schema
- Shared PostgreSQL usage through the existing `postgres-service`

## Model Configuration

- Chat API: `http://192.168.0.13:11434/v1`
- Chat model: `qwen3.5:27b`
- Embedding API: `http://192.168.0.13:11434/v1`
- Embedding model: `qwen3-embedding`
- Embedding dimension: `4096`

## Required Secret

Create the MCP access key file before applying manifests:

```bash
printf '%s' 'your-random-mcp-key' > ./secrets/mcp_access_key.txt
```

The `secrets/` directory is ignored by git and is consumed by `kustomization.yaml`.

## Build The MCP Image

The manifests expect the image tag `openbrain-mcp-server:latest`.

Build from the upstream OpenBrain integration source:

```bash
docker build -t openbrain-mcp-server:latest ./.upstream-OB1/integrations/kubernetes-deployment
```

## Import The Image Into k3s

This workstation does not run k3s locally, so import the image on a k3s node:

```bash
docker save -o openbrain-mcp-server.tar openbrain-mcp-server:latest
scp openbrain-mcp-server.tar <user>@192.168.0.211:/tmp/
ssh <user>@192.168.0.211 'sudo k3s ctr images import /tmp/openbrain-mcp-server.tar'
```

## Apply Manifests

Apply PostgreSQL first so the cluster uses the pgvector-enabled image:

```bash
kubectl apply -k ../k3s-Postgres
kubectl apply -k .
```

## Verify

```bash
kubectl get pods,job,svc
kubectl logs job/openbrain-db-init
kubectl logs deploy/openbrain
```

To test the MCP endpoint after the pod is ready:

```bash
curl -X POST http://192.168.0.211:8000 \
  -H "x-brain-key: YOUR_MCP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```
