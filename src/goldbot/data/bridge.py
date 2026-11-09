"""Bridge HTTP: le PC Windows expose les donnees MT5, Kali fait l'analyse.

Server:  python bot.py bridge --port 8765       (sur Windows, MT5 lance et connecte)
Client:  data.provider: bridge + data.bridge.url + token dans .env

Reponses en CSV: 50k bougies pesent ~2 Mo la ou le JSON en peserait 8.
"""
from __future__ import annotations

import argparse
import io
import json
import secrets
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pandas as pd

from ..config import get
from .bars import normalize

_TOKEN_HEADER = "X-Goldbot-Token"


class BridgeProvider:
    """Meme interface que MT5Provider, mais a distance."""

    name = "bridge"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.url = str(get(cfg, "data.bridge.url", "http://127.0.0.1:8765")).rstrip("/")
        self.token = str(get(cfg, "data.bridge.token", "") or "")
        self.symbol = str(get(cfg, "symbol.candidates", ["XAUUSD"])[0])
        self._point = 0.01

    def connect(self) -> dict:
        import requests

        self._requests = requests
        health = self._get("/health").json()
        if health.get("token_required") and not self.token:
            raise RuntimeError("data.bridge.token vide: mettre GOLDBOT_BRIDGE_TOKEN dans .env")
        self.symbol = health.get("symbol", self.symbol)
        self._point = float(health.get("point", 0.01))
        return health

    def _headers(self) -> dict:
        return {_TOKEN_HEADER: self.token} if self.token else {}

    def _get(self, path: str, params: dict | None = None):
        resp = self._requests.get(self.url + path, params=params, headers=self._headers(), timeout=90)
        if resp.status_code == 401:
            raise RuntimeError("Bridge: token refuse (401). Verifier GOLDBOT_BRIDGE_TOKEN.")
        resp.raise_for_status()
        return resp

    def terminal_info(self) -> dict:
        return self._get("/health").json()

    def tick(self) -> dict:
        return self._get("/tick").json()

    def point(self) -> float:
        return self._point

    def bars_range(self, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        resp = self._get("/bars", {
            "tf": timeframe,
            "from": pd.Timestamp(start, tz="UTC").strftime("%Y-%m-%dT%H:%M"),
            "to": pd.Timestamp(end, tz="UTC").strftime("%Y-%m-%dT%H:%M"),
        })
        return normalize(pd.read_csv(io.StringIO(resp.text)))

    def bars_count(self, timeframe: str, count: int, start_pos: int = 0) -> pd.DataFrame:
        resp = self._get("/bars", {"tf": timeframe, "count": count, "start_pos": start_pos})
        return normalize(pd.read_csv(io.StringIO(resp.text)))

    def shutdown(self) -> None:
        pass


def build_server(cfg: dict, host: str, port: int, token: str | None):
    from .mt5_provider import MT5Provider

    provider = MT5Provider(cfg)
    info = provider.connect()
    token = token if token is not None else (str(get(cfg, "data.bridge.token", "") or "") or None)
    if not token:
        token = secrets.token_urlsafe(16)

    class Handler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            return token is None or secrets.compare_digest(self.headers.get(_TOKEN_HEADER, ""), token)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (contrat BaseHTTPRequestHandler)
            if not self._authorized():
                return self._send(401, b'{"error":"unauthorized"}', "application/json")
            parsed = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                if parsed.path == "/health":
                    payload = {**provider.terminal_info(), "symbol": provider.symbol,
                               "point": provider.point(), "token_required": token is not None,
                               "server_time": datetime.now(timezone.utc).isoformat()}
                    return self._send(200, json.dumps(payload, default=str).encode(), "application/json")
                if parsed.path == "/tick":
                    return self._send(200, json.dumps(provider.tick(), default=str).encode(), "application/json")
                if parsed.path == "/bars":
                    tf = query.get("tf", "M5")
                    if query.get("count"):
                        df = provider.bars_count(tf, int(query["count"]), int(query.get("start_pos", 0)))
                    else:
                        fmt = "%Y-%m-%dT%H:%M"
                        start = datetime.strptime(query["from"], fmt).replace(tzinfo=timezone.utc)
                        end = datetime.strptime(query["to"], fmt).replace(tzinfo=timezone.utc)
                        df = provider.bars_range(tf, start, end)
                    return self._send(200, df.to_csv(index=False).encode(), "text/csv")
                self._send(404, b'{"error":"not found"}', "application/json")
            except Exception as exc:  # le serveur ne doit jamais mourir sur une requete
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")

        def log_message(self, fmt, *args):
            pass

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.goldbot_token = token
    httpd.goldbot_info = info
    return httpd, provider


def run_bridge(cfg: dict, host: str, port: int, token: str | None) -> None:
    httpd, provider = build_server(cfg, host, port, token)
    info = httpd.goldbot_info
    print(json.dumps({
        "listen": f"{host}:{port}",
        "symbol": provider.symbol,
        "mt5_server": info.get("server"),
        "mt5_login": info.get("login"),
        "token": httpd.goldbot_token,
    }, indent=2))
    print("Copier le token dans .env cote Kali: GOLDBOT_BRIDGE_TOKEN=...  puis Ctrl+C pour arreter.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        provider.shutdown()


def main(argv=None) -> int:
    from ..config import load_config

    args = argparse.ArgumentParser(description="Exposer MT5 au reseau local (Kali)").parse_args(argv)
    cfg = load_config()
    run_bridge(cfg, "0.0.0.0", int(get(cfg, "data.bridge.port", 8765)), None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
