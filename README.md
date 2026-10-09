# OpenBrain for k3s

Kubernetes manifests for running OpenBrain in the existing k3s cluster.

## What This Deploys

- OpenBrain MCP server as a `Deployment`
- OpenBrain Service on TCP 8000 with private backend NodePort 32080 for HAProxy
- PostgreSQL bootstrap `Job` that creates the `openbrain` database and schema
- PostgreSQL on the Synology iSCSI LUN, with a `Retain` PV and internal `ClusterIP` service
- Egress policies that deny general internet access to OpenBrain and PostgreSQL while allowing DNS, PostgreSQL, and the configured local Ollama endpoint

## Model Configuration

- Chat API: `http://arch.home:11434/v1`
- Chat model: `smollm2:1.7b`
- Primary embedding API: `http://arch.home:11435/v1` (RX 6800 XT, ROCm)
- CPU fallback API: `http://arch.home:11434/api` (native Ollama, `num_gpu: 0`)
- Embedding model: `qwen3-embedding`
- Embedding dimension: `4096`

OpenBrain sends both embedding and metadata-extraction calls to the Ollama instance on the
desktop at `arch.home`. Embeddings prefer the dedicated GPU instance on port
11435; metadata extraction stays on the existing CPU service on port 11434.

### GPU primary with CPU fallback — 2026-10-09

`app/embedding.ts` supplies one shared 15-second deadline. The GPU attempt gets
at most 5 seconds, reserving the remaining time for CPU. Connection errors,
timeouts, HTTP 408/429, and server errors trigger fallback; invalid requests,
authentication/model configuration errors, malformed JSON, and invalid vectors
fail without retrying. Both responses must contain 4096 finite numbers.

After two consecutive primary availability failures, the circuit bypasses GPU
for 60 seconds. Once the cooldown expires, one recovery probe is allowed while
other requests use CPU; a successful probe restores normal routing. Logs record
backend, timing, and circuit events, never thought text or credentials.
Only embedding generation is retried: `capture_thought` still inserts once,
after an embedding succeeds. There is no automatic full-capture retry.

Both Ollama instances read the same model store. At verification the model
manifest digest was `64b933495768fbd3b87c20583d379728a07471e0c66733a9df87cd1901b3c44b`.
GPU version is 0.24.0 and CPU version 0.40.1; identical model artifacts do not
guarantee bit-identical vectors across runtime versions/hardware. The initial
cross-backend diagnostic cosine similarity was 0.9977. Preserve the model and
dimensions when maintaining either backend; do not independently pull a
different model onto one service.

Verified GPU offload: 37/37 layers, approximately 12.5 GiB model/runtime VRAM.
Cold request: 3.38 seconds; warmed GPU: 0.14 seconds; warmed CPU: 0.32 seconds.
The GPU model stays resident (`OLLAMA_KEEP_ALIVE=-1`), leaving less VRAM for
desktop or other GPU workloads. CPU fallback is explicitly enforced per request
with native Ollama `options.num_gpu=0`; it does not depend on GPU autodetection.
Large inputs can exceed the CPU's remaining deadline; they fail explicitly
rather than extending the overall wait. A 4,700-character diagnostic completed
on GPU in 0.91 seconds but did not finish on CPU within a 12-second test budget.

Start the dedicated GPU service on **arch.home**:

```bash
bash scripts/start-ollama-gpu.sh
```

The script uses the existing digest-pinned cached ROCm image with `--pull=never`,
read-only `/var/.ollama/models`, and a separate configuration volume. Cloud
features and model pruning are disabled. It refuses to replace an unmanaged
container. Docker is enabled and the container uses `unless-stopped` for reboot
recovery. `compose.ollama-gpu.yaml` describes the same service for future use
with Compose V2 (the plugin was not installed at verification).

