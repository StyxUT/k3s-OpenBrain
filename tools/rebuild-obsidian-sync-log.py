#!/usr/bin/env python3
"""
Rebuild a per-vault Obsidian sync log from the source vault files.

Useful when rows were imported before per-vault sync logs existed.
"""

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def parse_frontmatter(raw: str):
    if not raw.startswith("---\n"):
        return {}, raw
    end = raw.find("\n---\n", 4)
    if end == -1:
        return {}, raw
    return {}, raw[end + 5 :]


def word_count(text: str) -> int:
    return len(text.split())


def sync_log_path(script_dir: Path, vault_name: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", vault_name)
    return script_dir / f"obsidian-sync-{safe_name}.json"


def main():
    parser = argparse.ArgumentParser(description="Rebuild a per-vault Obsidian sync log")
    parser.add_argument("vault_path")
    parser.add_argument("--vault-name", required=True)
    parser.add_argument("--min-words", type=int, default=20)
    args = parser.parse_args()

    vault_root = Path(args.vault_path).expanduser().resolve()
    script_dir = Path(__file__).parent

    log = {
        "vault_path": str(vault_root),
        "last_run": datetime.now(tz=timezone.utc).isoformat(),
        "thoughts": {},
    }

    for path in sorted(vault_root.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(vault_root).parts):
            continue
        raw = path.read_text(errors="replace")
        _, body = parse_frontmatter(raw)
        if word_count(body) < args.min_words:
            continue
        rel = str(path.relative_to(vault_root))
        note_hash = hashlib.sha256(body.encode()).hexdigest()[:16]
        log["thoughts"][rel] = {
            "note_hash": note_hash,
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }

    out = sync_log_path(script_dir, args.vault_name)
    out.write_text(json.dumps(log, indent=2) + "\n")
    print(out)
    print(f"tracked_notes={len(log['thoughts'])}")


if __name__ == "__main__":
    main()
