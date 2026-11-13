#!/usr/bin/env python3
"""Gold AI Signal Bot — espace d'analyse des chandeliers japonais.

  python main.py                 lance l'espace d'analyse (navigateur + terminal)
  python main.py watch           surveillance dans le terminal uniquement
  python main.py scan            une analyse de la dernière bougie, puis quitte
  python main.py backtest        backtest dans le terminal
  python main.py selftest        vérifie l'installation puis quitte

Le robot de trading (onglet Robot) est désactivé par défaut.

Options : --demo (marché simulé), --source mt5|yfinance|csv, --csv FICHIER,
          --port 8010, --no-browser, --no-bell
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
import webbrowser
from pathlib import Path

from analyzer.config import load_config
from analyzer.data import SOURCE_LABELS
from analyzer.paths import FROZEN, VERSION, load_env, prepare_user_dir, resource_dir, user_dir
from analyzer.terminal import TerminalNotifier


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1" if host == "0.0.0.0" else host, port)) == 0


def cmd_start(cfg, args, term: TerminalNotifier) -> None:
    import threading

    import uvicorn

    from analyzer.server import create_app

    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
    if _port_in_use(args.host, args.port):
        # déjà lancé (double-clic sur le raccourci) : on ouvre simplement l'interface
        print(f"Gold AI Signal Bot est déjà ouvert : {url}")
        if not args.no_browser:
            webbrowser.open(url)
        time.sleep(2)
        return
    term.banner(url, SOURCE_LABELS.get(cfg.source, "auto (MetaTrader 5, sinon Yahoo Finance)"))
    app = create_app(cfg, args.config, terminal=term)
    if not args.no_browser:
        threading.Timer(2.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_watch(cfg, term: TerminalNotifier, once: bool = False) -> None:
    from analyzer.executor import Executor
    from analyzer.server import seconds_to_next_close
    from analyzer.service import AnalysisService

    svc = AnalysisService(cfg)
    svc.listeners.append(lambda ev: term.signal(ev["signal"]) if ev["type"] == "signal"
                         else term.error(ev["message"]) if ev["type"] == "error" else None)
    executor = None
    if not once:
        term.banner("(mode terminal — pas d'interface web)", SOURCE_LABELS.get(cfg.source, "auto"))
        executor = Executor(cfg, Path(cfg.data_dir))
        executor.listeners.append(lambda ev: term.bot(ev["entry"]) if ev["type"] == "bot" else None)
        svc.executor = executor
        if cfg.trading.start_enabled:
            try:
                executor.enable(cfg.trading.allow_live)
            except Exception as e:  # noqa: BLE001
                term.error(f"Robot non activé : {e}")
    while True:
        svc.refresh()
        term.market(svc.snapshot())
        if once:
            radar = (svc.snapshot() or {}).get("radar")
            if radar and not radar["blocked"]:
                term.signal(radar)
            return
        end = time.time() + seconds_to_next_close()
        while time.time() < end:             # gestion des positions toutes les 10 s
            time.sleep(min(10, max(0.0, end - time.time())))
            executor.manage()


def cmd_backtest(cfg) -> None:
    from analyzer.backtest import run_backtest
    from analyzer.data import load_data

    df, src = load_data(cfg)
    print(f"Données {SOURCE_LABELS.get(src, src)} : {len(df)} bougies M5 "
          f"du {df.index[0]:%d/%m/%Y} au {df.index[-1]:%d/%m/%Y}\n")
    res = run_backtest(df, cfg)
    st = res["stats"]
    if not st.get("trades"):
        print("Aucun trade sur la période.")
        return
    labels = {"trades": "Trades", "win_rate": "Réussite %", "total_r": "Total (R)",
              "expectancy_r": "Espérance (R/trade)", "profit_factor": "Profit factor",
              "max_drawdown_r": "Drawdown max (R)", "max_losing_streak": "Pertes d'affilée max",
              "exits": "Sorties"}
    for k, lab in labels.items():
        print(f"  {lab:<22} {st.get(k)}")
    print("\n  Par figure :")
    for g in res["by_pattern"]:
        print(f"    {g['key']:<38} {g['trades']:>4} trades  {g['win_rate']:>5.1f} %  {g['total_r']:>+7.2f} R")


def cmd_selftest(cfg, config_path: str) -> int:
    """Vérifie que l'application installée est complète : démarre le vrai serveur sur un port
    libre, interroge son API, puis s'arrête (utilisé par les installateurs et le build)."""
    import json
    import threading
    import urllib.request

    import uvicorn

    from analyzer.server import create_app

    checks = []
    web = resource_dir() / "web"
    checks.append(("Interface web", (web / "index.html").exists() and (web / "app.js").exists()))
    checks.append(("Graphiques", (web / "vendor" / "lightweight-charts.standalone.production.js").exists()))
    checks.append(("Guide d'utilisation", (resource_dir() / "docs" / "Guide_Gold_AI_Signal_Bot.pdf").exists()))
    try:
        import MetaTrader5  # noqa: F401
        checks.append(("Module MetaTrader 5", True))
    except ImportError:
        bridge = cfg.mt5_bridge
        checks.append((f"MetaTrader 5 : {'pont Wine ' + bridge if bridge else 'non disponible (Linux : Yahoo / Simulateur)'}",
                       os.name != "nt"))
    cfg.source, cfg.data_dir, cfg.mt5_bridge = "demo", str(user_dir() / "selftest-data"), ""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(cfg, str(user_dir() / "selftest-config.yaml")),
                                           host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()

    def get(path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=30) as r:
            body = r.read()
            return r.status, (json.loads(body) if path.startswith("/api") else body)

    try:
        for _ in range(120):
            if server.started:
                break
            time.sleep(0.25)
        checks.append(("Serveur local", server.started))
        checks.append(("Analyse (démo)", get("/api/state")[1].get("ready") is True))
        checks.append(("Page d'accueil", get("/")[0] == 200))
        checks.append(("Robot (désactivé par défaut)", get("/api/trading")[1]["enabled"] is False))
    except Exception as e:  # noqa: BLE001
        checks.append((f"Serveur local ({e})", False))
    finally:
        server.should_exit = True
        th.join(timeout=10)
    ok = all(v for _, v in checks)
    for name, v in checks:
        print(f"  {'OK ' if v else 'ÉCHEC'}  {name}")
    print(f"Gold AI Signal Bot {VERSION} — {'installation valide' if ok else 'installation INCOMPLÈTE'}")
    import shutil
    shutil.rmtree(user_dir() / "selftest-data", ignore_errors=True)
    (user_dir() / "selftest-config.yaml").unlink(missing_ok=True)
    return 0 if ok else 1


