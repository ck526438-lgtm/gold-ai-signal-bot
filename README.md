# XAUUSD Bot — Python + MetaTrader 5

Bot de trading **XAUUSD** (or) modulaire, testé et documenté, **pour compte démo**, avec :

- 3 stratégies interchangeables via `config.yaml` : **mean-reversion** (RSI 7 + Bollinger, M1), **trend-following** (EMA 9/21 + EMA 100 + ATR, M5) et **ML XGBoost** (triple barrier, M5)
- gestion du risque stricte : 0,5 % par trade, SL = 1,5 × ATR, TP = 2 × SL, **kill-switch** (DD jour > 3 % ou 3 pertes consécutives), trailing optionnel
- exécution MT5 robuste (retcode, 3 essais, refus si slippage excessif, journal `trades.csv`)
- backtester réaliste (spread, commission, slippage, **zéro look-ahead**)
- **Walk-Forward Analysis** (6 mois / 2 mois / pas 2 mois) avec verdict GO / NO-GO
- **monitoring Telegram** (alertes + commandes `/status /pause /resume /report /stop`)
- **application Windows** (`XAUUSD-Bot.exe`) : tableau de bord, backtest, WFA, entraînement ML, configuration

> ## ⚠️ Avertissement sur les risques
> Le trading à haute fréquence sur l'or (XAUUSD) est **extrêmement risqué**. Le spread, la commission et le
> slippage suffisent à rendre perdantes la plupart des stratégies de scalping M1/M5. Les performances passées,
> les backtests et la walk-forward **ne garantissent pas les résultats futurs**. Ce logiciel est fourni à des fins
> éducatives, sans aucune garantie. N'engage jamais de l'argent que tu ne peux pas te permettre de perdre.
> **Le code refuse volontairement le trading réel** : seuls `mode: demo` et `mode: backtest` sont acceptés, et le
> bot vérifie que le compte MT5 connecté est bien un compte démo.

---

## Installation rapide

### Option A — application Windows (sans Python)
1. Télécharge `XAUUSD-Bot-windows.zip` dans l'onglet **Releases** (ou **Actions → Windows build → artefact**).
2. Dézippe dans un dossier (ex. `C:\XAUUSD-Bot`) et lance `XAUUSD-Bot.exe`.
3. `config.yaml` et `.env.example` sont à côté de l'exe : configure-les depuis l'onglet **Configuration**.

### Option B — depuis le code (Windows, Python 3.10+)
```powershell
git clone https://github.com/ck526438-lgtm/xauusd-bot.git
cd xauusd-bot
python -m venv .venv ; .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # puis remplis les valeurs
python -m pytest -q         # tous les tests doivent passer
python app\xauusd_app.py    # l'appli graphique
```
`MetaTrader5` ne fonctionne que sous **Windows** avec le terminal MT5 installé et connecté.

---

## Architecture

```
xauusd-bot/
├── config.yaml              # toute la configuration (aucune valeur codée en dur)
├── .env.example             # secrets : TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, MT5_PASSWORD
├── main.py                  # boucle live (DÉMO uniquement)
├── backtest.py              # backtest simple
├── walk_forward.py          # WFA (règles + --strategy ml_xgboost)
├── train_ml.py              # entraînement XGBoost
├── app/xauusd_app.py        # application Windows (Tkinter)
├── xauusd_bot.spec          # build PyInstaller → XAUUSD-Bot.exe
├── src/
│   ├── config.py            # chargement + validation (refus du mode réel, risque ≤ 2 %)
│   ├── mt5_connector.py     # connexion, vérification compte démo, reconnexion
│   ├── data_feed.py         # bougies CLÔTURÉES uniquement, resampling M1→M5
│   ├── indicators.py        # RSI, EMA, ATR, Bollinger (causaux)
│   ├── strategy.py          # BaseStrategy, MeanReversion, TrendFollowing
│   ├── filters.py           # spread, position, news, ATR min, session
│   ├── risk_manager.py      # sizing, SL/TP ATR, trailing, kill-switch
│   ├── execution.py         # order_send, retcode, retry, slippage, trades.csv
│   ├── backtester.py        # moteur événementiel avec coûts réalistes
│   ├── metrics.py           # winrate, PF, Sharpe, max DD
│   ├── wfa.py               # walk-forward + rapports
│   ├── telegram_notifier.py # alertes + commandes, rate-limit, retry, dry_run
│   ├── bot.py               # orchestration live
│   └── ml/                  # features, labeler, trainer, predictor, strategy_ml
├── tests/                   # 76 tests, dont test_no_lookahead.py et test_no_lookahead_ml.py
└── docs/                    # guides Telegram, ML et checklists
```

---

## Utilisation

| Action | Ligne de commande | Appli Windows |
|---|---|---|
| Backtest | `python backtest.py --strategy trend_following` | onglet **Backtest** |
| Walk-forward | `python walk_forward.py --strategy all` | onglet **Walk-Forward** |
| Entraîner le ML | `python train_ml.py` | onglet **Modèle ML** |
| Bot démo | `python main.py` | **Tableau de bord → Démarrer** |

Sans MT5 (ex. sous Linux), utilise un CSV M1 (`time,open,high,low,close,tick_volume,spread`) :
```bash
python tools/make_synthetic_data.py --months 14 --out data/synthetic_m1.csv   # données ALÉATOIRES, test uniquement
python walk_forward.py --strategy all --csv data/synthetic_m1.csv
```

### Rapports générés (`reports/`)
- `equity_<stratégie>.csv/.png`, `trades_backtest_<stratégie>.csv`
- `wfa/<stratégie>/wfa_results.csv`, `wfa_report.md`, `wfa_equity.png`
- `wfa/wfa_ml_comparison.md` (le ML bat-il les règles de plus de 0,3 de Sharpe OOS ?)

> **La Walk-Forward Analysis ne garantit pas la rentabilité future.** Elle montre seulement l'absence de
> sur-optimisation flagrante sur l'historique testé. Sur les données synthétiques fournies (marche aléatoire),
> toutes les stratégies sortent NO-GO — c'est le comportement attendu.

---

## Sécurité intégrée

- Pas de martingale, pas de grille, risque par trade plafonné à **2 %** (le code refuse au-delà)
- Le lot minimum n'est **jamais forcé** si cela dépasse le risque autorisé
- `mode` ≠ `demo`/`backtest` → refus ; compte MT5 réel → refus
- Kill-switch notifié sur Telegram, réarmé le lendemain (ou `/resume`)
- Token Telegram et mot de passe MT5 **masqués dans tous les logs** (`***`)
- Seul le `TELEGRAM_CHAT_ID` configuré peut piloter le bot ; `/stop` exige `/stop confirm`
- `/stop confirm` garde les positions ; seul `/stop confirm close` les ferme
- Modèle ML de plus de 90 jours : avertissement (démo) / refus (réel)

## Documentation
- [Guide de démarrage et checklist avant le réel](docs/GUIDE_DEMARRAGE.md)
- [Configuration Telegram](docs/GUIDE_TELEGRAM.md)
- [Guide ML : entraînement, lecture du comparatif, GO/NO-GO](docs/GUIDE_ML.md)

## Licence
MIT — voir `LICENSE`. Aucune garantie : utilisation à tes risques.
