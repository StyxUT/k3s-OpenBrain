#!/bin/zsh
set -u

PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
ROOT=/Users/openclaw/.openclaw/workspace-coder/k3s-OpenBrain
IMPORTER="$ROOT/tools/import-obsidian-selfhosted.py"
ENV_FILE="$ROOT/tools/obsidian-sync.env"
SERVICE_ENV_FILE="/Users/openclaw/.openclaw/service-env/ai.openclaw.gateway.env"
LOG_DIR="$ROOT/logs"
LOG_FILE="$LOG_DIR/obsidian-incremental-sync.log"
LOCK_DIR=/tmp/openbrain-obsidian-sync.lock
OLLAMA_BASE="${EMBEDDING_API_BASE:-http://192.168.0.13:11434/v1}"
KUBE_API_SERVER="${KUBE_API_SERVER:-https://k3s-nodes.home:6443}"

mkdir -p "$LOG_DIR"

if [[ -f "$SERVICE_ENV_FILE" ]]; then
  set -a
  source "$SERVICE_ENV_FILE"
  set +a
fi

if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "sync already running; skipping this interval"
  exit 0
fi
trap 'rmdir "$LOCK_DIR" >/dev/null 2>&1 || true' EXIT

if ! curl -fsS --max-time 10 "$OLLAMA_BASE/models" >/dev/null 2>&1; then
  log "ollama unavailable at $OLLAMA_BASE; skipping and retrying next run"
  exit 0
fi

if [[ -z "${OPENBRAIN_DB_PASSWORD:-}" && -z "${OPENBRAIN_API_KEY:-}" && -z "${OPENBRAIN_MCP_KEY:-}" ]]; then
  if ! command -v kubectl >/dev/null 2>&1; then
    log "no DB password, API key, or kubectl access available; skipping and retrying next run"
    exit 0
  fi

  if ! kubectl --server="$KUBE_API_SERVER" get secret postgres-password -o jsonpath='{.data.POSTGRES_PASSWORD}' >/dev/null 2>&1; then
    log "no DB password or API key available, and kubectl cannot read postgres-password via $KUBE_API_SERVER; skipping and retrying next run"
    exit 0
  fi
fi

cd "$ROOT" || exit 1

vault_names=(BB EricBnTraciB StyxUT)
vault_paths=(
  "/Users/Obsidian/BB"
  "/Users/Obsidian/EricBnTraciB"
  "/Users/Obsidian/StyxUT/StyxUT"
)

exit_code=0
for i in {1..$#vault_names}; do
  vault_name="$vault_names[$i]"
  vault_path="$vault_paths[$i]"

  if [[ ! -d "$vault_path" ]]; then
    log "vault missing for $vault_name at $vault_path; skipping"
    continue
  fi

  log "starting incremental import for $vault_name"
  if ! python3 "$IMPORTER" "$vault_path" --vault-name "$vault_name" >> "$LOG_FILE" 2>&1; then
    log "import failed for $vault_name; will retry next run"
    exit_code=1
  else
    log "completed incremental import for $vault_name"
  fi
done

exit $exit_code
