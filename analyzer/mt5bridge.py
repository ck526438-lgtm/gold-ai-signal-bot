"""Client du pont MetaTrader 5 (Linux) : se comporte comme le module MetaTrader5.

Les appels sont transmis au serveur `bridge/mt5_bridge_server.py` qui tourne dans Wine
à côté du terminal MT5. Les résultats reviennent avec les mêmes attributs
(account_info().balance, positions_get()[0].ticket…).
"""
from __future__ import annotations

import json
import os
import socket
import threading
from types import SimpleNamespace


def from_json(x):
    if isinstance(x, dict):
        if "__nt__" in x:
            return SimpleNamespace(**{k: from_json(v) for k, v in x["__nt__"].items()})
        if "__rows__" in x:
            return x["__rows__"]
        return {k: from_json(v) for k, v in x.items()}
    if isinstance(x, list):
        return tuple(from_json(v) for v in x)
    return x


def _arg(v):
    """Les dates sont envoyées en secondes UNIX (accepté par MetaTrader5)."""
    if hasattr(v, "timestamp"):
        return int(v.timestamp())
    return v


class BridgeMT5:
    def __init__(self, address: str, token: str | None = None, timeout: float = 20.0):
        host, _, port = address.rpartition(":")
        self.host, self.port = host or "127.0.0.1", int(port or 18822)
        self.token = token if token is not None else os.getenv("MT5_BRIDGE_TOKEN", "")
        self.timeout = timeout
        self._sock = None
        self._file = None
        self._lock = threading.Lock()
        self._consts: dict | None = None

    # ------------------------------------------------------------------ transport
    def _connect(self):
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise RuntimeError(
                f"Pont MetaTrader 5 injoignable sur {self.host}:{self.port} ({e}). "
                "Lance « gold-ai-mt5 » (MetaTrader 5 + pont sous Wine), puis réessaie.") from e
        self._file = self._sock.makefile("rwb")

    def _close(self):
        for obj in (self._file, self._sock):
            try:
                if obj:
                    obj.close()
            except OSError:
                pass
        self._sock = self._file = None

    def call(self, fn: str, *args, **kwargs):
        req = {"token": self.token, "fn": fn, "args": [_arg(a) for a in args],
               "kwargs": {k: _arg(v) for k, v in kwargs.items()}}
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._connect()
                    self._file.write((json.dumps(req) + "\n").encode("utf-8"))
                    self._file.flush()
                    line = self._file.readline()
                    if not line:
                        raise ConnectionError("connexion fermée par le pont")
                    break
                except (OSError, ConnectionError):
                    self._close()
                    if attempt == 2:
                        raise
        resp = json.loads(line)
        if not resp.get("ok"):
            raise RuntimeError(f"Pont MetaTrader 5 : {resp.get('error')}")
        return from_json(resp["result"])

    # ------------------------------------------------------------------ module
    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name.isupper():
            if self._consts is None:
                self._consts = self.call("__constants__")
            if name in self._consts:
                return self._consts[name]
            raise AttributeError(name)
        return lambda *a, **k: self.call(name, *a, **k)

    def ping(self) -> bool:
        return self.call("__ping__") == "pong"
