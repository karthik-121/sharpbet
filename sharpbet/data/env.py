"""Minimal .env loader (stdlib only). Real keys live in .env, which is git-ignored."""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_env(path: str | Path | None = None) -> dict[str, str]:
    """Read KEY=value lines from .env into os.environ (existing variables win).
    Looks in the current directory, then the repo root. Returns what the file held."""
    candidates = [Path(path)] if path else [Path.cwd() / ".env", REPO_ROOT / ".env"]
    for p in candidates:
        if p.is_file():
            values = {}
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip("'\"")
            for k, v in values.items():
                if v:
                    os.environ.setdefault(k, v)
            return values
    return {}
