"""Append-only JSONL audit log. Rotates at 10 MB.

Inventory writes are blocked if the audit line cannot be written.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("nexnoc.audit")

AUDIT_MAX_FILE_BYTES = 10_485_760
AUDIT_MAX_READ_BYTES = 2_097_152


def default_audit_path() -> Path:
    env = (os.environ.get("NEXNOC_AUDIT_FILE") or "").strip()
    if env:
        return Path(env)
    prod_dir = Path("/var/lib/nexnoc")
    if prod_dir.is_dir():
        return prod_dir / "audit.jsonl"
    return Path(__file__).resolve().parent / "audit.jsonl"


def _rotate(path: Path) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < AUDIT_MAX_FILE_BYTES:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    try:
        path.rename(backup)
    except OSError as exc:
        logger.warning("audit rotate failed: %s", exc)


def audit_log(action: str, user: Optional[dict], ip: str = "",
              details: Optional[dict] = None, ok: Optional[bool] = None,
              path: Optional[Path] = None) -> bool:
    if not user:
        return False
    dest = path or default_audit_path()
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "username": user.get("username"),
        "user_id": user.get("id"),
        "ip": ip or "unknown",
        "action": action,
        "ok": ok,
    }
    if details:
        entry.update(details)
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            _rotate(dest)
        with dest.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except OSError as exc:
        logger.error("audit write failed: %s", exc)
        return False


def audit_read(limit: int = 200, offset: int = 0,
               path: Optional[Path] = None) -> dict:
    dest = path or default_audit_path()
    if not dest.is_file():
        return {"entries": [], "total": 0}
    try:
        size = dest.stat().st_size
    except OSError:
        return {"entries": [], "total": 0}
    if size == 0:
        return {"entries": [], "total": 0}
    read_from = max(0, size - AUDIT_MAX_READ_BYTES) if size > AUDIT_MAX_READ_BYTES else 0
    entries = []
    try:
        with dest.open("rb") as fh:
            if read_from:
                fh.seek(read_from)
                fh.readline()
            for raw in fh:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    entries.append(row)
    except OSError:
        return {"entries": [], "total": 0}
    entries.reverse()
    total = len(entries)
    limit = max(1, min(int(limit or 200), 500))
    offset = max(0, int(offset or 0))
    return {"entries": entries[offset:offset + limit], "total": total}
