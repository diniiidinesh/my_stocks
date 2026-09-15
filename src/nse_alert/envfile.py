from __future__ import annotations

import re
from pathlib import Path

ENV_PATH = Path(".env")


def upsert_env(values: dict[str, str], path: Path = ENV_PATH) -> None:
    """Create or update keys in a dotenv file, preserving other lines/comments."""
    existing: list[str] = []
    if path.exists():
        existing = path.read_text(encoding="utf-8").splitlines()

    remaining = dict(values)
    out: list[str] = []
    for line in existing:
        matched = False
        for key in list(remaining):
            if re.match(rf"^\s*{re.escape(key)}\s*=", line):
                out.append(f"{key}={remaining.pop(key)}")
                matched = True
                break
        if not matched:
            out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def read_env_value(key: str, path: Path = ENV_PATH) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(rf"^\s*{re.escape(key)}\s*=", line):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""
