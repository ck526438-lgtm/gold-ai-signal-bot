"""Pont MetaTrader 5 pour Linux (Kali, Ubuntu…) — partie serveur.

Ce script tourne sous **Python pour Windows dans Wine**, à côté du terminal MetaTrader 5,
et expose le module MetaTrader5 à l'application Linux sur 127.0.0.1 uniquement.

    wine python.exe mt5_bridge_server.py --port 18822 --token <jeton>

Protocole : une ligne JSON par requête  {"token", "fn", "args", "kwargs"}
            une ligne JSON par réponse  {"ok": true, "result": ...} ou {"ok": false, "error": "..."}
Uniquement la bibliothèque standard + MetaTrader5 (pas de dépendance).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hmac
import json
import socketserver
import sys
import threading

MT5 = None
TOKEN = ""
LOCK = threading.Lock()


def to_json(x):
    """Convertit les résultats de MetaTrader5 (namedtuples, tableaux numpy…) en JSON."""
    if x is None or isinstance(x, (bool, int, float, str)):
        return x
    if hasattr(x, "_asdict"):
        return {"__nt__": {k: to_json(v) for k, v in x._asdict().items()}}
    dtype = getattr(x, "dtype", None)
    if dtype is not None and getattr(dtype, "names", None):       # tableau structuré numpy
        names = dtype.names
        return {"__rows__": [{n: to_json(row[n]) for n in names} for row in x]}
    if hasattr(x, "item") and callable(x.item):                    # scalaire numpy
        return x.item()
    if isinstance(x, bytes):
        return x.decode("utf-8", "replace")
    if isinstance(x, _dt.datetime):
        return x.timestamp()
    if isinstance(x, dict):
        return {str(k): to_json(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [to_json(v) for v in x]
    if hasattr(x, "__dict__"):                                     # objets simples (tests)
        return {"__nt__": {k: to_json(v) for k, v in vars(x).items() if not k.startswith("_")}}
    return str(x)


def constants(mod) -> dict:
    out = {}
    for k in dir(mod):
        if k.isupper():
            v = getattr(mod, k)
            if isinstance(v, (int, float, str)):
                out[k] = v
    return out


def handle(req: dict) -> dict:
    if TOKEN and not hmac.compare_digest(str(req.get("token", "")), TOKEN):
        return {"ok": False, "error": "jeton du pont MT5 invalide"}
    fn = req.get("fn", "")
    if fn == "__constants__":
        return {"ok": True, "result": constants(MT5)}
    if fn == "__ping__":
        return {"ok": True, "result": "pong"}
    if fn.startswith("_") or not hasattr(MT5, fn) or not callable(getattr(MT5, fn)):
        return {"ok": False, "error": f"fonction inconnue : {fn}"}
    try:
        with LOCK:  # le module MetaTrader5 n'est pas conçu pour des appels simultanés
            result = getattr(MT5, fn)(*req.get("args", []), **req.get("kwargs", {}))
        return {"ok": True, "result": to_json(result)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            line = line.strip()
            if not line:
                continue
            try:
                resp = handle(json.loads(line))
            except json.JSONDecodeError as e:
                resp = {"ok": False, "error": f"requête illisible : {e}"}
            self.wfile.write((json.dumps(resp) + "\n").encode("utf-8"))
            self.wfile.flush()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(module, host: str = "127.0.0.1", port: int = 18822, token: str = "") -> Server:
    """Démarre le serveur (utilisé aussi par les tests avec un faux module)."""
    global MT5, TOKEN
    MT5, TOKEN = module, token
    return Server((host, port), Handler)


def main() -> None:
    ap = argparse.ArgumentParser(description="Pont MetaTrader 5 pour Gold AI Signal Bot")
    ap.add_argument("--port", type=int, default=18822)
    ap.add_argument("--token", default="")
    ap.add_argument("--log", default="", help="fichier journal (utile avec pythonw.exe, sans console)")
    a = ap.parse_args()
    if a.log:
        stream = open(a.log, "a", buffering=1, encoding="utf-8")  # noqa: SIM115
        sys.stdout = sys.stderr = stream
    try:
        import MetaTrader5 as mt5
    except ImportError:
        sys.exit("Module MetaTrader5 introuvable : lance « python.exe -m pip install MetaTrader5 » dans Wine.")
    srv = serve(mt5, "127.0.0.1", a.port, a.token)
    print(f"Pont MetaTrader 5 prêt sur 127.0.0.1:{a.port} (Ctrl+C pour arrêter)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
