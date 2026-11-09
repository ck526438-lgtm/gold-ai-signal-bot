# GOLD AI SIGNAL BOT — XAUUSD M5

Bot d'analyse de graphique pour l'or sur MetaTrader 5, en Python, qui émet des
signaux à chaque clôture de bougie M5. Deux moteurs, évalués sur les mêmes
bougies et les mêmes coûts :

| Moteur | Commande | Ce qu'il fait |
|---|---|---|
| **v1 — règles** | `signal`, `backtest --engine rules` | 6 familles pondérées (tendance, momentum, retour à la moyenne, volatilité, price action, session) → score 0-100 |
| **v2 — ML** | `train`, `backtest --engine ml` | HistGradientBoosting entraîné en walk-forward, prédit le franchissement d'un seuil en ATR sur 12 bougies |
| **consensus** | `backtest --engine both` | Le signal n'existe que si les deux moteurs sont d'accord sur le même sens |

---

## État des lieux chiffré (à lire avant de dépenser un euro)

Mesure sur **77 348 bougies M5 réelles** du serveur ForexClub demo, 5 octobre
2025 → 9 novembre 2026, avec spread 33 points + 2 slippages de 5 points, soit
**0,43 USD par aller-retour**, et un t-test sur la moyenne des R multiples.

```
engine  trades  per_day   win%   avg_R  t_stat  significatif  exp_net  exp_brut    PF   total_pts  maxDD
 rules    1948     5.68  38.40 -0.0281   -0.89         False   -0.154     0.276  0.975     -299.5  872.6
    ml    3210     9.36  41.78 -0.0357   -1.68         False   -0.179     0.251  0.964     -575.4 1178.6
  both      70     0.20  40.00  0.0195    0.12         False    1.904     2.334  1.364      133.3   57.6
```

`per_day` est rapporte au nombre de jours ou il existe des bougies (343 jours
de cotation), pas aux seuls jours ou le bot a trade : un reglage rare comme le
consensus fait 0,2 signal/jour, pas 1,2.

Ce que ça veut dire, sans tourner autour du pot :

- **Aucun des trois moteurs ne bat les coûts de façon significative.** Le
  meilleur t-stat est 0,12 pour 1,96 requis. En clair : sur ces données, l'edge
  mesurable est nul.
- Le ML est meme **légèrement négatif avec une confiance qui se tient**
  (t = -1,68, 3 210 trades) : sa direction vaut un peu moins que le hasard une
  fois le spread paye. Le signaler comme « moteur v2 » ne le rend pas meilleur.
- Les deux moteurs seuls rendent **négatif net** alors qu'ils sont **positifs
  bruts** : la direction est légèrement meilleure que le hasard, le spread la
  mange. C'est le problème central du M5 sur l'or.
- Le consensus est le seul positif (+1,9 pt/trade, drawdown divisé par 15),
  mais sur **70 trades** : c'est un indice, pas une preuve.
- Le ML a un vrai signal, très faible : AUC walk-forward **0,524**, précision
  directionnelle **52,6 %** contre 51,2 % de base, et calibration monotone
  (probabilité 0,41 → 0,55 de hausse réelle). Un AUC de 0,52 sur du 60 minutes
  est typique : prédire l'or à court terme est proche du bruit.

La grille `tune` (64 réglages, split in-sample/out-of-sample) montre aussi que
**miner les heures gagnantes ne tient pas** : le mode `learn_from_is` passe de
+1,54 pt en in-sample à -0,98 pt en out-of-sample (decay -2,5). C'est du curve
fitting, et le harness est là précisément pour le dénoncer.

### Le sens est-il vraiment informé ? (`compare`, temoin de direction)

Un backtest négatif ne dit pas si le moteur ne comprend rien au marché ou s'il
comprend mais paie trop de spread. `compare` répond en rejouant **le même
timing de signaux avec le sens tiré à pile ou face** (sortie sur retournement
coupée des deux côtés, sinon les deux variantes n'ont pas le même nombre de
trades) :

```
engine  n_reel  net_reel  t_reel  n_hasard  net_hasard  sd_hasard  apport_sens  net_tjs_buy  net_tjs_sell
 rules    1948    -0.158   -0.90      1997      -0.612      0.207        0.454       -0.649        -0.076
    ml    2876    -0.119   -1.07      2850      -0.353      0.200        0.234       -1.016         0.193
```

- `apport_sens` = ce que rapporte le **choix de la direction**, à nombre de
  trades comparable : **+0,45 pt/trade pour les règles**, +0,23 pour le ML.
  L'information directionnelle est donc **réelle** — mais elle vaut à peu de
  choses près le **0,43 USD de coût par aller-retour**. C'est toute l'histoire :
  le moteur a raison de justesse, et c'est le courtage qui gagne.
- Une direction fixe ne fait pas mieux (`net_tjs_buy` -0,65, `net_tjs_sell`
  -0,08) : le résultat ci-dessus n'est pas de la beta déguisée.
