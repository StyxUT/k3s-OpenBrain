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

import requests
import subprocess


ALWAYS_SKIP = {".obsidian", ".trash", ".git", "node_modules"}
DEFAULT_MIN_WORDS = 20
WHOLE_NOTE_THRESHOLD = 500
EMBEDDING_MODEL = "qwen3-embedding"

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
    resp = requests.post(
        f"{api_base}/embeddings",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"model": EMBEDDING_MODEL, "input": text[:8000]},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


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
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"vault_path": "", "last_run": "", "thoughts": {}}


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
    db_password: str,
):
    sql = (
        "INSERT INTO thoughts (content, embedding, metadata, created_at) "
        f"VALUES ({sql_literal(content)}, {vector_literal(embedding)}::vector, "
        f"{sql_literal(json.dumps(metadata))}::jsonb, {sql_literal(created_at)}::timestamptz);"
    )
    env = os.environ.copy()
    env["PGPASSWORD"] = db_password
    result = subprocess.run(
        [
            "kubectl",
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
        input=sql,
        capture_output=True,
        text=True,
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

    for note in notes:
        note_date = extract_date(note["meta"], note["full_path"])
        for chunk in chunk_note(note):
            section_part = f" > {chunk['section']}" if chunk["section"] else ""
            content = f"[Obsidian: {note['title']} | {note['folder']}{section_part}] {chunk['content']}"
            secret = scan_for_secrets(content)
            if secret:
                secrets.append({"path": note["path"], "reason": secret})
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
                        "content_fingerprint": content_fingerprint(content),
                    },
                }
            )

    print(f"Vault: {vault_root}")
    print(f"Vault name: {vault_name}")
    print(f"Notes selected: {len(notes)}")
    print(f"Thoughts generated: {len(thoughts)}")
    print(f"Thoughts skipped for secrets: {len(secrets)}")

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

    if not args.db_password:
        print("Error: --db-password or OPENBRAIN_DB_PASSWORD is required for live import", file=sys.stderr)
        sys.exit(1)

    inserted = 0
    for idx, thought in enumerate(thoughts, start=1):
        embedding = generate_embedding(thought["content"], args.embedding_api_base, args.embedding_api_key)
        insert_thought_postgres(
            thought["content"],
            embedding,
            thought["metadata"],
            f"{thought['metadata']['date']}T00:00:00Z",
            args.db_password,
        )
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