Only worker-01 `192.168.50.221/32` may reach `arch.home` `192.168.0.13/32`
on the required model ports under the pfSense Services exceptions:
`SERV-OPENBRAIN-OLLAMA-01` (TCP/11434) and
`SERV-OPENBRAIN-OLLAMA-GPU-01` (TCP/11435). These are node-scoped, not per-pod
isolation; the OpenBrain NetworkPolicy also limits its model destination.
No general internet/internal exception was added. Both services share the host,
so CPU fallback does not protect against an `arch.home` outage.

Metadata extraction is intentionally handled by a small chat model. The OpenBrain server bounds
metadata extraction with a short timeout and normalizes the returned JSON, so captures can still
succeed if metadata extraction is slow or malformed.

### Verified model path and timeout — 2026-10-09

Historical CPU-only diagnostic below is superseded by the GPU/fallback
configuration above. The database-only `/healthz` remains unchanged.

The active runtime is the **host Ollama service on `arch.home:11434`**, not the
AMD container described below. A live `qwen3-embedding` request produced a
4096-dimensional vector; Ollama reported `size_vram: 0` (CPU execution).
The AMD container/port `11435` was unavailable during this check. Treat the
AMD section below as a setup reference, not evidence of current GPU use.

pfSense permits only worker-01 `192.168.50.221/32` to `arch.home`
`192.168.0.13/32` on TCP/11434 via `SERV-OPENBRAIN-OLLAMA-01`. This is node-scoped,
not per-pod isolation; the existing OpenBrain NetworkPolicy also limits its
model destination. General internet and other internal access remain blocked.

Embedding requests, including response-body consumption, are bounded to
**15 seconds** and return an explicit timeout error. Metadata extraction retains
its 10-second timeout and fallback. A failed embedding prevents insertion.
The database-only `/healthz` check does not establish model-service health.

The patched `app/index.ts` is supplied by the `openbrain-runtime-source`
ConfigMap, together with the local `app/embedding.ts` module, mounted read-only
over their `/app` paths in the existing offline image. No external dependencies
were added; `--cached-only` remains enabled. After
future source ConfigMap changes, explicitly roll out the Deployment because
a `subPath` mount does not refresh in place. Rebuild the bundled image through
the approved offline workflow when removing this source override.

Regression tests (run with Deno 2.3.3, without external dependencies):

```bash
deno test --no-config --allow-read tests/embedding_test.ts
```

Tests now cover GPU success, actual timeout cancellation, CPU forcing, shared
deadline, retryable/non-retryable failures, output validation, and circuit
opening/recovery. Tests do not save thoughts.

### Acceptance and rollback

All 19 regression tests and the full Deno server type-check passed. Live public
MCP searches passed on GPU (0.28 seconds), with the dedicated GPU container
temporarily paused (5.36 seconds including timeout and CPU fallback), and after
GPU recovery (0.20 seconds). Backend logs confirmed primary/fallback/primary
routing. CPU `size_vram` remained zero; GPU reported 13,403,149,344 bytes and
37/37 layers offloaded. The container was unpaused in a guarded cleanup and
left running. No diagnostic thoughts were saved.

Protected pre-change source and live Deployment/ConfigMap/NetworkPolicy
checkpoints are on `arch.home` under
`/tmp/opencode/openbrain-gpu-fallback-20261009/` (mode-0600 checkpoint files).
For rollback, restore only the changed source/configuration from those files,
apply the previous source ConfigMap and model NetworkPolicy, then restore the
previous Deployment. Stop only the dedicated managed `ollama-amd` container if
reverting GPU use, and remove only pfSense rule tracker `1791589045` through
the supported API. Preserve the existing CPU rule and shared model/configuration
volumes. Reconcile newer edits before using a checkpoint; don't overwrite
unrelated work or restore an entire old firewall snapshot.

## AMD Ollama Runtime (historical setup reference)

The old example below is historical. Use `scripts/start-ollama-gpu.sh` for the
current pinned, read-only-model-store configuration.

The AMD Ollama instance runs outside k3s as a Docker container on the desktop that hosts the Radeon
RX 6800 XT. This keeps ROCm isolated from the host Ollama packages while preserving the existing
OpenBrain dependency on the desktop model host.