- Le témoin est trompeur pour `both` : au hasard les deux moteurs sont d'accord
  une fois sur deux, son `apport_sens` (+2,9) est sur-évalué.

### Élargir l'horizon : testé, ça aide, ça ne prouve rien

Sweep `risk.tp_atr` × `backtest.max_hold_bars` (moteur règles, split
2026-06-01, 343 jours de cotation) :

| tp/ATR | hold | trades OOS | net/trade IS | net/trade OOS | t OOS |
|---|---|---|---|---|---|
| 2,5 | 6 h | 803 | +0,29 | **-0,79** | -2,05 |
| 6,0 | 12 h | 583 | +1,33 | +0,03 | -0,15 |
| 12,0 | 24 h | 435 | +2,45 | **+0,74** | +0,66 |

Le mécanisme est celui attendu — 0,43 USD de coût fixe sur une cible 5 fois
plus large pèse 5 fois moins — et le gain vient aussi du **nombre de trades
divisé par deux** (le backtest est séquentiel : des sorties plus lointaines
consomment plus de bougies). Mais le t hors échantillon reste à 0,66 et le
profil devient méconnaissable : **18 % de trades gagnants**, 42 bougies de
détention moyenne, soit du swing avec une entrée en M5, plus de l'aide à la
décision sur M5.

**Les réglages livrés ne changent pas.** Passer `tp_atr` à 12 parce que ça
améliore le tableau que je viens de courir sur la fenêtre de test, c'est
l'exact contraire de ce que `tune` et ce témoin existent à dénoncer. C'est la
première piste à re-tester sur données fraîches, pas un défaut à éditer
aujourd'hui.

**Conséquence pratique :** ce bot est livré comme **assistant de signal + suivi
papier**, avec exécution automatique verrouillée (`trading.auto_execute: false`).
Ce n'est pas un robot à laisser déposité sur un compte réel.

---

## Installation — Windows (MT5 natif)

Le paquet `MetaTrader5` n'existe que sous Windows : c'est là que le terminal
tourne et que les données sont récupérées.

```bat
cd gold-ai-signal-bot
python -m pip install -r requirements.txt
copy config.example.yaml config.yaml
python bot.py doctor
python bot.py fetch --days 400
python bot.py check
```

`doctor` doit afficher `"connected": true`, le nom du symbole résolu et
`"broker_utc_offset_hours"` qui correspond à ton serveur (ici **2**).

## Installation — Kali

Le paquet Python `MetaTrader5` **ne s'installe pas** sous Linux (aucune roue
`manylinux`, et le module charge une `.dll` MetaQuotes). Le bot tourne donc
sur Kali en mode **bridge** : le PC Windows sert les données, Kali fait
l'analyse, les règles, le ML, le backtest et le live.

**1. Sur Windows** — terminal MT5 lancé et connecté, puis :

```bat
python bot.py bridge
```

Note l'adresse IP de la machine et le **token** affiché.

**2. Sur Kali** :

```bash
git clone <repo> gold-ai-signal-bot && cd gold-ai-signal-bot
python3 -m venv .venv && source .venv/bin/activate
pip install numpy pandas scikit-learn PyYAML requests   # pas de MetaTrader5
cp config.example.yaml config.yaml
```

Puis dans `config.yaml` :

```yaml
data:
  provider: bridge
  bridge:
    url: http://IP_WINDOWS:8765
```

et dans `.env` à la racine (`cp .env.example .env`, puis coller la valeur) :

```
GOLDBOT_BRIDGE_TOKEN=le_token_affiche
```

**3. Vérifier depuis Kali** :

```bash
python bot.py doctor      # doit voir le serveur broker via le bridge
python bot.py fetch --days 400
python bot.py selftest --skip-bridge
```

Le bridge n'expose que des données de marché : pas d'ordres, pas de compte
modifiable, et l'authentification est une comparaison constante de temps.
Ne l'expose jamais sur Internet — réseau local uniquement.

---

## Commandes

| Commande | Rôle |
|---|---|
| `doctor` | Terminal MT5, symbole résolu, tick courant, offset UTC, cache |
| `fetch --days N` | Historique M5 par morceaux → `data/cache/XAUUSD_M5.csv.gz` |
| `check` | Intégrité des bougies **+ test de look-ahead** sur les features |
| `signal` | Verdict sur la dernière bougie clôturée, répartition des blocages |
| `backtest` | Simulation complète avec coûts, t-stat, découpages |
| `compare` | rules / ml / both sur les mêmes bougies **+ témoin direction au hasard** |
| `train` | Walk-forward ML, AUC par fold, calibration, sauvegarde du modèle |
| `tune` | Grille de réglages avec split in-sample/out-of-sample |
| `live` | Boucle temps réel sur chaque clôture M5 |
| `paper` | Bilan du suivi papier journalisé |
| `selftest` | Vérifie que live, backtest et bridge calculent la même chose |
| `bridge` | Expose MT5 au réseau local (à lancer sur Windows) |

Exemples :

