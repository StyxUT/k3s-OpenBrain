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

## Obsidian Import

This repo includes a self-hosted Obsidian importer at `tools/import-obsidian-selfhosted.py`.

It imports notes directly into the shared `thoughts` table, generates embeddings through Ollama,
and stores vault-specific metadata alongside each imported thought.

### Multi-Vault Workflow

Recommended approach:

1. Import each vault separately.
2. Always set `--vault-name`.
3. Use dry run first.
4. Start with a small `--limit` batch.
5. Run the full import after verifying results.

The importer keeps a separate sync log per vault in `tools/obsidian-sync-<vault>.json`, so reruns
skip unchanged notes independently for each vault.

### Current Imported Vaults

- `<vault-name-a>` -> `<thought-count-a>` thoughts
- `<vault-name-b>` -> `<thought-count-b>` thoughts
- `<vault-name-c>` -> `<thought-count-c>` thoughts

### Example Commands

Dry run:

```bash
python tools/import-obsidian-selfhosted.py \
  "/path/to/Obsidian/<vault-name>" \
  --vault-name <vault-name> \
  --dry-run
```

Live import:

```bash
OPENBRAIN_DB_PASSWORD='your-postgres-password' \
python tools/import-obsidian-selfhosted.py \
  "/path/to/Obsidian/<vault-name>" \
  --vault-name <vault-name> \
  --limit 25 \
  --verbose
```

If you explicitly want to include notes that would normally be flagged by the secret scanner:

```bash
OPENBRAIN_DB_PASSWORD='your-postgres-password' \
python tools/import-obsidian-selfhosted.py \
  "/path/to/Obsidian/<vault-name>" \
  --vault-name <vault-name> \
  --no-secret-scan \
  --verbose
```

Warning: `--no-secret-scan` imports note contents verbatim. Any credentials, tokens, connection
strings, or other sensitive-looking values present in your notes will be stored in OpenBrain and
can be surfaced by semantic search later.

### Notes

- Imported thoughts are tagged with `metadata.source = obsidian`.
- Imported thoughts are also tagged with `metadata.vault` so you can distinguish vaults later.
- Embeddings use `qwen3-embedding` and are stored as `vector(4096)`.
