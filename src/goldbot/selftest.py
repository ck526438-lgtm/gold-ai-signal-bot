"""Tests de coherence integration.

Le risque n'est pas que le code plante: c'est que le live et le backtest
racontent deux histoires differentes sur les memes bougies. Ces tests
rejouent de vrais signaux passes par le chemin live (journal + papier) et
comparent leurs chiffres a ceux du backtest.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pandas as pd

from .backtest import run_backtest_scored
from .config import get
from .features import compute_features
from .journal import close_paper, connect, list_open, open_paper
from .live import _engine_verdict, _fires, manage_paper, score_frame
from .risk import round_turn_cost
from .signals import rules as rules_engine
from .signals import ml as ml_engine

CHECKS: list[tuple[str, bool, str]] = []


def _report(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def cmd_selftest(cfg: dict, args) -> int:
    from .data.bars import closed_only
    from .data.history import load_cache

    CHECKS.clear()
    tf = str(get(cfg, "symbol.timeframe", "M5"))
    df = closed_only(load_cache(cfg, args.symbol, tf), tf)
    if len(df) < 5000:
        print("Cache trop court: lancer `python bot.py fetch`.")
        return 1

    scored = rules_engine.rule_frame(compute_features(df, cfg), cfg)
    scored = ml_engine.add_ml_columns(scored, cfg, walk_forward_mode=True)

    # 1. Les prix du signal live sont bien calcules depuis la bougie de signal.
    _report("features_presentes", all(c in scored.columns for c in ("atr", "ema_200", "vwap", "ml_prob_up")))
    _report("bougies_firmees_seulement", bool(scored["time"].iloc[-1] < pd.Timestamp.now(tz="UTC")))

    engines = ["rules", "ml", "consensus"]
    tmp = Path(tempfile.mkdtemp())
    test_cfg = json.loads(json.dumps(cfg, default=str))
    test_cfg["journal"] = {"db_path": str(tmp / "test.sqlite")}
    db = connect(test_cfg)

    for engine in engines:
        fire_col = {"rules": "rules_fire", "ml": "ml_fire", "consensus": None}[engine]
        if fire_col is None:
            mask = scored["rules_fire"] & scored["ml_fire"] & (scored["rules_side"] == scored["ml_side"])
        else:
            mask = scored[fire_col]
        log, stats = run_backtest_scored(scored, test_cfg, engine=engine)
        positions = [int(i) for i in mask.to_numpy().nonzero()[0]][-args.trades:]
        if not positions or log.empty:
            _report(f"{engine}: rejoue", False, "aucun signal a rejouer")
            continue

        mismatch_side, mismatch_risk, close_diffs = 0, 0, []
        for pos in positions:
            bar = scored.iloc[pos]
            sig = _engine_verdict(scored.iloc[: pos + 1], test_cfg, engine, args.symbol)
            # L'entree du backtest est l'ouverture SUIVANTE: on ne compare pas le
            # prix, on compare la charpente du trade (sens + distances en ATR).
            if sig["side"] != bar["rules_side"] and engine != "ml":
                mismatch_side += 1
            risk = abs(sig["entry"] - sig["sl"])
            expected = float(get(cfg, "risk.sl_atr", 1.5)) * float(bar["atr"])
            if sig["sl"] is None or abs(risk - expected) > 0.02:
                mismatch_risk += 1
            row = log.loc[(pd.to_datetime(log["entry_time"]) == pd.Timestamp(bar["time"]) + pd.Timedelta(minutes=5))]
            if row.empty:
                continue
            t = row.iloc[0]
            paper_cfg = dict(test_cfg)
            trade_id = open_paper(db, engine, sig, 0.01)
            reason = "open"
            for j in range(pos + 1, min(len(scored), pos + 1 + int(get(cfg, "backtest.max_hold_bars", 72)))):
                closed = manage_paper(db, paper_cfg, scored.iloc[[j]])
                if closed:
                    reason = closed[0]["reason"]
                    close_diffs.append(round(float(closed[0]["points"]) - float(t["points"]), 3))
                    break
            if reason == "open":
                # Le metier n'a pas declenche: si l'horizon depasse la fin des
                # donnees, le backtest a lui aussi cloture a la derniere bougie,
                # donc la comparaison reste valide ici.
                last = scored.iloc[-1]
                res = close_paper(db, trade_id, str(last["time"]), float(last["close"]),
                                  "selftest_end", round_turn_cost(paper_cfg))
                truncated = pos + int(get(cfg, "backtest.max_hold_bars", 72)) >= len(scored)
                close_diffs.append(round(float(res["points"]) - float(t["points"]), 3)
                                   if truncated else 999.0)
        _report(f"{engine}: sides_coherents", mismatch_side == 0, f"{mismatch_side} ecarts")
        _report(f"{engine}: sl_en_atr", mismatch_risk == 0, f"{mismatch_risk} ecarts")
        same = [d for d in close_diffs if abs(d) < 0.05]
        _report(f"{engine}: papier==backtest", len(same) == len(close_diffs),
                f"{len(same)}/{len(close_diffs)} ecarts < 0.05, pires {sorted(close_diffs, key=abs)[-3:]}")

    # 2. Le chemin bridge (Kali) doit rendre exactement les memes bougies.
    if not args.skip_bridge:
        try:
            from .data.bridge import BridgeProvider, build_server
            from .data.mt5_provider import MT5Provider

            direct = MT5Provider(cfg)
            direct.connect()
            httpd, _server_provider = build_server(cfg, "127.0.0.1", args.port, "test-token")
            import threading

            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            remote_cfg = json.loads(json.dumps(cfg, default=str))
            remote_cfg["data"] = {"provider": "bridge", "bridge": {"url": f"http://127.0.0.1:{args.port}",
                                                                   "token": "test-token"}}
            remote = BridgeProvider(remote_cfg)
            remote.connect()
            a = direct.bars_count(tf, 400)
            b = remote.bars_count(tf, 400)
            same_shape = a.shape == b.shape
            same_values = same_shape and bool(
                (abs(a["close"].to_numpy() - b["close"].to_numpy()) < 1e-9).all()
                and (a["time"].to_numpy() == b["time"].to_numpy()).all())
            _report("bridge_identique_a_mt5", same_values, f"forme {a.shape} vs {b.shape}")
            httpd.shutdown()
            direct.shutdown()
        except Exception as exc:
            _report("bridge_identique_a_mt5", False, f"{type(exc).__name__}: {exc}")

    db.close()
    failed = [c for c in CHECKS if not c[1]]
    for name, ok, detail in CHECKS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    print(json.dumps({"checks": len(CHECKS), "failed": len(failed)}, indent=2))
    return 1 if failed else 0
