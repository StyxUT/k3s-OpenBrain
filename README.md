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

## Runtime Layout

The OpenBrain Deno server source is stored in `app/` and injected into the pod by a generated ConfigMap.

This avoids the need to build and import a custom image onto the k3s nodes.

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
