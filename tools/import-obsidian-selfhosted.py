#!/usr/bin/env python3
"""
Import an Obsidian vault into self-hosted OpenBrain.

Supports dry-run previews and live import against the self-hosted PostgreSQL
schema used by the k3s OpenBrain deployment.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import subprocess
from typing import Optional
from urllib import error, request


ALWAYS_SKIP = {".obsidian", ".trash", ".git", "node_modules"}
DEFAULT_MIN_WORDS = 20
WHOLE_NOTE_THRESHOLD = 500
EMBEDDING_MODEL = "qwen3-embedding"
MAX_RETRIES = 3
RETRY_BACKOFF = 2
KUBE_API_SERVER = os.environ.get("KUBE_API_SERVER", "https://k3s-nodes.home:6443")
OPENBRAIN_API_BASE = os.environ.get("OPENBRAIN_API_BASE", "http://k3s-nodes.home:8000")
OPENBRAIN_API_KEY = os.environ.get("OPENBRAIN_API_KEY") or os.environ.get("OPENBRAIN_MCP_KEY") or ""
OPENBRAIN_API_TIMEOUT = int(os.environ.get("OPENBRAIN_API_TIMEOUT", "600"))

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")
INLINE_TAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_/-]+)")

SECRET_PATTERNS = [
    ("API key", re.compile(r"sk-(?:or-v1-|proj-|live-)?[a-zA-Z0-9]{20,}")),
    ("JWT token", re.compile(r"eyJ[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"gh[ps]_[a-zA-Z0-9]{36,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Private key block", re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----")),
    (
        "Generic secret assignment",
        re.compile(
            r"(?:password|secret|token|api_key|apikey|api_secret|access_token|auth_token)"
            r"\s*[=:]\s*[\"']?[a-zA-Z0-9_\-/.]{16,}",
            re.IGNORECASE,
        ),
    ),
    (
        "Connection string with credentials",
        re.compile(r"(?:postgres|mysql|mongodb|redis)://[^:]+:[^@]+@", re.IGNORECASE),
    ),
]


def run_kubectl(args: list[str], *, input_text: Optional[str] = None, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    command = ["kubectl"]
    if KUBE_API_SERVER:
        command.append(f"--server={KUBE_API_SERVER}")
    command.extend(args)
    return subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
    )


def resolve_db_password(explicit_password: str) -> str:
    if explicit_password:
        return explicit_password

    result = run_kubectl(
        [
            "get",
            "secret",
            "postgres-password",
            "-o",
            "jsonpath={.data.POSTGRES_PASSWORD}",
        ]
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""

    try:
        return subprocess.run(
            ["python3", "-c", "import base64,sys;print(base64.b64decode(sys.stdin.read()).decode(), end='')"],
            input=result.stdout.strip(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return ""


def parse_mcp_sse_payload(raw: str) -> dict:
    payload = None
    for line in raw.splitlines():
        if line.startswith("data: "):
            payload = line[6:]
    if not payload:
        raise RuntimeError(f"unexpected MCP response: {raw[:500]}")
    return json.loads(payload)


def read_first_sse_event(resp) -> str:
    lines = []
    while True:
        line = resp.readline().decode()
        if line in ("", "\n", "\r\n"):
            if lines:
                return "".join(lines)
            continue
        lines.append(line)


def call_openbrain_tool(tool_name: str, arguments: dict) -> dict:
    if not OPENBRAIN_API_KEY:
        raise RuntimeError("OPENBRAIN_API_KEY or OPENBRAIN_MCP_KEY is not set")

    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
    ).encode()
    req = request.Request(
        OPENBRAIN_API_BASE,
        data=payload,
        headers={
            "Authorization": f"Bearer {OPENBRAIN_API_KEY}",
            "x-brain-key": OPENBRAIN_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=OPENBRAIN_API_TIMEOUT) as resp:
        return parse_mcp_sse_payload(read_first_sse_event(resp))


def scan_for_secrets(text: str):
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


def word_count(text: str) -> int:
    return len(text.split())


def parse_frontmatter(raw: str):
    if not raw.startswith("---\n"):
        return {}, raw

    end = raw.find("\n---\n", 4)
    if end == -1:
        return {}, raw

    frontmatter = raw[4:end]
    body = raw[end + 5 :]
    meta = {}
    current_key = None

    for line in frontmatter.splitlines():
        if not line.strip():
            continue
        if line.startswith("  - ") or line.startswith("- "):
            if current_key:
                meta.setdefault(current_key, [])
                meta[current_key].append(line.split("- ", 1)[1].strip())
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            current_key = key
            if value:
                if value.startswith("[") and value.endswith("]"):
                    meta[key] = [v.strip().strip('"\'') for v in value[1:-1].split(",") if v.strip()]
                else:
                    meta[key] = value.strip('"\'')
            else:
                meta[key] = []

    return meta, body


def extract_date(meta: dict, path: Path) -> str:
    for key in ("date", "created", "created_at", "date_created"):
        val = meta.get(key)
        if val:
            if isinstance(val, str):
                s = val.strip()[:10]
                if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
                    return s
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def parse_note(path: Path):
    raw = path.read_text(errors="replace")
    meta, body = parse_frontmatter(raw)
    wikilinks = list(dict.fromkeys(w.strip() for w in WIKILINK_RE.findall(raw)))
    inline_tags = list(dict.fromkeys(INLINE_TAG_RE.findall(body)))

    raw_tags = meta.get("tags", [])
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    tags = list(dict.fromkeys([str(t) for t in raw_tags] + inline_tags))

    return meta, body, wikilinks, tags


def chunk_by_headings(body: str, title: str):
    parts = re.split(r"^(#{1,6}\s+.+)$", body, flags=re.MULTILINE)
    chunks = []
    current_section = title
    current_content = []

    for part in parts:
        heading_match = re.match(r"^#{1,6}\s+(.+)$", part.strip())
        if heading_match:
            text = "\n".join(current_content).strip()
            if text and word_count(text) > 10:
                chunks.append({"section": current_section, "content": text})
            current_section = heading_match.group(1).strip()
            current_content = []
        else:
            current_content.append(part)

    text = "\n".join(current_content).strip()
    if text and word_count(text) > 10:
        chunks.append({"section": current_section, "content": text})

    return chunks


def chunk_note(note):
    body = note["body"]
    wc = word_count(body)
    if wc <= WHOLE_NOTE_THRESHOLD:
        return [{"content": body.strip(), "section": None}]

    chunks = chunk_by_headings(body, note["title"])
    if len(chunks) <= 1:
        return [{"content": body.strip(), "section": None}]
    return chunks


def iter_notes(vault_root: Path, skip_folders: set[str]):
    all_skip = ALWAYS_SKIP | skip_folders
    for root, dirs, files in os.walk(vault_root):
        dirs[:] = [d for d in dirs if d not in all_skip and not d.startswith(".")]
        for fname in sorted(files):
            if not fname.endswith(".md"):
                continue
            full = Path(root) / fname
            rel = full.relative_to(vault_root)
            folder = str(rel.parent) if str(rel.parent) != "." else ""
            title = fname[:-3]
            yield full, str(rel), folder, title


def generate_embedding(text: str, api_base: str, api_key: str):
    payload = json.dumps({"model": EMBEDDING_MODEL, "input": text[:8000]}).encode()
    for attempt in range(MAX_RETRIES):
        try:
            req = request.Request(
                f"{api_base}/embeddings",
                data=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with request.urlopen(req, timeout=180) as resp:
                return json.loads(resp.read().decode())["data"][0]["embedding"]
        except (error.URLError, error.HTTPError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
            if attempt == MAX_RETRIES - 1:
                raise RuntimeError(f"embedding request failed: {exc}") from exc
            wait = RETRY_BACKOFF * (2 ** attempt)
            print(f"Embedding retry in {wait}s: {exc}", flush=True)
            time.sleep(wait)


def content_fingerprint(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(normalized.encode()).hexdigest()


def sync_log_path(script_dir: Path, vault_name: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", vault_name)
    return script_dir / f"obsidian-sync-{safe_name}.json"


def load_sync_log(script_dir: Path, vault_name: str) -> dict:
    path = sync_log_path(script_dir, vault_name)
    if path.exists():
        try:
            log = json.loads(path.read_text())
            log.setdefault("thoughts", {})
            log.setdefault("imported_fingerprints", [])
            return log
        except Exception:
            pass
    return {"vault_path": "", "last_run": "", "thoughts": {}, "imported_fingerprints": []}


def save_sync_log(script_dir: Path, vault_name: str, log: dict):
    sync_log_path(script_dir, vault_name).write_text(json.dumps(log, indent=2) + "\n")


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def vector_literal(values: list[float]) -> str:
    return "'[%s]'" % ",".join(format(v, ".12g") for v in values)


def insert_thought_postgres(
    content: str,
    embedding: list[float],
    metadata: dict,
    created_at: str,
    fingerprint: str,
    db_password: str,
):
    sql = (
        "INSERT INTO thoughts (content, embedding, metadata, created_at, content_fingerprint) "
        f"VALUES ({sql_literal(content)}, {vector_literal(embedding)}::vector, "
        f"{sql_literal(json.dumps(metadata))}::jsonb, {sql_literal(created_at)}::timestamptz, "
        f"{sql_literal(fingerprint)}) "
        "ON CONFLICT (content_fingerprint) DO NOTHING;"
    )
    env = os.environ.copy()
    env["PGPASSWORD"] = db_password
    result = run_kubectl(
        [
            "exec",
            "-i",
            "deploy/postgres",
            "--",
            "psql",
            "-U",
            "postgres",
            "-d",
            "openbrain",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        env=env,
        input_text=sql,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def insert_thought_via_api(content: str):
    result = call_openbrain_tool("capture_thought", {"content": content})
    if result.get("error"):
        raise RuntimeError(json.dumps(result["error"]))


def ensure_fingerprint_dedup(db_password: str):
    sql = """
