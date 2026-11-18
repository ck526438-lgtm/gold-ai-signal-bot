# Configuration du monitoring Telegram

## 1. Créer le bot avec @BotFather
1. Dans Telegram, ouvre **@BotFather** → `/newbot`.
2. Donne un nom (ex. `XAUUSD Monitor`) puis un identifiant finissant par `bot` (ex. `christ_xauusd_bot`).
3. BotFather renvoie un **token** du type `123456789:AAH...`. **Ne le partage jamais** et ne le mets jamais dans `config.yaml`.

## 2. Récupérer ton chat_id
1. Ouvre la conversation avec ton nouveau bot et envoie-lui `/start`.
2. Dans un navigateur, ouvre `https://api.telegram.org/bot<TON_TOKEN>/getUpdates`.
3. Repère `"chat":{"id":123456789,...}` → c'est ton **chat_id**.
   (Alternative : écris à **@userinfobot**, il affiche ton id.)

## 3. Renseigner `.env`
```
TELEGRAM_TOKEN=123456789:AAH...
TELEGRAM_CHAT_ID=123456789
```
Ou dans l'appli : **Configuration → Secrets → Enregistrer .env**. Le fichier `.env` est dans `.gitignore`.

## 4. Tester sans risque avec `dry_run`
1. Laisse `monitoring.dry_run: true` → les messages sont **écrits dans le journal** au lieu d'être envoyés.
2. Appli : **Configuration → Tester Telegram** → la ligne `[TELEGRAM dry_run] ✅ Test…` apparaît dans le journal.
3. Passe `dry_run: false`, re-clique **Tester Telegram** → tu reçois le message sur ton téléphone.

## 5. Ce que tu recevras
| Événement | Exemple |
|---|---|
| ✅ Entrée | `ENTRÉE BUY XAUUSD @ 2650.40 · SL 2647.10 · TP 2657.00 · 0.15 lot · raison` |
| 🎯 Sortie | `SORTIE BUY XAUUSD : +2.00R (+98.50$) en 34 min` |
| ⚠️ Kill-switch | raison (drawdown / pertes consécutives) + equity + DD du jour |
| 🔌 Incident | déconnexion MT5, retcode anormal |
| 📊 Rapport | chaque jour à 22h00 UTC : trades, winrate, PnL, drawdown, equity |

`monitoring.verbosity` : `minimal` (incidents + rapport), `normal` (+ entrées/sorties), `debug`.

## 6. Commandes (vérifiées toutes les 30 s, uniquement depuis ton chat_id)
- `/status` : positions, PnL flottant, equity, drawdown du jour
- `/pause` / `/resume` : suspend / réactive les nouvelles entrées (`/resume` réarme aussi le kill-switch)
- `/report` : rapport de la semaine
- `/stop` → demande confirmation, puis :
  - `/stop confirm` : arrête le bot, **garde** les positions (SL/TP restent chez le broker)
  - `/stop confirm close` : arrête le bot **et ferme** les positions

Limites intégrées : 1 message / 3 s maximum (file d'attente), 3 essais avec backoff exponentiel, token masqué dans tous les logs.