Current container shape:

```bash
docker run -d \
  --name ollama-amd \
  --restart unless-stopped \
  --device /dev/kfd \
  --device /dev/dri \
  --group-add 985 \
  --group-add 989 \
  -e OLLAMA_HOST=0.0.0.0:11434 \
  -p 11435:11434 \
  -v /var/.ollama:/root/.ollama \
  ollama/ollama:rocm
```

Notes:

- `985` is the host `video` group and `989` is the host `render` group on the current desktop.
- `--restart unless-stopped` restarts the container after boot unless it was manually stopped.
- Docker must be enabled on the desktop: `sudo systemctl enable docker`.
- The legacy host `ollama-amd.service` should remain removed/disabled so it does not occupy port `11435`.

Verify the AMD Ollama runtime:

```bash
docker ps --filter name=ollama-amd
curl http://localhost:11435/api/tags
docker logs ollama-amd --tail 100
rocm-smi
```

Successful ROCm startup logs include `library=ROCm`, `compute=gfx1030`, and
`AMD Radeon RX 6800 XT`. Successful generation logs should show GPU offload such as
`offloaded 33/33 layers to GPU`.

## Required Secret

Create the MCP access key file before applying manifests:

```bash
printf '%s' 'your-random-mcp-key' > ./secrets/mcp_access_key.txt
```

The `secrets/` directory is ignored by git and is consumed by `kustomization.yaml`.

## Runtime Layout

The Deno dependency cache is bundled into `openbrain-mcp:20261004-r2`; runtime
dependency downloads are disabled with `--cached-only`. Patched `app/index.ts`
and the local `app/embedding.ts` module are mounted from `openbrain-runtime-source`.
The image is imported into worker-01 containerd, and the workload is pinned there.
`Dockerfile` also copies both modules for a future approved bundled-image rebuild.

## Apply Manifests

Build/promote images before applying if the node-local images are missing. Acquire upstream images only on the designated egress host (worker-02); do not pull from worker-01 or worker-03. Ensure worker-01 and worker-03 have `open-iscsi` installed and `iscsid.service` enabled. The Synology CSI node plugin must be configured with `--chroot-dir=/host` so it runs the host `iscsiadm`.

Then apply the application resources from this directory:

```bash
kubectl apply -k .
```

## Verify

```bash
kubectl get pods,job,svc
kubectl logs job/openbrain-db-init
kubectl logs deploy/openbrain
```

The service keeps its ClusterIP port 8000 for in-cluster clients. HAProxy reaches the backend through worker-01 NodePort 32080; keep that port limited to approved HAProxy sources.

The unauthenticated `GET /healthz` endpoint returns HTTP 200 only when PostgreSQL is reachable; use it for HAProxy and Kubernetes readiness checks. The MCP endpoint itself remains protected by `x-brain-key`.

To test the MCP endpoint after the pod is ready:

```bash
kubectl port-forward svc/openbrain 8000:8000
# In another terminal:
curl -X POST http://127.0.0.1:8000 \
  -H "x-brain-key: YOUR_MCP_KEY" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```

## Obsidian Import

This repo includes a self-hosted Obsidian importer at `tools/import-obsidian-selfhosted.py`.

It imports notes through the OpenBrain API and stores vault-specific metadata alongside each imported thought.
Embeddings and metadata extraction are handled by the OpenBrain server.

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
OPENBRAIN_MCP_KEY='your-mcp-key' \
python tools/import-obsidian-selfhosted.py \
  "/path/to/Obsidian/<vault-name>" \
  --vault-name <vault-name> \
  --limit 25 \
  --verbose
```

If you explicitly want to include notes that would normally be flagged by the secret scanner:

```bash
OPENBRAIN_MCP_KEY='your-mcp-key' \
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
- The importer is API-only: it requires `OPENBRAIN_API_KEY` or `OPENBRAIN_MCP_KEY`.
- Embeddings use `qwen3-embedding` and are handled by the OpenBrain server.
