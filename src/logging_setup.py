"""Logs loguru : rotation quotidienne + masquage systématique des secrets."""
from __future__ import annotations

import sys
from typing import Iterable

from loguru import logger

from .config import resolve

_SECRETS: set[str] = set()


def register_secret(*secrets: str | None) -> None:
    """Tout secret enregistré est remplacé par *** dans TOUS les messages de log."""
    for s in secrets:
        if s and len(s) >= 6:
            _SECRETS.add(s)


def redact(text: str, secrets: Iterable[str] | None = None) -> str:
    for s in secrets if secrets is not None else _SECRETS:
        if s:
            text = text.replace(s, "***")
    return text


def _patcher(record):
    if _SECRETS:
        record["message"] = redact(record["message"])
        if record["exception"] is not None:
            exc = record["exception"]
            if exc.value is not None:
                try:
                    exc.value.args = tuple(redact(str(a)) for a in exc.value.args)
                except Exception:  # pragma: no cover
                    pass


def setup_logging(cfg: dict, extra_sink=None) -> None:
    log_cfg = cfg.get("logging", {})
    log_dir = resolve(log_cfg.get("dir", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    level = log_cfg.get("level", "INFO")
    logger.remove()
    logger.configure(patcher=_patcher)
    if sys.stderr is not None:  # None dans un .exe sans console
        logger.add(sys.stderr, level=level, backtrace=False, diagnose=False)
    logger.add(
        log_dir / "bot_{time:YYYY-MM-DD}.log",
        level=level,
        rotation="00:00",
        retention=f"{int(log_cfg.get('retention_days', 30))} days",
        encoding="utf-8",
        backtrace=False,
        diagnose=False,  # diagnose=True pourrait afficher des variables (dont des secrets)
    )
    if extra_sink is not None:
        logger.add(extra_sink, level=level, format="{time:HH:mm:ss} | {level: <7} | {message}")
