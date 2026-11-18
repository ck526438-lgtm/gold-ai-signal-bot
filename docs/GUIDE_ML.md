# Guide ML (XGBoost)

## Principe
- **Features** (18 + spread) calculées uniquement sur des bougies clôturées : retours 1/3/5/15, ATR normalisé,
  écart-type 20, RSI 7/14, ROC 10, ratios EMA 9/21 et 50/200, distance au VWAP de session, position dans le
  range 20, volume relatif, heure et jour encodés en sin/cos.
- **Labels triple barrier** : +1 si +1,5 ATR est atteint avant −1,5 ATR, −1 dans le cas inverse, 0 si rien en
  15 bougies ou si les deux barrières sont touchées dans la même bougie. Les labels qui se chevauchent sont
  pondérés par leur **unicité** (sinon le modèle « voit » plusieurs fois la même information).
- **Split temporel** train → validation → test, **purge** (on retire du train les labels qui débordent sur la
  validation) et **embargo** de 15 bougies. Scaler ajusté sur le train uniquement.
- En live : un trade seulement si la probabilité dépasse `ml.min_confidence` (0,60). Sinon **aucun trade**.
- Le ML génère **moins de trades** que les règles : c'est voulu (qualité > quantité).

## 1. Premier entraînement sur l'historique MT5
```powershell
python train_ml.py                       # utilise backtest.start/end et ml.train_end/val_end
```
Il faut au minimum ~2 ans de M1. Si les dates `train_end`/`val_end` ne tombent pas dans tes données, le script
bascule automatiquement sur un split chronologique 70/15/15.

Fichiers créés dans `src/models/` : `xgb_AAAAMMJJ.json`, `scaler_AAAAMMJJ.pkl`, `metadata.json`,
`feature_importance_AAAAMMJJ.csv`. Les logs affichent le top 10 des features, la matrice de confusion et
precision/recall des classes +1 et −1.

**Lecture rapide** : une precision(+1) ou (−1) au-dessus de ~0,40 sur le **test** est un début ; en dessous de
0,34 (hasard pour 3 classes), le modèle n'apporte rien.

## 2. Walk-forward ML et comparaison
```powershell
python walk_forward.py --strategy ml_xgboost
```
Le modèle est **réentraîné à chaque fenêtre** puis comparé à MeanReversion et TrendFollowing dans
`reports/wfa/wfa_ml_comparison.md` :

| Colonne | Signification |
|---|---|
| Sharpe OOS | Sharpe de la courbe out-of-sample concaténée (le chiffre qui compte) |
| Max DD OOS % | pire baisse de l'equity hors échantillon |
| Fenêtres OK | fenêtres où Sharpe OOS > 60 % du Sharpe IS |
| Verdict WFA | GO si ≥ 70 % des fenêtres sont OK |

## 3. Décider GO / NO-GO : ML ou règles ?
Le ML n'est retenu que si **les trois** conditions sont vraies :
1. son verdict WFA est **GO** ;
2. `Sharpe OOS ML > meilleur Sharpe OOS des règles + 0,3` (ligne « Le ML bat-il significativement… : OUI ») ;
3. son nombre de trades OOS est suffisant (≥ 100 sur l'ensemble des fenêtres) pour que le Sharpe ait un sens.

Sinon : garde la meilleure stratégie à règles qui est GO, ou ne trade pas.

## 4. Checklist de validation avant tout passage en réel avec le ML
1. ✅ `pytest` vert, en particulier `test_no_lookahead_ml.py` (features, perturbation du futur, scaler).
2. ✅ WFA ML **GO** sur ≥ 2 ans de vraies données broker, OOS cumulé positif.
3. ✅ Le ML bat les règles de plus de 0,3 de Sharpe OOS (comparatif « OUI »).
4. ✅ Les top features sont stables d'une fenêtre à l'autre (sinon le modèle apprend du bruit).
5. ✅ 3 mois de démo avec le **même** modèle, résultats cohérents avec l'OOS.
6. ✅ Modèle de moins de 90 jours (`metadata.json`), réentraînement planifié tous les 3 mois au minimum.
