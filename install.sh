#!/usr/bin/env bash
# =============================================================================
#  Gold AI Signal Bot — installation Linux (Kali, Debian, Ubuntu, Parrot, Mint)
#
#    ./install.sh                installe ou met à jour
#    ./install.sh --with-mt5     + MetaTrader 5 et son pont sous Wine (robot réel)
#    ./install.sh --service      + démarrage automatique à l'ouverture de session
#    ./install.sh --uninstall    désinstalle (garde tes données, sauf --purge)
#
#  Tout est installé dans ton dossier personnel, sans toucher au Python du système :
#    ~/.local/share/gold-ai-signal-bot/   application, environnement Python, données
#    ~/.local/bin/gold-ai               commande de lancement
# =============================================================================
set -euo pipefail

APP_NAME="Gold AI Signal Bot"
PREFIX="${GOLD_AI_PREFIX:-$HOME/.local/share/gold-ai-signal-bot}"
APP_DIR="$PREFIX/app"
VENV="$PREFIX/venv"
DATA="$PREFIX/data"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
UNIT_DIR="$HOME/.config/systemd/user"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WITH_MT5=0; SERVICE=0; UNINSTALL=0; PURGE=0; YES=0; NO_SELFTEST=0

if [ -t 1 ]; then
  B=$'\e[1m'; G=$'\e[92m'; Y=$'\e[93m'; R=$'\e[91m'; C=$'\e[96m'; N=$'\e[0m'
else
  B=""; G=""; Y=""; R=""; C=""; N=""
fi
step() { printf '\n%s==>%s %s%s%s\n' "$C" "$N" "$B" "$*" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n%s✗ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

usage() { sed -n '3,13p' "$0" | sed 's/^#  \{0,1\}//'; exit 0; }

for arg in "$@"; do
  case "$arg" in
    --with-mt5) WITH_MT5=1 ;;
    --service) SERVICE=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) UNINSTALL=1; PURGE=1 ;;
    -y|--yes) YES=1 ;;
    --no-selftest) NO_SELFTEST=1 ;;
    -h|--help) usage ;;
    *) die "Option inconnue : $arg (voir ./install.sh --help)" ;;
  esac
done

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
fi

apt_install() {
  command -v apt-get >/dev/null 2>&1 || die "Installe manuellement : $* (apt-get introuvable)."
  if [ "$(id -u)" -ne 0 ] && [ -z "$SUDO" ]; then die "sudo est nécessaire pour installer : $*"; fi
  warn "Installation des paquets système : $*"
  $SUDO apt-get update -qq || warn "apt-get update a échoué, on continue"
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq "$@" >/dev/null
}

confirm() {  # confirm "question" -> 0 si oui
  [ "$YES" -eq 1 ] && return 1
  read -r -p "  $1 [o/N] " r </dev/tty || return 1
  [[ "$r" =~ ^[oOyY]$ ]]
}

# -----------------------------------------------------------------------------
uninstall() {
  step "Désinstallation de $APP_NAME"
  if command -v systemctl >/dev/null 2>&1 && [ -f "$UNIT_DIR/gold-ai.service" ]; then
    systemctl --user disable --now gold-ai.service >/dev/null 2>&1 || true
    rm -f "$UNIT_DIR/gold-ai.service"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    ok "Service de démarrage automatique supprimé"
  fi
  rm -f "$BIN_DIR/gold-ai" "$BIN_DIR/gold-ai-mt5"
  rm -f "$DESKTOP_DIR/gold-ai-signal-bot.desktop" "$DESKTOP_DIR/gold-ai-mt5.desktop"
  rm -rf "$APP_DIR" "$VENV"
  ok "Application, commandes et raccourcis supprimés"
  if [ "$PURGE" -eq 1 ] || confirm "Supprimer aussi tes données (réglages, journal, historique du robot, MetaTrader 5 sous Wine) ?"; then
    rm -rf "$PREFIX"
    ok "Données supprimées"
  else
    ok "Données conservées dans $DATA"
  fi
  exit 0
}
[ "$UNINSTALL" -eq 1 ] && uninstall

# -----------------------------------------------------------------------------
printf '%s%s — installation Linux%s\n' "$B" "$APP_NAME" "$N"
[ -f "$SRC/main.py" ] && [ -d "$SRC/analyzer" ] || die "Lance ce script depuis le dossier du projet (là où se trouve main.py)."
if [ -r /etc/os-release ]; then . /etc/os-release; ok "Système : ${PRETTY_NAME:-Linux}"; fi

step "Python"
if ! command -v python3 >/dev/null 2>&1; then apt_install python3; fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10 ou plus est requis ($(python3 -V))."
ok "$(python3 -V)"
if [ ! -x "$VENV/bin/python" ]; then
  if ! python3 -m venv "$VENV" >/dev/null 2>&1; then
    rm -rf "$VENV"
    apt_install python3-venv python3-pip
    python3 -m venv "$VENV" || die "Impossible de créer l'environnement Python."
  fi
fi
ok "Environnement isolé : $VENV"

step "Application"
mkdir -p "$PREFIX" "$DATA" "$BIN_DIR" "$DESKTOP_DIR"
rm -rf "$APP_DIR.new"
mkdir -p "$APP_DIR.new"
( cd "$SRC" && tar --exclude=.git --exclude=data --exclude=logs --exclude=dist --exclude=build \
    --exclude=.venv --exclude=venv --exclude='__pycache__' --exclude=.pytest_cache -cf - . ) \
  | ( cd "$APP_DIR.new" && tar -xf - )
