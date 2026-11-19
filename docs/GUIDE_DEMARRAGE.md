# Guide de démarrage

## 1. Préparer MetaTrader 5 (Windows)
1. Installe MetaTrader 5 depuis le site de ton broker et ouvre un **compte démo**.
2. Dans MT5 : *Outils → Options → Expert Advisors* → coche **Autoriser le trading algorithmique**.
3. Ajoute **XAUUSD** au Market Watch (le nom peut varier : `XAUUSD`, `XAUUSD.m`, `GOLD`… → mets le bon nom dans `symbol:`).
4. Télécharge l'historique : ouvre un graphique XAUUSD M1 et fais défiler vers la gauche
   (ou *Outils → Options → Graphiques → Nb max de barres* = illimité).

## 2. Configurer `config.yaml`
- `mt5.server` : déjà réglé sur **MetaQuotes-Demo**.
- Numéro de compte : à saisir dans l'appli (**Configuration → Secrets → N° de compte MT5**) ou dans `.env`
  (`MT5_LOGIN=...`). Il n'est pas dans `config.yaml` car le dépôt est public. Le bot refuse de trader si le
  terminal est connecté à un autre compte.
- Mot de passe : optionnel. Sans lui, le bot utilise la session déjà ouverte dans MT5 (le plus simple).
- `account.initial_capital` : capital du backtest.
- `strategy.name` : `trend_following`, `mean_reversion` ou `ml_xgboost`.
- `filters.news_events_utc` : **mets à jour** les dates NFP / CPI / FOMC depuis un calendrier économique.
- `filters.max_spread_points` : adapte au spread réel de ton broker (regarde-le aux heures de Londres/NY).

## 3. Valider hors ligne
```powershell
python -m pytest -q                                   # 76 tests verts
python backtest.py --strategy trend_following         # historique MT5
python walk_forward.py --strategy all                 # verdicts GO / NO-GO
```

## 4. Lancer en démo
- Appli : **Tableau de bord → Démarrer** (le terminal MT5 doit rester ouvert).
- Ligne de commande : `python main.py`.
- Laisse `monitoring.dry_run: true` tant que Telegram n'est pas configuré ([guide](GUIDE_TELEGRAM.md)).

## 5. Checklist — les 5 signaux verts avant d'envisager le réel

Le code bloque le réel. Si un jour tu modifies ce garde-fou, ne le fais **qu'après** avoir vu les 5 signaux :

1. ✅ **Tests** : `pytest` 100 % vert, y compris `test_no_lookahead.py` et `test_no_lookahead_ml.py`.
2. ✅ **WFA GO** sur au moins 2 ans de **vraies** données MT5 de ton broker, avec un OOS cumulé positif
   et un max drawdown OOS que tu peux supporter.
3. ✅ **Démo ≥ 3 mois / ≥ 100 trades** dont les résultats (winrate, R moyen, PF) restent proches du backtest
   (écart de Sharpe < 50 %).
4. ✅ **Kill-switch testé en vrai** : tu l'as vu se déclencher en démo et recevoir l'alerte Telegram ⚠️.
5. ✅ **Exploitation stable** : aucune erreur d'exécution non expliquée, reconnexion MT5 vérifiée,
   rapports quotidiens reçus chaque jour pendant 1 mois.

Même avec ces 5 signaux : commence avec le capital minimal, 0,25–0,5 % de risque, et réévalue chaque mois.