def _setup_logging(verbose: bool) -> None:
    """Console : avertissements seulement. Fichier logs/app.log : tout, pour le diagnostic."""
    from logging.handlers import RotatingFileHandler

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s : %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        logs = user_dir() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(logs / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    for noisy in ("httpx", "urllib3", "yfinance", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    _utf8_console()
    if FROZEN or os.getenv("GOLD_AI_HOME"):
        os.chdir(prepare_user_dir())       # données dans %LOCALAPPDATA%\Gold AI Signal Bot
    load_env(user_dir() / ".env")
    ap = argparse.ArgumentParser(description=f"Gold AI Signal Bot {VERSION}")
    ap.add_argument("command", nargs="?", default="start",
                    choices=["start", "watch", "scan", "backtest", "selftest"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--source", choices=["auto", "mt5", "yfinance", "csv", "demo"])
    ap.add_argument("--demo", action="store_true", help="marché simulé (pour découvrir l'outil)")
    ap.add_argument("--csv", help="fichier CSV de bougies M5 (heure UTC)")
    ap.add_argument("--bars", type=int, help="nombre de bougies à charger")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-bell", action="store_true", help="pas de bip à chaque signal")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    _setup_logging(a.verbose)
    cfg = load_config(a.config)
    if a.source:
        cfg.source = a.source
    if a.demo:
        cfg.source = "demo"
    if a.csv:
        cfg.source, cfg.csv_path = "csv", a.csv
    if a.bars:
        cfg.bars = a.bars
    term = TerminalNotifier(cfg.symbol, bell=not a.no_bell)

    if a.command == "start":
        cmd_start(cfg, a, term)
    elif a.command == "watch":
        try:
            cmd_watch(cfg, term)
        except KeyboardInterrupt:
            print("\nArrêt.")
    elif a.command == "scan":
        cmd_watch(cfg, term, once=True)
    elif a.command == "backtest":
        cmd_backtest(cfg)
    elif a.command == "selftest":
        sys.exit(cmd_selftest(cfg, a.config))


if __name__ == "__main__":
    main()