rm -rf "$APP_DIR" && mv "$APP_DIR.new" "$APP_DIR"
VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$APP_DIR/analyzer/paths.py")"
ok "Version $VERSION copiée dans $APP_DIR"

step "Dépendances Python (1 à 3 minutes la première fois)"
"$VENV/bin/python" -m pip install -q --disable-pip-version-check --upgrade pip
"$VENV/bin/python" -m pip install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"
ok "Dépendances installées"

step "Commande et raccourcis"
cat > "$BIN_DIR/gold-ai" <<EOF
#!/usr/bin/env bash
# $APP_NAME — lanceur (généré par install.sh)
export GOLD_AI_HOME="$DATA"
BRIDGE_ENV="$PREFIX/mt5-bridge.env"
if [ -f "\$BRIDGE_ENV" ] && [ "\${1:-start}" != "selftest" ] && [ -x "$BIN_DIR/gold-ai-mt5" ]; then
  . "\$BRIDGE_ENV"
  if ! (exec 3<>"/dev/tcp/127.0.0.1/\${MT5_BRIDGE_PORT}") 2>/dev/null; then
    echo "Démarrage de MetaTrader 5 et du pont (Wine)…"
    mkdir -p "$DATA/logs"
    nohup "$BIN_DIR/gold-ai-mt5" </dev/null >>"$DATA/logs/mt5-bridge.log" 2>&1 &
    for _ in \$(seq 1 60); do
      (exec 3<>"/dev/tcp/127.0.0.1/\${MT5_BRIDGE_PORT}") 2>/dev/null && break
      sleep 1
    done
  fi
fi
exec "$VENV/bin/python" "$APP_DIR/main.py" "\$@"
EOF
chmod +x "$BIN_DIR/gold-ai"
ok "Commande : gold-ai"

cat > "$DESKTOP_DIR/gold-ai-signal-bot.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
GenericName=Analyse et robot de l'or (XAUUSD)
Comment=Analyse XAUUSD M5 par chandeliers japonais et robot de trading
Exec=$BIN_DIR/gold-ai
Icon=$APP_DIR/packaging/icon.png
Terminal=true
Categories=Office;Finance;
Keywords=trading;or;gold;xauusd;metatrader;
Actions=watch;guide;

[Desktop Action watch]
Name=Mode terminal (sans navigateur)
Exec=$BIN_DIR/gold-ai watch

[Desktop Action guide]
Name=Guide d'utilisation
Exec=xdg-open $APP_DIR/docs/Guide_Gold_AI_Signal_Bot.pdf
EOF
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
ok "Raccourci ajouté au menu des applications"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
      if [ -f "$rc" ] || [ "$(basename "${SHELL:-}")" = "$(basename "$rc" | tr -d .rc)" ]; then
        grep -qs 'gold-ai (PATH)' "$rc" || printf '\n# gold-ai (PATH)\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
      fi
    done
    warn "$BIN_DIR ajouté au PATH : ouvre un nouveau terminal (ou lance : export PATH=\"\$HOME/.local/bin:\$PATH\")"
    ;;
esac

if [ "$WITH_MT5" -eq 1 ]; then
  step "MetaTrader 5 sous Wine + pont"
  bash "$APP_DIR/scripts/setup-mt5-wine.sh" "$PREFIX" "$APP_DIR" "$VENV" "$DATA" "$BIN_DIR"
fi

if [ "$SERVICE" -eq 1 ]; then
  step "Démarrage automatique"
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/gold-ai.service" <<EOF
[Unit]
Description=$APP_NAME
After=network-online.target

[Service]
ExecStart=$BIN_DIR/gold-ai start --no-browser --no-bell
Restart=on-failure
RestartSec=20
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now gold-ai.service >/dev/null
    ok "Service actif : interface sur http://localhost:8010 à chaque ouverture de session"
    ok "Journal : journalctl --user -u gold-ai -f"
  else
    warn "systemd (mode utilisateur) indisponible : démarrage automatique non configuré"
  fi
fi

if [ "$NO_SELFTEST" -eq 0 ]; then
  step "Vérification"
  "$BIN_DIR/gold-ai" selftest || die "La vérification a échoué (voir ci-dessus)."
fi

printf '\n%s%s✓ %s %s est installé.%s\n\n' "$G" "$B" "$APP_NAME" "$VERSION" "$N"
echo "  Lancer :            gold-ai            (ou menu Applications → $APP_NAME)"
echo "  Mode terminal :     gold-ai watch"
[ "$WITH_MT5" -eq 1 ] && echo "  MetaTrader 5 :      gold-ai-mt5        (lancé automatiquement par gold-ai)"
echo "  Mettre à jour :     git pull && ./install.sh"
echo "  Désinstaller :      ./install.sh --uninstall"
echo "  Données :           $DATA"
[ "$WITH_MT5" -eq 0 ] && echo "
  Sans MetaTrader 5, les prix viennent de Yahoo Finance et le robot fonctionne en Simulateur.
  Pour trader sur MT5 depuis Linux : ./install.sh --with-mt5"
exit 0
