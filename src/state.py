"""État partagé du bot (pause / arrêt) dans state.json, écrit de façon atomique."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import resolve

DEFAULT_STATE = {"paused": False, "stop_requested": False, "close_positions_on_stop": False}


class StateStore:
    def __init__(self, path: str | Path = "state.json"):
        self.path = resolve(path)
        self._lock = threading.Lock()

    def read(self) -> dict:
        with self._lock:
            if not self.path.exists():
                return dict(DEFAULT_STATE)
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return dict(DEFAULT_STATE)
            return {**DEFAULT_STATE, **data}

    def update(self, **changes) -> dict:
        state = self.read()
        state.update(changes)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        return state

    def reset(self) -> dict:
        return self.update(**DEFAULT_STATE)
