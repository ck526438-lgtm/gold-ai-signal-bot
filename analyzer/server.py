"""Serveur de l'espace d'analyse (FastAPI) : API JSON + flux temps réel + interface web."""
from __future__ import annotations

import asyncio
import io
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .autonomy import is_autostart, keep_awake, launch_command, set_autostart
from .config import Config, save_config, update_config
from .executor import Executor
from .mt5link import LINK
from .onboarding import detect, diagnostics
from .paths import VERSION, resource_dir, user_dir
from .service import AnalysisService
from .terminal import TerminalNotifier

log = logging.getLogger(__name__)
STATIC = resource_dir() / "web"
GUIDE = resource_dir() / "docs" / "Guide_Gold_AI_Signal_Bot.pdf"
SCAN_DELAY_S = 8  # laisse le temps au courtier de publier la bougie clôturée


def seconds_to_next_close(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    s = (now.minute % 5) * 60 + now.second + now.microsecond / 1e6
    return SCAN_DELAY_S - s if s < SCAN_DELAY_S else 300 - s + SCAN_DELAY_S


def create_app(cfg: Config, config_path: str = "config.yaml",
               terminal: TerminalNotifier | None = None, autostart: bool = True) -> FastAPI:
    LINK.set_bridge(cfg.mt5_bridge)
    LINK.set_path(cfg.mt5_path)
    service = AnalysisService(cfg)
    executor = Executor(cfg, Path(cfg.data_dir))
    service.executor = executor
    queues: set[asyncio.Queue] = set()
    state = {"loop": None, "backtest": None, "next_scan": None}

    def on_event(ev: dict) -> None:
        loop = state["loop"]
        if loop is None:
            return
        for q in list(queues):
            loop.call_soon_threadsafe(q.put_nowait, ev)

    service.listeners.append(on_event)
    executor.listeners.append(on_event)
    if terminal:
        service.listeners.append(
            lambda ev: terminal.signal(ev["signal"]) if ev["type"] == "signal"
            else terminal.error(ev["message"]) if ev["type"] == "error" else None)
        executor.listeners.append(lambda ev: terminal.bot(ev["entry"]) if ev["type"] == "bot" else None)

    async def run_refresh() -> None:
        await asyncio.to_thread(service.refresh)
        if terminal:
            terminal.market(service.snapshot())

    async def wait_for_new_bar(boundary: datetime) -> float:
        """MetaTrader 5 : attend que la nouvelle bougie apparaisse chez le courtier (≈ 1 s
        après la clôture) au lieu d'un délai fixe. Renvoie le délai observé (s)."""
        from .data import mt5_forming_bar_utc

        t0 = datetime.now(timezone.utc)
        limit = boundary.timestamp() + SCAN_DELAY_S
        while datetime.now(timezone.utc).timestamp() < limit:
            try:
                cur = await asyncio.to_thread(mt5_forming_bar_utc, service.cfg)
                if cur is not None and cur >= pd.Timestamp(boundary):
                    return (datetime.now(timezone.utc) - boundary).total_seconds()
            except Exception:  # noqa: BLE001 — repli : délai fixe
                break
            await asyncio.sleep(0.3)
        rest = limit - datetime.now(timezone.utc).timestamp()
        if rest > 0:
            await asyncio.sleep(rest)
        return (datetime.now(timezone.utc) - t0).total_seconds()

    async def scheduler() -> None:
        while True:
            wait = seconds_to_next_close()
            fast = service.source_used == "mt5"
            if fast:            # se réveille à la clôture exacte puis guette la nouvelle bougie
                wait = max(0.0, wait - SCAN_DELAY_S + 0.2)
            target = datetime.now(timezone.utc).timestamp() + wait
            state["next_scan"] = target
            await asyncio.sleep(wait)
            if fast:
                boundary = datetime.fromtimestamp((target + 1) // 300 * 300, timezone.utc)   # clôture visée
                state["last_bar_delay"] = round(await wait_for_new_bar(boundary), 2)
            try:
                await run_refresh()
            except Exception as e:  # noqa: BLE001 — la boucle ne doit jamais s'arrêter
                log.exception("Analyse impossible : %s", e)

    async def compute_edge() -> None:
        from .config import update_config
        from .data import load_data
        q = executor.quality.edge
        if q.computing:
            return
        q.computing = True
        df = None
        try:
            c = update_config(service.cfg, {"bars": service.cfg.trading.edge_bars})
            df, _ = await asyncio.to_thread(load_data, c)
            await asyncio.to_thread(q.compute, df, service.cfg)
            s = q.summary(service.cfg.trading)
            bars = f"{s['period']['bars']:,}".replace(",", " ")
            t = service.cfg.trading
            tradable = len(s["qualified"]) + (0 if t.edge_block_unproven else len(s["unproven"]))
            executor._log("warn" if tradable == 0 else "info",
                          f"Statistiques du robot recalculées sur {bars} bougies : {len(s['qualified'])} figure(s) "
                          f"rentable(s), {len(s['blocked'])} écartée(s) car perdante(s), {len(s['unproven'])} sans assez "
                          f"d'historique ({'tradées' if not t.edge_block_unproven else 'bloquées'})"
                          + (" — ATTENTION : aucune figure ne peut être tradée" if tradable == 0 else ""),
                          notify=tradable == 0)
        except Exception as e:  # noqa: BLE001
            q.error = str(e)
            log.warning("Calcul des statistiques impossible : %s", e)
        finally:
            q.computing = False
        if df is not None:
            await train_ml(df)

    async def train_ml(df) -> None:
        m = executor.quality.ml
        if m.computing or not service.cfg.trading.quality_ml:
            return
        m.computing = True
        try:
            rep = await asyncio.to_thread(m.compute, df, service.cfg, executor.quality.edge.management(service.cfg))
            b, f = rep["test_baseline"], rep["test_filtered"]
            verdict = ("actif : il filtre les signaux du robot" if rep["useful"] else
                       "affiché seulement : il ne bat pas la stratégie seule sur la période test")
            executor._log("info", f"Modèle IA entraîné sur {rep['samples']} opportunités — période test "
                                  f"jamais vue : {f['win_rate']:.0f} % gagnants avec IA ({f['trades']} trades) "
                                  f"contre {b['win_rate']:.0f} % sans ({b['trades']}) — {verdict}")
        except Exception as e:  # noqa: BLE001
            m.error = str(e)
            log.warning("Entraînement du modèle IA impossible : %s", e)
        finally:
            m.computing = False

    async def quality_maintenance() -> None:
        """Calendrier économique toutes les heures, statistiques une fois par jour."""
        while True:
            await asyncio.to_thread(executor.quality.news.refresh)
            age = executor.quality.edge.age_hours()
            ml_age = executor.quality.ml.age_hours()
            m = executor.quality.ml
            if age is None or age > 24 or (service.cfg.trading.quality_ml and m.available_to_train
                                            and m.error is None and (ml_age is None or ml_age > 24)):
                await compute_edge()
            await asyncio.sleep(3600)

    async def position_manager() -> None:
        """Break-even, stop suiveur et sortie au temps : toutes les 3 s si une position est
        ouverte, toutes les 10 s sinon (le robot n'attend pas la clôture de la bougie)."""
        while True:
            busy = bool(executor.store.q("SELECT 1 FROM trades WHERE status='ouvert' LIMIT 1"))
            await asyncio.sleep(1 if executor._pending else 3 if busy else 10)
            if executor.enabled or executor.t.mode == "mt5":
                await asyncio.to_thread(executor.manage)

    async def watchdog() -> None:
        """Mode autonome : relance le robot, surveille l'arrivée des données, bilan du jour."""
        down_since, alerted, last_try, summary_day = None, False, 0.0, None
        while True:
            await asyncio.sleep(30)
            t = service.cfg.trading
            if not t.autonomous:
                continue
            now = datetime.now(timezone.utc)
            # 1) robot relancé s'il n'est pas actif (sauf arrêt volontaire ou pause du jour)
            if not executor.enabled and not state.get("user_disabled") and not executor.paused \
                    and now.timestamp() - last_try > 120:
                last_try = now.timestamp()
                try:
                    await asyncio.to_thread(executor.enable, t.allow_live)
                    executor._log("ok", "Mode autonome : robot réactivé automatiquement", notify=True)
                except Exception as e:  # noqa: BLE001
                    log.info("Réactivation automatique impossible : %s", e)
            # 2) santé des données
            if service.error:
                down_since = down_since or now
                if not alerted and (now - down_since).total_seconds() > 600:
                    alerted = True
                    executor._log("err", f"Plus de données depuis 10 min : {service.error} — le robot ne peut pas "
                                         "trader (MT5 ouvert et connecté ?)", notify=True)
            else:
                if alerted:
                    executor._log("ok", "Données rétablies : le robot reprend l'analyse", notify=True)
                down_since, alerted = None, False
            # 3) bilan du jour
            if now.hour == 21 and summary_day != now.date():
                summary_day = now.date()
                try:
                    st = await asyncio.to_thread(executor.status)
                    td, cur = st.get("today") or {}, (st.get("account") or {}).get("currency", "")
                    if td:
                        executor._log("info", f"Bilan du jour : {td.get('trades', 0)} trade(s), réalisé "
                                              f"{td.get('realized', 0):+.2f} {cur}, en cours {td.get('floating', 0):+.2f} {cur}",
                                      notify=True)
                except Exception as e:  # noqa: BLE001
                    log.info("Bilan du jour impossible : %s", e)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["loop"] = asyncio.get_running_loop()
        tasks = []
        if autostart:
            await run_refresh()
            if cfg.trading.start_enabled:
                try:
                    await asyncio.to_thread(executor.enable, cfg.trading.allow_live)
                except Exception as e:  # noqa: BLE001
                    executor._log("err", f"Activation automatique du robot impossible : {e}", notify=True)
            tasks = [asyncio.create_task(scheduler()), asyncio.create_task(position_manager()),
                     asyncio.create_task(quality_maintenance()), asyncio.create_task(watchdog())]
            if cfg.trading.autonomous:
                keep_awake(True)
                executor._log("info", "Mode autonome : l'application tourne seule (robot relancé automatiquement, "
                                      "alertes de santé, bilan du jour à 21 h UTC)")
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(service.wait_idle)       # attend la fin d'une analyse en cours
        service.journal.close()
        executor.close()

    app = FastAPI(title="Gold AI Signal Bot", version=VERSION, lifespan=lifespan, docs_url="/api/docs")
    app.state.service = service

    # ------------------------------------------------------------- API
    @app.get("/api/state")
    def get_state():
        snap = service.snapshot()
        snap["next_scan"] = state["next_scan"]
        return snap

    @app.post("/api/scan")
    async def scan_now():
        await run_refresh()
        return {"ok": service.error is None, "error": service.error}

    @app.get("/api/journal")
    def journal(limit: int = 500):
        return {"signals": service.journal.list(limit), "stats": service.journal.stats()}

    @app.put("/api/journal/{key}/note")
    def note(key: str, payload: dict = Body(...)):
        service.journal.set_note(key, str(payload.get("note", "")))
        return {"ok": True}

    @app.get("/api/journal.csv")
    def journal_csv():
        import csv
        rows = service.journal.list(100_000)
        buf = io.StringIO()
        cols = ["time", "side", "pattern", "score", "quality", "entry", "sl", "tp1", "tp2",
                "risk_pips", "status", "result_r", "exit_time", "origin", "note"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=journal_gold_ai.csv"})

    @app.get("/api/meta")
    def meta():
        from .patterns import PATTERN_NAMES
        return {"patterns": PATTERN_NAMES, "sources": ["auto", "mt5", "yfinance", "csv", "demo"],
                "version": VERSION}

    @app.get("/api/settings")
    def get_settings():
        return service.cfg.to_dict()

    @app.put("/api/settings")
    async def put_settings(changes: dict = Body(...)):
        try:
            new = update_config(service.cfg, changes)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from e
        service.set_config(new)
        LINK.set_bridge(new.mt5_bridge)
        LINK.set_path(new.mt5_path)
        save_config(new, config_path)
        await run_refresh()
        return new.to_dict()

    @app.post("/api/backtest")
    async def backtest(payload: dict = Body(default={})):
        from .backtest import run_backtest
        from .data import load_data

        bars = int(payload.get("bars") or service.cfg.bars)
        changes = {"bars": max(300, min(bars, 100_000)),
                   "strategy": payload.get("strategy", {})}
        try:
            cfg_bt = update_config(service.cfg, changes)
            df, src = await asyncio.to_thread(load_data, cfg_bt)
            result = await asyncio.to_thread(run_backtest, df, cfg_bt)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"Backtest impossible : {e}") from e
        result["source"] = src
        state["backtest"] = result
        return result

    @app.get("/api/backtest/last")
    def last_backtest():
        return state["backtest"] or {}

    # ------------------------------------------------------------- robot
    def _err(e: Exception) -> HTTPException:
        code = 403 if isinstance(e, PermissionError) else 404 if isinstance(e, LookupError) else 400
        return HTTPException(code, str(e))

    @app.get("/api/trading")
    async def trading_status():
        return await asyncio.to_thread(executor.status)

    @app.post("/api/trading/enable")
    async def trading_enable(payload: dict = Body(default={})):
        try:
            out = await asyncio.to_thread(executor.enable, bool(payload.get("confirm_live")))
            state["user_disabled"] = False
            return out
        except Exception as e:  # noqa: BLE001
            raise _err(e) from e

    @app.post("/api/trading/disable")
    def trading_disable():
        executor.disable("Robot désactivé depuis l'interface")
        state["user_disabled"] = True       # le mode autonome ne le relancera pas
        return {"ok": True}

    @app.get("/api/trading/autonomous")
    def trading_autonomous_get():
        return {"enabled": service.cfg.trading.autonomous, "autostart": is_autostart(),
                "command": " ".join(launch_command())}

    @app.post("/api/trading/autonomous")
    async def trading_autonomous(payload: dict = Body(...)):
        on = bool(payload.get("enabled"))
        new = update_config(service.cfg, {"trading": {"autonomous": on, "start_enabled": on}})
        service.set_config(new)
        save_config(new, config_path)
        try:
            start = set_autostart(on)
        except OSError as e:
            start = f"démarrage automatique impossible : {e}"
        keep_awake(on)
        msg = (f"Mode autonome activé : {start}, robot activé à chaque lancement, anti-veille, alertes de santé"
               if on else f"Mode autonome désactivé ({start})")
        executor._log("info", msg, notify=True)
        if on and not executor.enabled:
            state["user_disabled"] = False
            try:
                await asyncio.to_thread(executor.enable, new.trading.allow_live)
            except Exception as e:  # noqa: BLE001
                msg += f" — robot pas encore actif : {e} (nouvel essai automatique toutes les 2 min)"
        return {"message": msg, "enabled": on, "autostart": is_autostart()}

    def find_signal(key: str) -> dict:
        sig = service.journal.get(key)
        if sig is None:
            snap = service.snapshot()
            for k in ("radar", "radar_recent"):
                r = snap.get(k)
                if r and r.get("key") == key:
                    sig = r
                    break
        if sig is None:
            raise HTTPException(404, "Signal introuvable")
        return sig

    @app.get("/api/trading/place/{key}")
    async def trading_place_preview(key: str):
        try:
            return await asyncio.to_thread(executor.preview_manual, find_signal(key))
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise _err(e) from e

    @app.post("/api/trading/place/{key}")
    async def trading_place(key: str):
        try:
            done = await asyncio.to_thread(executor.place_manual, find_signal(key))
            return {"ok": True, "orders": done}
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise _err(e) from e

    @app.post("/api/trading/test_order")
    async def trading_test_order():
        try:
            return await asyncio.to_thread(executor.test_order)
        except Exception as e:  # noqa: BLE001
            raise _err(e) from e

    @app.post("/api/trading/close_all")
    async def trading_close_all():
        try:
            return {"closed": await asyncio.to_thread(executor.close_all)}
        except Exception as e:  # noqa: BLE001
            raise _err(e) from e

    @app.post("/api/trading/close/{ticket}")
    async def trading_close(ticket: int):
        try:
            return {"ok": await asyncio.to_thread(executor.close_position, ticket)}
        except Exception as e:  # noqa: BLE001
            raise _err(e) from e

    @app.post("/api/trading/quality/recompute")
    async def quality_recompute():
        await compute_edge()
        if executor.quality.edge.error:
            raise HTTPException(400, executor.quality.edge.error)
        return executor.quality.status(service.cfg)

    @app.post("/api/trading/quality/news")
    async def quality_news():
        await asyncio.to_thread(executor.quality.news.refresh, True)
        return executor.quality.status(service.cfg)["news"]

    @app.get("/api/trading/profiles")
    def trading_profiles():
        from .config import PROFILES
        return [{"id": k, "label": v["label"], "desc": v["desc"]} for k, v in PROFILES.items()]

    @app.post("/api/trading/profile")
    async def trading_profile(payload: dict = Body(...)):
        from .config import PROFILES, apply_profile
        name = str(payload.get("name", ""))
        try:
            new = apply_profile(service.cfg, name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        service.set_config(new)
        save_config(new, config_path)
        executor._log("info", f"Profil « {PROFILES[name]['label']} » appliqué : {PROFILES[name]['desc']}", notify=True)
        asyncio.create_task(compute_edge())        # statistiques recalculées avec les nouveaux seuils
        return await asyncio.to_thread(executor.status)

    @app.put("/api/trading/settings")
    async def trading_settings(payload: dict = Body(...)):
        changes = dict(payload.get("trading") or {})
        if changes.get("allow_live") and not service.cfg.trading.allow_live \
                and payload.get("confirm") != "REEL":
            raise HTTPException(403, "Pour autoriser un compte réel, tape REEL dans la confirmation.")
        if "mode" in changes and changes["mode"] != service.cfg.trading.mode:
            executor.disable("Robot désactivé : changement de mode")
        try:
            new = update_config(service.cfg, {"trading": changes})
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from e
        service.set_config(new)
        save_config(new, config_path)
        executor._log("info", "Réglages du robot enregistrés")
        return await asyncio.to_thread(executor.status)

    @app.get("/api/stream")
    async def stream():
        q: asyncio.Queue = asyncio.Queue()
        queues.add(q)

        async def gen():
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=20)
                        yield f"data: {json.dumps(ev)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                queues.discard(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------- installation / diagnostic
    @app.get("/api/setup")
    def setup_state():
        c = service.cfg
        return {"setup_done": c.setup_done, "version": VERSION, "data_dir": str(user_dir().resolve()),
                "source": c.source, "symbol": c.symbol, "broker_utc_offset": c.broker_utc_offset,
                "mt5_path": c.mt5_path, "mt5_bridge": c.mt5_bridge, "guide": GUIDE.exists(),
                "platform": "windows" if __import__("os").name == "nt" else "linux"}

    @app.get("/api/setup/detect")
    async def setup_detect():
        return await asyncio.to_thread(detect, LINK, service.cfg.symbol)

    @app.post("/api/setup/apply")
    async def setup_apply(payload: dict = Body(...)):
        allowed = {k: payload[k] for k in ("source", "symbol", "broker_utc_offset", "mt5_path") if k in payload}
        try:
            new = update_config(service.cfg, {**allowed, "setup_done": True})
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from e
        service.set_config(new)
        LINK.set_path(new.mt5_path)
        save_config(new, config_path)
        await run_refresh()
        return {"ok": service.error is None, "error": service.error, "settings": new.to_dict()}

    @app.post("/api/setup/skip")
    def setup_skip():
        new = update_config(service.cfg, {"setup_done": True})
        service.set_config(new)
        save_config(new, config_path)
        return {"ok": True}

    @app.get("/api/diagnostics")
    async def get_diagnostics():
        return await asyncio.to_thread(diagnostics, service, executor, LINK)

    @app.post("/api/open-data-folder")
    def open_data_folder():
        import os
        path = str(user_dir().resolve())
        if os.name == "nt":
            os.startfile(path)  # noqa: S606 — dossier de l'utilisateur, application locale
            return {"ok": True, "path": path}
        return {"ok": False, "path": path}

    @app.get("/guide")
    def guide():
        if not GUIDE.exists():
            raise HTTPException(404, "Guide introuvable")
        return FileResponse(GUIDE, media_type="application/pdf", filename="Guide_Gold_AI_Signal_Bot.pdf",
                            content_disposition_type="inline")

    # ------------------------------------------------------------- interface
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app