ALTER TABLE thoughts
  ADD COLUMN IF NOT EXISTS content_fingerprint TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS thoughts_content_fingerprint_idx
  ON thoughts (content_fingerprint)
  WHERE content_fingerprint IS NOT NULL;
""".strip()

    env = os.environ.copy()
    env["PGPASSWORD"] = db_password
    result = run_kubectl(
        [
            "exec",
            "-i",
            "deploy/postgres",
            "--",
            "psql",
            "-U",
            "postgres",
            "-d",
            "openbrain",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        env=env,
        input_text=sql,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def main():
    parser = argparse.ArgumentParser(description="Import an Obsidian vault into self-hosted OpenBrain")
    parser.add_argument("vault_path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-words", type=int, default=DEFAULT_MIN_WORDS)
    parser.add_argument("--skip-folders", type=str, default="")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--secret-scan", action="store_true")
    parser.add_argument("--embedding-api-base", default=os.environ.get("EMBEDDING_API_BASE", "http://192.168.0.13:11434/v1"))
    parser.add_argument("--embedding-api-key", default=os.environ.get("EMBEDDING_API_KEY", "ollama"))
    parser.add_argument("--db-password", default=os.environ.get("OPENBRAIN_DB_PASSWORD", ""))
    parser.add_argument("--vault-name", default="")
    args = parser.parse_args()

    vault_root = Path(args.vault_path).expanduser().resolve()
    if not vault_root.is_dir():
        print(f"Error: vault not found at {vault_root}", file=sys.stderr)
        sys.exit(1)

    vault_name = args.vault_name.strip() or vault_root.name

    skip_folders = {f.strip() for f in args.skip_folders.split(",") if f.strip()}
    script_dir = Path(__file__).parent
    sync_log = load_sync_log(script_dir, vault_name)

    notes = []
    for full_path, rel_path, folder, title in iter_notes(vault_root, skip_folders):
        meta, body, wikilinks, tags = parse_note(full_path)
        if word_count(body) < args.min_words:
            continue
        note_hash = hashlib.sha256(body.encode()).hexdigest()[:16]
        existing = sync_log.get("thoughts", {}).get(rel_path)
        if existing and existing.get("note_hash") == note_hash:
            continue
        notes.append(
            {
                "title": title,
                "path": rel_path,
                "folder": folder,
                "body": body,
                "tags": tags,
                "wikilinks": wikilinks,
                "meta": meta,
                "full_path": full_path,
                "note_hash": note_hash,
            }
        )

    if args.limit > 0:
        notes = notes[: args.limit]

    thoughts = []
    secrets = []
    imported_fingerprints = set(sync_log.get("imported_fingerprints", []))

    for note in notes:
        note_date = extract_date(note["meta"], note["full_path"])
        for chunk in chunk_note(note):
            section_part = f" > {chunk['section']}" if chunk["section"] else ""
            content = f"[Obsidian: {note['title']} | {note['folder']}{section_part}] {chunk['content']}"
            if args.secret_scan:
                secret = scan_for_secrets(content)
                if secret:
                    secrets.append({"path": note["path"], "reason": secret})
                    continue
            fingerprint = content_fingerprint(content)
            if fingerprint in imported_fingerprints:
                continue
            thoughts.append(
                {
                    "content": content,
                    "metadata": {
                        "source": "obsidian",
                        "vault": vault_name,
                        "title": note["title"],
                        "folder": note["folder"],
                        "tags": note["tags"],
                        "date": note_date,
                        "wikilinks": note["wikilinks"],
                        "content_fingerprint": fingerprint,
                    },
                }
            )

    print(f"Vault: {vault_root}")
    print(f"Vault name: {vault_name}")
    print(f"Notes selected: {len(notes)}")
    print(f"Thoughts generated: {len(thoughts)}")
    print(f"Thoughts skipped for secrets: {len(secrets)}")
    print(f"Embedding model configured: {EMBEDDING_MODEL}")

    if args.verbose and secrets:
        print("\nSecret scan skips:")
        for item in secrets[:10]:
            print(f"  {item['path']}: {item['reason']}")

    if args.dry_run:
        print("\n=== DRY RUN ===")
        for thought in thoughts[:10]:
            preview = thought["content"][:140].replace("\n", " ")
            print(f"- {preview}{'...' if len(thought['content']) > 140 else ''}")
        return

    if not thoughts:
        print("\nNo new thoughts to import")
        return

    db_password = resolve_db_password(args.db_password)
    use_api_fallback = bool(OPENBRAIN_API_KEY)

    if use_api_fallback:
        print(f"Using OpenBrain API import path at {OPENBRAIN_API_BASE}")
        print("Metadata extraction model handled by OpenBrain server config (expected: qwen3.5:27b)")
    elif db_password:
        ensure_fingerprint_dedup(db_password)
    else:
        print("Error: unable to resolve database password from --db-password, OPENBRAIN_DB_PASSWORD, or kubectl secret postgres-password", file=sys.stderr)
        sys.exit(1)

    inserted = 0
    imported_fingerprint_list = sync_log.setdefault("imported_fingerprints", [])
    if use_api_fallback:
        print(f"Import mode: OpenBrain API capture_thought (embedding via server-configured model: {EMBEDDING_MODEL})")
    else:
        print(f"Import mode: direct Postgres + local embeddings via {EMBEDDING_MODEL}")
    for idx, thought in enumerate(thoughts, start=1):
        if use_api_fallback:
            insert_thought_via_api(thought["content"])
        else:
            embedding = generate_embedding(thought["content"], args.embedding_api_base, args.embedding_api_key)
            insert_thought_postgres(
                thought["content"],
                embedding,
                thought["metadata"],
                f"{thought['metadata']['date']}T00:00:00Z",
                thought["metadata"]["content_fingerprint"],
                db_password,
            )
        fingerprint = thought["metadata"]["content_fingerprint"]
        if fingerprint not in imported_fingerprints:
            imported_fingerprints.add(fingerprint)
            imported_fingerprint_list.append(fingerprint)
            save_sync_log(script_dir, vault_name, sync_log)
        inserted += 1
        if args.verbose and (idx % 10 == 0 or idx == len(thoughts)):
            print(f"Imported {idx}/{len(thoughts)}")
        time.sleep(0.15)

    sync_log["vault_path"] = str(vault_root)
    sync_log["last_run"] = datetime.now(tz=timezone.utc).isoformat()
    notes_log = sync_log.setdefault("thoughts", {})
    for note in notes:
        notes_log[note["path"]] = {
            "note_hash": note["note_hash"],
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    save_sync_log(script_dir, vault_name, sync_log)

    print(f"\nImported {inserted} thoughts")


if __name__ == "__main__":
    main()