```bash
python bot.py backtest --engine both --start 2026-06-01 --export data/trades_both.csv
python bot.py signal --live --history 10
python bot.py live            #Ctrl+C pour arrêter
python bot.py live --once     # pour un cron toutes les 5 minutes
```

**Comment le suivi papier est rempli.** Un signal détecté sur la clôture de la
bougie i n'est pas exécuté au prix de cette clôture : il reste `pending` et se
remplit a l ouverture de la bougie i+1, exactement comme dans le backtest. SL et
TP sont calcules autour de ce prix d entree, le SL est prioritaire sur le TP
quand les deux sont touches dans la meme bougie, un retournement du moteur ferme
la position, et le plafond de `backtest.max_hold_bars` se compte depuis le
signal. Le cout est le meme des deux cotes (`backtest.spread_points` + 2 ×
`slippage_points`). `selftest` rejoue 12 signaux par moteur par le chemin live
et verifie que les points papier tombent a moins de 0,05 USD du backtest : sans
cette contrainte, le papier paraitrait toujours plus beau que la simulation.

---

## Configuration : les quatre réglages qui comptent

**1. Le nom du symbole.** Broker « other » = nom différent. Le bot teste la
liste `symbol.candidates` dans l'ordre et prend le premier tradable. Trouve le
tien dans MT5 : `Affichage > Liste des symboles`.

**2. Le point et la taille de contrat** (`broker.point`, `broker.contract_size`).
Ouvre un graphique XAUUSD, `F1`/Details, « Specification du contrat ». Tout le
modèle de coût et le dimensionnement en dépendent : faux ici = faux partout.

**3. Le décalage horaire du serveur** (`mt5.broker_utc_offset_hours`). Les
horodatages MT5 sont en **heure serveur**, pas en UTC. Mesuré ici : UTC+2,
vérifié à la seconde près contre un en-tête `Date` HTTP. Le bot le mesure au
démarrage et convertit tout en UTC. Sans cette correction, le VWAP ancré, la
fenêtre Londres/New York et le filtre de pré-clôture jouent à l'heure à
côté. Si la mesure échoue (marché fermé le week-end), le bot rejoue la valeur
mémorisée dans `data/cache/broker_offset.json`.

**4. La coupure quotidienne.** Ce broker ne cote **pas entre 21:00 et 23:00
UTC** (vérifié : heure 22 = 0 bougie). `market.daily_close_utc_hour` pilote le
filtre `pre_break`, qui interdit un signal dans les 15 minutes précédant :
sur l'or, ces bougies-là se payent en gap à la réouverture.

Autres clés : `signal.min_score` (68 — à 55 le bot signalait 62 % des bougies,
du pur bruit), `signal.min_agreeing_components` (4/6),
`signal.require_directional_backbone` (tendance **ou** volatilité doit suivre,
sinon les composants de repli produisent un signal en permanence),
`signal.cooldown_bars` (un signal est un **changement d'état**, pas un état :
sans ça le même mouvement émet 30 signaux identiques).

---

## Pour chercher un edge réel

Dans cet ordre, parce que c'est celui qui coûte le moins cher :

1. **Réduire le nombre de trades avant d'augmenter la précision.** 1 à
   3 signaux/jour de qualité valent mieux que 14 : le coût est fixe par trade.
2. **Élargir l'horizon.** Fait et mesuré plus haut : `tp_atr` 2,5 → 12 ramène
   l'espérance nette hors échantillon de -0,79 à +0,74 pt/trade, sans jamais
   atteindre la significativité, et en changeant la nature du bot. À re-tester
   sur des données qui n'ont pas encore servi, pas à acter en l'état.
3. **Le seul gap qui paie peut-être déjà :** le sens est informé de +0,45
   pt/trade et le coût est de 0,43 USD. Un broker à 15 points de spread médian
   au lieu de 33 ferait basculer ce chiffre en net positif sans toucher une
   ligne de code. C'est l'item à vérifier en premier avec `doctor`.
4. **Contexte en M15/H1** en plus du M5 : les features actuelles sont toutes
   calcules sur la meme echelle de temps.
5. **Un meilleur spread.** Voir item 3 — 33 points sur ce compte demo, et des
   pointes à 504.
6. **Filtre de news.** Les pointes de spread à 504 points sont dans les données :
   un filtre calendrier (CPI, NFP, FOMC) est probablement plus rentable qu'un
   indicateur de plus.
7. **Beaucoup plus d'historique** avant de conclure quoi que ce soit sur le
   consensus : 70 trades, c'est 15 % du minimum pour un t-stat exploitable.

---

## Avertissements

- Les signaux sont fournis à titre d'aide à la décision. Le backtest ci-dessus
  ne démontre pas de rentabilité : il démontre l'inverse, en l'état.
- Le trading sur marge peut faire perdre plus que le dépôt. Un compte demo
  d'abord, puis du papier (`live` fait les deux), puis très petit si un jour
  les statistiques le justifient.
- `trading.auto_execute` reste à `false` tant que `selftest` n'est pas à 0 échec
  et que `compare` n'a pas un t-stat supérieur à 1,96 sur un échantillon
  d'au moins 500 trades.
