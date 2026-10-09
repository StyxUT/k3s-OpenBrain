#!/usr/bin/env bash
set -euo pipefail

# Run on arch.home. Uses a cached, digest-pinned image; never pulls.
IMAGE='ollama/ollama@sha256:e87f7608374a9f4a3f103032b895910bf501ea248a91b12c83f34f41673176f6'
if docker inspect ollama-amd >/dev/null 2>&1; then
  role="$(docker inspect ollama-amd --format '{{index .Config.Labels "com.openbrain.role"}}')"
  if [[ "$role" != 'embedding-gpu' ]]; then
    echo 'An unmanaged ollama-amd container exists; inspect it before changing anything.' >&2
    exit 1
  fi
  docker start ollama-amd
  exit 0
fi

docker image inspect "$IMAGE" >/dev/null
docker run -d --pull=never \
  --name ollama-amd --label com.openbrain.role=embedding-gpu \
  --restart unless-stopped \
  --device /dev/kfd --device /dev/dri \
  --group-add 985 --group-add 989 \
  -p 192.168.0.13:11435:11434 \
  -v /var/.ollama/models:/models:ro \
  -v openbrain-ollama-gpu-config:/root/.ollama \
  -e OLLAMA_HOST=0.0.0.0:11434 \
  -e OLLAMA_MODELS=/models \
  -e OLLAMA_CONTEXT_LENGTH=32768 \
  -e OLLAMA_FLASH_ATTENTION=1 \
  -e OLLAMA_NUM_PARALLEL=1 \
  -e OLLAMA_MAX_LOADED_MODELS=1 \
  -e OLLAMA_KEEP_ALIVE=-1 \
  -e OLLAMA_NO_CLOUD=1 \
  -e OLLAMA_NOPRUNE=1 \
  "$IMAGE"
