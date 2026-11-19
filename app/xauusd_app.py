"""XAUUSD Bot — application de bureau Windows (Tkinter).

Onglets : Tableau de bord (bot démo), Backtest, Walk-Forward, Modèle ML, Configuration.
Lancement : python app/xauusd_app.py   (ou XAUUSD-Bot.exe une fois compilé)
"""
from __future__ import annotations

import os
import queue
import shutil
import sys
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, scrolledtext, ttk  # noqa: E402

import yaml  # noqa: E402

from src import __version__  # noqa: E402
from src.config import ConfigError, base_dir, load_config, validate_config  # noqa: E402
from src.env import load_env  # noqa: E402
from src.logging_setup import register_secret, setup_logging  # noqa: E402
from src.state import StateStore  # noqa: E402

BG, PANEL, GOLD, TEXT, MUTED, RED, GREEN = "#0f1115", "#181b22", "#d4af37", "#e8e6e1", "#8b8f98", "#e05252", "#4caf7a"
STRATEGIES = ["trend_following", "mean_reversion", "ml_xgboost"]


def ensure_runtime_files() -> Path:
    """Dans le .exe : copie config.yaml / .env.example à côté de l'exécutable au premier lancement."""
    home = base_dir()
    bundle = Path(getattr(sys, "_MEIPASS", ROOT))
    for name in ("config.yaml", ".env.example"):
        dst = home / name
        src = bundle / name
        if not dst.exists() and src.exists():
            shutil.copy(src, dst)
    for d in ("logs", "reports", "data", "src/models"):
        (home / d).mkdir(parents=True, exist_ok=True)
    return home


