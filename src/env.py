"""Lecture minimale d'un fichier .env (sans dépendance externe)."""
from __future__ import annotations

import os
from pathlib import Path

from .config import resolve


def load_env(path: str | Path = ".env") -> dict[str, str]:
    """Charge .env dans os.environ (sans écraser l'existant) et renvoie les paires lues."""
    values: dict[str, str] = {}
    p = resolve(path)
    if not p.exists():
        return values
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        values[key] = val
        os.environ.setdefault(key, val)
    return values
