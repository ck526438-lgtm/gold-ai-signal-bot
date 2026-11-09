"""Config loading: YAML + ${ENV} interpolation + tiny .env reader."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_dotenv(path: Path | None = None) -> None:
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        def sub(match: re.Match) -> str:
            return os.environ.get(match.group(1), "")
        return _ENV_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def resolve_path(rel: str | Path) -> Path:
    path = Path(rel)
    return path if path.is_absolute() else (ROOT / path)


def load_config(path: str | Path | None = None) -> dict:
    load_dotenv()
    cfg_path = Path(path) if path else None
    if cfg_path is None:
        cfg_path = ROOT / "config.yaml"
        if not cfg_path.exists():
            cfg_path = ROOT / "config.example.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return _interpolate(raw)


def get(cfg: dict, dotted: str, default: Any = None) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node