class QueueSink:
    def __init__(self, q: "queue.Queue[str]"):
        self.q = q

    def write(self, message):
        self.q.put(str(message).rstrip("\n"))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.home = ensure_runtime_files()
        os.chdir(self.home)
        load_env()
        self.title(f"XAUUSD Bot {__version__} — DÉMO uniquement")
        self.geometry("1180x760")
        self.minsize(980, 640)
        self.configure(bg=BG)
        ico = Path(getattr(sys, "_MEIPASS", ROOT)) / "app" / "icon.ico"
        if sys.platform == "win32" and ico.exists():
            try:
                self.iconbitmap(default=str(ico))
            except tk.TclError:
                pass
        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.bot_thread: threading.Thread | None = None
        self.bot_stop = threading.Event()
        self.task_running = False
        self._style()
        self.cfg = self._load_cfg_safe()
        if self.cfg:
            setup_logging(self.cfg, QueueSink(self.log_q))
            register_secret(os.environ.get("TELEGRAM_TOKEN"), os.environ.get("MT5_PASSWORD"))
        self._build()
        self.after(200, self._drain_logs)
        self.after(1500, self._refresh_status)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================================================================ style
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL, font=("Segoe UI", 10))
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(16, 8))
        s.map("TNotebook.Tab", background=[("selected", BG)], foreground=[("selected", GOLD)])
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("Card.TLabel", background=PANEL, foreground=TEXT)
        s.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        s.configure("Title.TLabel", background=BG, foreground=GOLD, font=("Segoe UI Semibold", 18))
        s.configure("Big.TLabel", background=PANEL, foreground=GOLD, font=("Segoe UI Semibold", 15))
        s.configure("TButton", background=PANEL, foreground=TEXT, padding=(14, 7), borderwidth=1)
        s.map("TButton", background=[("active", "#232733"), ("disabled", BG)], foreground=[("disabled", MUTED)])
        s.configure("Gold.TButton", background=GOLD, foreground="#111")
        s.map("Gold.TButton", background=[("active", "#e6c55a"), ("disabled", "#5c5232")])
        s.configure("Red.TButton", background="#3a1f22", foreground="#ffb3b3")
        s.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=TEXT, arrowcolor=GOLD)
        s.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT)
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=24)
        s.configure("Treeview.Heading", background=BG, foreground=GOLD)

    def _load_cfg_safe(self):
        try:
            return load_config("config.yaml")
        except Exception as exc:
            if "--selftest" in sys.argv:
                raise
            messagebox.showerror("Configuration", f"config.yaml invalide :\n{exc}")
            return None

    # ================================================================ layout
    def _build(self):
        header = ttk.Frame(self)
        header.pack(fill="x", padx=18, pady=(14, 6))
        ttk.Label(header, text="XAUUSD BOT", style="Title.TLabel").pack(side="left")
        self.mode_lbl = ttk.Label(header, text="", foreground=MUTED)
        self.mode_lbl.pack(side="left", padx=16)
        ttk.Label(header, text="⚠️ Trading à haut risque — compte démo uniquement", foreground=RED).pack(side="right")

        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.nb = ttk.Notebook(paned)
        paned.add(self.nb, weight=3)
        self._tab_dashboard()
        self._tab_backtest()
        self._tab_wfa()
        self._tab_ml()
        self._tab_config()

        logf = ttk.Frame(paned)
        paned.add(logf, weight=1)
        ttk.Label(logf, text="Journal", foreground=GOLD).pack(anchor="w")
        self.log = scrolledtext.ScrolledText(logf, height=9, bg="#0a0c10", fg="#c9cbd1", insertbackground=TEXT,
                                             font=("Consolas", 9), relief="flat", borderwidth=0)
        self.log.pack(fill="both", expand=True)
        self._update_header()

    def _card(self, parent, title):
        f = ttk.Frame(parent, style="Card.TFrame", padding=14)
        ttk.Label(f, text=title, style="Muted.TLabel").pack(anchor="w")
        return f

    # ---------------------------------------------------------------- dashboard
    def _tab_dashboard(self):
        t = ttk.Frame(self.nb, padding=12)
        self.nb.add(t, text="Tableau de bord")
        cards = ttk.Frame(t)
        cards.pack(fill="x")
        self.kpi = {}
        for i, (key, title) in enumerate([("state", "État du bot"), ("strategy", "Stratégie"),
                                          ("risk", "Risque / trade"), ("telegram", "Telegram")]):
            c = self._card(cards, title)
            c.grid(row=0, column=i, sticky="nsew", padx=6, pady=6)
            cards.columnconfigure(i, weight=1)
            lbl = ttk.Label(c, text="—", style="Big.TLabel")
            lbl.pack(anchor="w", pady=(6, 0))
            self.kpi[key] = lbl

        ctl = self._card(t, "Contrôle (compte DÉMO MT5 — le terminal MetaTrader 5 doit être ouvert et connecté)")
        ctl.pack(fill="x", padx=6, pady=10)
        row = ttk.Frame(ctl, style="Card.TFrame")
        row.pack(anchor="w", pady=(10, 0))
        ttk.Label(row, text="Stratégie :", style="Card.TLabel").pack(side="left")
        self.dash_strategy = ttk.Combobox(row, values=STRATEGIES, state="readonly", width=18)
        self.dash_strategy.set((self.cfg or {}).get("strategy", {}).get("name", STRATEGIES[0]))
        self.dash_strategy.pack(side="left", padx=(6, 18))
        self.btn_start = ttk.Button(row, text="▶ Démarrer", style="Gold.TButton", command=self.start_bot)
        self.btn_start.pack(side="left", padx=4)
        ttk.Button(row, text="⏸ Pause", command=lambda: self._set_state(paused=True)).pack(side="left", padx=4)
        ttk.Button(row, text="⏵ Reprendre", command=lambda: self._set_state(paused=False, resume_requested=True)).pack(side="left", padx=4)
        self.btn_stop = ttk.Button(row, text="■ Arrêter", style="Red.TButton", command=self.stop_bot, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        ttk.Label(ctl, text="L'arrêt conserve les positions ouvertes (SL/TP restent actifs chez le broker).",
                  style="Muted.TLabel").pack(anchor="w", pady=(10, 0))

        info = self._card(t, "Rappels")
        info.pack(fill="both", expand=True, padx=6)
        for line in (
            "• Le bot refuse de trader si le compte MT5 n'est pas un compte DÉMO.",
            "• Kill-switch : arrêt automatique si drawdown jour > 3 % ou 3 pertes consécutives.",
            "• Avant le démo : backtest + walk-forward GO sur des données MT5 réelles.",
            "• Commandes Telegram : /status /pause /resume /report /stop (+ /stop confirm).",
        ):
            ttk.Label(info, text=line, style="Card.TLabel").pack(anchor="w", pady=2)

    # ---------------------------------------------------------------- backtest
    def _data_row(self, parent):
        row = ttk.Frame(parent, style="Card.TFrame")
        ttk.Label(row, text="Données :", style="Card.TLabel").pack(side="left")
        var = tk.StringVar(value=(self.cfg or {}).get("backtest", {}).get("data_csv", ""))
        ttk.Entry(row, textvariable=var, width=46).pack(side="left", padx=6)
        ttk.Button(row, text="CSV…", command=lambda: var.set(filedialog.askopenfilename(
            filetypes=[("CSV", "*.csv")]) or var.get())).pack(side="left")
        ttk.Label(row, text="  (vide = historique MT5)", style="Muted.TLabel").pack(side="left")
        return row, var

    def _tab_backtest(self):
        t = ttk.Frame(self.nb, padding=12)
        self.nb.add(t, text="Backtest")
        top = self._card(t, "Paramètres")
        top.pack(fill="x", padx=6)
        r1 = ttk.Frame(top, style="Card.TFrame")
        r1.pack(anchor="w", pady=(8, 4))
        ttk.Label(r1, text="Stratégie :", style="Card.TLabel").pack(side="left")
        self.bt_strategy = ttk.Combobox(r1, values=STRATEGIES, state="readonly", width=18)
        self.bt_strategy.set(self.dash_strategy.get())
        self.bt_strategy.pack(side="left", padx=6)
        bt = (self.cfg or {}).get("backtest", {})
        self.bt_start = tk.StringVar(value=bt.get("start", "2024-01-01"))
        self.bt_end = tk.StringVar(value=bt.get("end", "2025-12-31"))
        for lbl, var in (("Début :", self.bt_start), ("Fin :", self.bt_end)):
            ttk.Label(r1, text=lbl, style="Card.TLabel").pack(side="left", padx=(14, 4))
            ttk.Entry(r1, textvariable=var, width=12).pack(side="left")
        r2, self.bt_csv = self._data_row(top)
        r2.pack(anchor="w", pady=4)
        ttk.Button(top, text="Lancer le backtest", style="Gold.TButton", command=self.run_backtest).pack(anchor="w", pady=(8, 0))

        body = ttk.Frame(t)
        body.pack(fill="both", expand=True, pady=8)
        self.bt_tree = ttk.Treeview(body, columns=("v",), show="tree headings", height=10)
        self.bt_tree.heading("#0", text="Métrique")
        self.bt_tree.heading("v", text="Valeur")
        self.bt_tree.column("#0", width=170)
        self.bt_tree.column("v", width=130)
        self.bt_tree.pack(side="left", fill="y", padx=6)
        self.bt_plot = ttk.Frame(body, style="Card.TFrame")
        self.bt_plot.pack(side="left", fill="both", expand=True, padx=6)

    # ---------------------------------------------------------------- WFA
    def _tab_wfa(self):
        t = ttk.Frame(self.nb, padding=12)
        self.nb.add(t, text="Walk-Forward")
        top = self._card(t, "Walk-Forward Analysis — train 6 mois / test 2 mois / pas 2 mois")
        top.pack(fill="x", padx=6)
        r = ttk.Frame(top, style="Card.TFrame")
        r.pack(anchor="w", pady=(8, 4))
        ttk.Label(r, text="Stratégie :", style="Card.TLabel").pack(side="left")
        self.wfa_strategy = ttk.Combobox(r, values=STRATEGIES + ["all"], state="readonly", width=18)
        self.wfa_strategy.set(self.dash_strategy.get())
        self.wfa_strategy.pack(side="left", padx=6)
        ttk.Label(r, text="ml_xgboost = réentraînement par fenêtre + comparaison aux règles", style="Muted.TLabel").pack(side="left", padx=10)
        r2, self.wfa_csv = self._data_row(top)
        r2.pack(anchor="w", pady=4)
        b = ttk.Frame(top, style="Card.TFrame")
        b.pack(anchor="w", pady=(8, 0))
        ttk.Button(b, text="Lancer la WFA", style="Gold.TButton", command=self.run_wfa).pack(side="left")
        ttk.Button(b, text="Ouvrir le dossier des rapports", command=self.open_reports).pack(side="left", padx=8)
        self.wfa_out = scrolledtext.ScrolledText(t, bg=PANEL, fg=TEXT, font=("Consolas", 10), relief="flat")
        self.wfa_out.pack(fill="both", expand=True, padx=6, pady=8)

    # ---------------------------------------------------------------- ML
    def _tab_ml(self):
        t = ttk.Frame(self.nb, padding=12)
        self.nb.add(t, text="Modèle ML")
        top = self._card(t, "XGBoost — triple barrier, split temporel, purge + embargo")
        top.pack(fill="x", padx=6)
        r2, self.ml_csv = self._data_row(top)
        r2.pack(anchor="w", pady=(8, 4))
        b = ttk.Frame(top, style="Card.TFrame")
        b.pack(anchor="w", pady=(8, 0))
        ttk.Button(b, text="Entraîner le modèle", style="Gold.TButton", command=self.train_ml).pack(side="left")
        ttk.Button(b, text="Rafraîchir", command=self.show_model_info).pack(side="left", padx=8)
        self.ml_out = scrolledtext.ScrolledText(t, bg=PANEL, fg=TEXT, font=("Consolas", 10), relief="flat")
        self.ml_out.pack(fill="both", expand=True, padx=6, pady=8)
        self.after(500, self.show_model_info)

    # ---------------------------------------------------------------- config
    def _tab_config(self):
        t = ttk.Frame(self.nb, padding=12)
        self.nb.add(t, text="Configuration")
        sec = self._card(t, "Secrets (.env — jamais dans config.yaml ni sur GitHub)")
        sec.pack(fill="x", padx=6)
        self.env_vars = {}
        grid = ttk.Frame(sec, style="Card.TFrame")
        grid.pack(anchor="w", pady=(8, 0))
        for i, (key, label) in enumerate([("TELEGRAM_TOKEN", "Token Telegram"), ("TELEGRAM_CHAT_ID", "Chat ID"),
                                          ("MT5_LOGIN", "N° de compte MT5"),
                                          ("MT5_PASSWORD", "Mot de passe MT5 (optionnel)")]):
            ttk.Label(grid, text=label, style="Card.TLabel").grid(row=i, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=os.environ.get(key, ""))
            ttk.Entry(grid, textvariable=v, width=52, show="" if key in ("TELEGRAM_CHAT_ID", "MT5_LOGIN") else "•").grid(row=i, column=1, padx=8, pady=3)
            self.env_vars[key] = v
        bb = ttk.Frame(sec, style="Card.TFrame")
        bb.pack(anchor="w", pady=(8, 0))
        ttk.Button(bb, text="Enregistrer .env", command=self.save_env).pack(side="left")
        ttk.Button(bb, text="Tester Telegram", command=self.test_telegram).pack(side="left", padx=8)

        ed = self._card(t, "config.yaml")
        ed.pack(fill="both", expand=True, padx=6, pady=10)
        self.cfg_text = scrolledtext.ScrolledText(ed, bg="#0a0c10", fg=TEXT, insertbackground=TEXT,
                                                  font=("Consolas", 10), relief="flat", undo=True)
        self.cfg_text.pack(fill="both", expand=True, pady=(8, 6))
        p = self.home / "config.yaml"
        if p.exists():
            self.cfg_text.insert("1.0", p.read_text(encoding="utf-8"))
        ttk.Button(ed, text="Valider et enregistrer", style="Gold.TButton", command=self.save_config).pack(anchor="w")

    # ================================================================ actions
    def _update_header(self):
        if not self.cfg:
            return
        mon = self.cfg.get("monitoring", {})
        self.mode_lbl.config(text=f"mode {self.cfg['mode']} · {self.cfg['symbol']} · {self.home}")
        self.kpi["strategy"].config(text=self.dash_strategy.get() if hasattr(self, "dash_strategy") else self.cfg["strategy"]["name"])
        self.kpi["risk"].config(text=f"{self.cfg['risk']['risk_per_trade_pct']} %")
        self.kpi["telegram"].config(text="dry_run" if mon.get("dry_run", True) else ("actif" if os.environ.get("TELEGRAM_TOKEN") else "token manquant"))

    def _refresh_status(self):
        running = self.bot_thread is not None and self.bot_thread.is_alive()
        if self.cfg:
            st = StateStore(self.cfg["monitoring"].get("pause_file", "state.json")).read()
            txt = ("⏸ En pause" if st.get("paused") else "● En marche") if running else "○ Arrêté"
            self.kpi["state"].config(text=txt, foreground=GREEN if running and not st.get("paused") else GOLD)
        self.btn_start.config(state="disabled" if running else "normal")
        self.btn_stop.config(state="normal" if running else "disabled")
        self.after(1500, self._refresh_status)

    def _drain_logs(self):
        try:
            for _ in range(300):
                line = self.log_q.get_nowait()
                self.log.insert("end", line + "\n")
                self.log.see("end")
        except queue.Empty:
            pass
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 4000:
            self.log.delete("1.0", f"{lines - 3000}.0")
        self.after(200, self._drain_logs)

    def _set_state(self, **kw):
        if self.cfg:
            StateStore(self.cfg["monitoring"].get("pause_file", "state.json")).update(**kw)
            self.log_q.put(f"État mis à jour : {kw}")

    def _cfg_for(self, strategy=None, csv=None):
        cfg = load_config("config.yaml")
        if strategy and strategy != "all":
            cfg["strategy"]["name"] = strategy
        if csv is not None:
            cfg["backtest"]["data_csv"] = csv.strip()
        return cfg

    def _background(self, label, fn, done=None):
        if self.task_running:
            messagebox.showinfo("Occupé", "Une tâche est déjà en cours.")
            return
        self.task_running = True
        self.log_q.put(f"▶ {label}…")

        def worker():
            try:
                result = fn()
                if done:
                    self.after(0, lambda: done(result))
                self.log_q.put(f"✔ {label} terminé")
            except Exception as exc:
                tb = traceback.format_exc(limit=3)
                self.log_q.put(f"✖ {label} : {exc}\n{tb}")
                self.after(0, lambda: messagebox.showerror(label, str(exc)))
            finally:
                self.task_running = False

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------------- bot
    def start_bot(self):
        try:
            cfg = load_config("config.yaml")
            if cfg["mode"] != "demo":
                raise ConfigError("Mets « mode: demo » dans config.yaml pour lancer le bot.")
            self.cfg = cfg
        except Exception as exc:
            messagebox.showerror("Configuration", str(exc))
            return
        if not messagebox.askyesno("Démarrer", "Démarrer le bot sur le compte DÉMO MT5 ouvert ?"):
            return
        self.bot_stop.clear()

        def run():
            from src.bot import run_bot

            try:
                run_bot("config.yaml", stop_event=self.bot_stop, log_sink=QueueSink(self.log_q),
                        strategy_name=self.dash_strategy.get())
            except Exception as exc:
                self.log_q.put(f"✖ Bot arrêté : {exc}")
                self.after(0, lambda: messagebox.showerror("Bot", str(exc)))

        self.bot_thread = threading.Thread(target=run, daemon=True)
        self.bot_thread.start()
        self.kpi["strategy"].config(text=self.dash_strategy.get())

    def stop_bot(self):
        if messagebox.askyesno("Arrêter", "Arrêter le bot ? Les positions ouvertes sont conservées."):
            self.bot_stop.set()

    # ---------------------------------------------------------------- backtest
    def run_backtest(self):
        strat, csv = self.bt_strategy.get(), self.bt_csv.get()

        def job():
            from src.runner import backtest_strategy

            cfg = self._cfg_for(strat, csv)
            cfg["backtest"]["start"], cfg["backtest"]["end"] = self.bt_start.get(), self.bt_end.get()
            return backtest_strategy(cfg, strat)

        self._background(f"Backtest {strat}", job, self._show_backtest)

    def _show_backtest(self, res):
        self.bt_tree.delete(*self.bt_tree.get_children())
        labels = {"trades": "Trades", "winrate_pct": "Winrate %", "profit_factor": "Profit factor", "sharpe": "Sharpe",
                  "max_drawdown_pct": "Max drawdown %", "net_profit": "Profit net $", "return_pct": "Rendement %",
                  "avg_r": "R moyen", "final_equity": "Equity finale $"}
        for k, v in res["metrics"].items():
            self.bt_tree.insert("", "end", text=labels.get(k, k), values=(v,))
        for w in self.bt_plot.winfo_children():
            w.destroy()
        eq = res["equity"]
        if len(eq):
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure

            fig = Figure(figsize=(7, 3.6), dpi=100, facecolor=PANEL)
            ax = fig.add_subplot(111, facecolor=PANEL)
            ax.plot(eq.index, eq.values, color=GOLD, linewidth=1.2)
            ax.tick_params(colors=MUTED, labelsize=8)
            for s in ax.spines.values():
                s.set_color("#2a2e38")
            ax.grid(alpha=0.15)
            ax.set_title("Courbe d'equity", color=TEXT, fontsize=10)
            fig.tight_layout()
            FigureCanvasTkAgg(fig, master=self.bt_plot).get_tk_widget().pack(fill="both", expand=True)

    # ---------------------------------------------------------------- WFA
    def run_wfa(self):
        strat, csv = self.wfa_strategy.get(), self.wfa_csv.get()
        rules = ["mean_reversion", "trend_following"]
        strategies = {"ml_xgboost": ["ml_xgboost", *rules], "all": [*rules, "ml_xgboost"]}.get(strat, [strat])

        def job():
            from src.wfa import run_wfa_suite

            return run_wfa_suite(self._cfg_for(strat, csv), strategies, progress=self.log_q.put)

        self._background(f"Walk-forward {strat}", job, self._show_wfa)

    def _show_wfa(self, out):
        self.wfa_out.delete("1.0", "end")
        for name, files in out["files"].items():
            p = Path(files) if isinstance(files, str) else Path(files["md"])
            if p.exists():
                self.wfa_out.insert("end", p.read_text(encoding="utf-8") + "\n\n" + "─" * 80 + "\n\n")

    def open_reports(self):
        p = self.home / "reports"
        p.mkdir(exist_ok=True)
        if sys.platform == "win32":
            os.startfile(p)  # type: ignore[attr-defined]
        else:
            messagebox.showinfo("Rapports", str(p))

    # ---------------------------------------------------------------- ML
    def train_ml(self):
        csv = self.ml_csv.get()

        def job():
            from src.data_feed import load_history, resample
            from src.ml.trainer import train

            cfg = self._cfg_for(None, csv)
            return train(resample(load_history(cfg), cfg["ml"].get("timeframe", "M5")), cfg)

        self._background("Entraînement ML", job, lambda r: self.show_model_info())

    def show_model_info(self):
        self.ml_out.delete("1.0", "end")
        p = self.home / ((self.cfg or {}).get("ml", {}).get("models_dir", "src/models")) / "metadata.json"
        if not p.exists():
            self.ml_out.insert("end", "Aucun modèle entraîné pour l'instant.\n\nClique sur « Entraîner le modèle » "
                                      "(historique MT5 ou CSV M1).")
            return
        import json
        from datetime import datetime, timezone

        meta = json.loads(p.read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(meta["created_at"])).days
        lines = [f"Modèle : {meta['model_file']}  (âge : {age} jours{' ⚠️ > 90 j, réentraîne' if age > 90 else ''})",
                 f"Période : {meta['split']['data_start']} → {meta['split']['data_end']}",
                 f"Train jusqu'à {meta['split']['train_end']} | validation jusqu'à {meta['split']['val_end']}",
                 f"Features ({len(meta['features'])}) : {', '.join(meta['features'])}", ""]
        for split in ("validation", "test"):
            m = meta["metrics"].get(split, {})
            if m.get("n"):
                lines.append(f"[{split}] n={m['n']} accuracy={m['accuracy']:.3f}  "
                             f"P(+1)={m['precision_+1']:.3f} R(+1)={m['recall_+1']:.3f}  "
                             f"P(-1)={m['precision_-1']:.3f} R(-1)={m['recall_-1']:.3f}")
                lines.append(f"   matrice de confusion (vrai -1/0/+1 en lignes) : {m['confusion_matrix']}")
        self.ml_out.insert("end", "\n".join(lines))

    # ---------------------------------------------------------------- config
    def save_env(self):
        p = self.home / ".env"
        existing = {}
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()
        for k, var in self.env_vars.items():
            existing[k] = var.get().strip()
            os.environ[k] = var.get().strip()
        p.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n", encoding="utf-8")
        register_secret(os.environ.get("TELEGRAM_TOKEN"), os.environ.get("MT5_PASSWORD"))
        self._update_header()
        messagebox.showinfo(".env", "Secrets enregistrés dans .env (fichier local, ignoré par git).")

    def save_config(self):
        text = self.cfg_text.get("1.0", "end-1c")
        try:
            data = yaml.safe_load(text)
            validate_config(data)
        except (yaml.YAMLError, ConfigError, KeyError, TypeError) as exc:
            messagebox.showerror("config.yaml", f"Configuration refusée :\n{exc}")
            return
        (self.home / "config.yaml").write_text(text, encoding="utf-8")
        self.cfg = data
        self._update_header()
        messagebox.showinfo("config.yaml", "Configuration valide et enregistrée.")

    def test_telegram(self):
        def job():
            from src.telegram_notifier import TelegramNotifier

            cfg = load_config("config.yaml")
            n = TelegramNotifier(cfg)
            n.send("✅ Test XAUUSD Bot : la connexion Telegram fonctionne.", 0)
            return n.dry_run

        self._background("Test Telegram", job, lambda dry: messagebox.showinfo(
            "Telegram", "dry_run actif : message écrit dans le journal (mets dry_run: false pour envoyer)."
            if dry else "Message envoyé — vérifie Telegram."))

    def _on_close(self):
        if self.bot_thread and self.bot_thread.is_alive():
            if not messagebox.askyesno("Quitter", "Le bot tourne. Quitter l'arrêtera (positions conservées). Continuer ?"):
                return
            self.bot_stop.set()
            self.bot_thread.join(timeout=15)
        self.destroy()


def main():
    if "--selftest" in sys.argv:  # utilisé par la CI pour vérifier que l'exe démarre
        out = base_dir() / "selftest.txt"
        try:
            import src.wfa  # noqa: F401  (vérifie que les modules lourds sont bien embarqués)
            import src.ml.strategy_ml  # noqa: F401
            import xgboost  # noqa: F401
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: F401

            app = App()
            app.update()
            for i in range(len(app.nb.tabs())):
                app.nb.select(i)
                app.update()
            app.destroy()
            out.write_text("SELFTEST OK", encoding="utf-8")
            sys.exit(0)
        except SystemExit:
            raise
        except Exception:
            out.write_text("SELFTEST FAIL\n" + traceback.format_exc(), encoding="utf-8")
            sys.exit(1)
    App().mainloop()


if __name__ == "__main__":
    main()
