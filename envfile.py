"""Read/write KEY=value env files (nexnoc.env) without logging values.

DB and config.json store only env *names*. Values live here (or in the
process environment). The portal writes through this module; the poller
re-reads the file on each resolve so a portal edit takes effect without
restarting nexnoc-poller.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def default_env_path() -> Path:
    override = (os.environ.get("NEXNOC_ENV_FILE") or "").strip()
    if override:
        return Path(override)
    prod = Path("/etc/nexnoc/nexnoc.env")
    if prod.is_file():
        return prod
    return Path(__file__).resolve().parent / "config" / "nexnoc.env"


def is_env_key(name: str) -> bool:
    return bool(name and _KEY_RE.match(name))


def _parse_line(line: str) -> Optional[tuple[str, str]]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    if not is_env_key(key):
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def read_all(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        parsed = _parse_line(line)
        if parsed:
            out[parsed[0]] = parsed[1]
    return out


def get_value(path: Path, name: str) -> Optional[str]:
    if not name:
        return None
    return read_all(path).get(name)


def is_set(path: Path, name: Optional[str]) -> bool:
    if not name:
        return False
    value = os.environ.get(name)
    if value:
        return True
    stored = get_value(path, name)
    return bool(stored)


def upsert_values(path: Path, updates: dict[str, str]) -> None:
    """Set or replace keys. Empty string deletes the assignment.

    Preserves comments and unrelated lines. Creates the file at mode 0o640
    when missing. Never logs values.
    """
    clean: dict[str, str] = {}
    for key, value in updates.items():
        if not is_env_key(key):
            raise ValueError(f"invalid env var name {key!r}")
        clean[key] = "" if value is None else str(value)
    if not clean:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = existing.splitlines() if existing else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        parsed = _parse_line(line)
        if parsed and parsed[0] in clean:
            key = parsed[0]
            seen.add(key)
            if clean[key] != "":
                out.append(f"{key}={clean[key]}")
            continue
        out.append(line)
    for key, value in clean.items():
        if key in seen or value == "":
            continue
        out.append(f"{key}={value}")
    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass
    for key, value in clean.items():
        if value:
            os.environ[key] = value
        elif key in os.environ:
            del os.environ[key]
