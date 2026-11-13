#!/usr/bin/env bash
# =============================================================================
#  MetaTrader 5 + pont Python sous Wine, pour Linux (appelé par install.sh --with-mt5)
#
#  1. installe Wine (paquets du système)
#  2. installe MetaTrader 5 (installateur officiel MetaQuotes) dans un préfixe Wine dédié
#  3. installe Python pour Windows + le module MetaTrader5 dans ce préfixe
#  4. crée la commande gold-ai-mt5 (terminal MT5 + pont) et configure l'application
#
#  Méthode : celle recommandée par MetaQuotes pour utiliser MetaTrader 5 sous Linux.
# =============================================================================
set -euo pipefail
PREFIX="$1"; APP_DIR="$2"; VENV="$3"; DATA="$4"; BIN_DIR="$5"

export WINEPREFIX="${WINEPREFIX:-$PREFIX/wine}"
export WINEDEBUG=-all WINEARCH=win64
PORT="${MT5_BRIDGE_PORT:-18822}"
PYVER="3.11.9"
MT5_URL="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
PY_URL="https://www.python.org/ftp/python/$PYVER/python-$PYVER-embed-amd64.zip"
CACHE="$PREFIX/downloads"

VERBOSE="${MT5_SETUP_VERBOSE:-0}"
Q="-q"; [ "$VERBOSE" = "1" ] && Q=""
log()  { [ "$VERBOSE" = "1" ] && printf '  [%s] %s\n' "$(date +%H:%M:%S)" "$*"; return 0; }
ok()   { printf '  \e[92m✓\e[0m %s\n' "$*"; }
warn() { printf '  \e[93m!\e[0m %s\n' "$*"; }
die()  { printf '\n\e[91m✗ %s\e[0m\n' "$*" >&2; exit 1; }
SUDO=""; [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"

# 1. Wine ------------------------------------------------------------------
if ! command -v wine >/dev/null 2>&1; then
  command -v apt-get >/dev/null 2>&1 || die "Installe Wine avec le gestionnaire de paquets de ta distribution, puis relance."
  warn "Installation de Wine (quelques minutes)…"
  $SUDO dpkg --add-architecture i386 || true
  $SUDO apt-get update -qq || true
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq wine wine64 >/dev/null \
    || DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq wine >/dev/null \
    || die "Installation de Wine impossible. Sur Kali : sudo apt install wine wine64"
fi
ok "$(wine --version 2>/dev/null || echo wine)"
for tool in wget winepath; do command -v "$tool" >/dev/null 2>&1 || { [ "$tool" = wget ] && $SUDO apt-get install -y -qq wget >/dev/null; }; done

mkdir -p "$WINEPREFIX" "$CACHE"
if [ ! -f "$WINEPREFIX/system.reg" ]; then
  warn "Préparation de l'environnement Windows (Wine)…"
  # sans Mono ni Gecko : évite les fenêtres « installer Wine Mono ? » qui bloquent le script
  log "wineboot"
  WINEDLLOVERRIDES="mscoree,mshtml=" timeout 300 wineboot -u >/dev/null 2>&1 </dev/null || true
  wineserver -w 2>/dev/null || true
fi
wine reg add 'HKEY_CURRENT_USER\Software\Wine' /v Version /t REG_SZ /d win10 /f >/dev/null 2>&1 || true
# pas de débogueur automatique : un plantage doit s'arrêter tout de suite, pas bloquer le script
wine reg add 'HKEY_LOCAL_MACHINE\Software\Microsoft\Windows NT\CurrentVersion\AeDebug' /v Debugger /t REG_SZ /d '' /f >/dev/null 2>&1 || true
wine reg add 'HKEY_LOCAL_MACHINE\Software\Microsoft\Windows NT\CurrentVersion\AeDebug' /v Auto /t REG_SZ /d 0 /f >/dev/null 2>&1 || true
ok "Préfixe Wine : $WINEPREFIX"

# 2. MetaTrader 5 ----------------------------------------------------------
TERMINAL="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"
if [ "${SKIP_MT5_TERMINAL:-0}" = "1" ]; then
  warn "Installation du terminal MetaTrader 5 ignorée (SKIP_MT5_TERMINAL=1)"
elif [ ! -f "$TERMINAL" ]; then
  wget -q --show-progress -O "$CACHE/mt5setup.exe" "$MT5_URL" || die "Téléchargement de MetaTrader 5 impossible."
  warn "Installation de MetaTrader 5 (une fenêtre peut s'ouvrir, laisse-la se terminer)…"
  timeout 900 wine "$CACHE/mt5setup.exe" /auto >/dev/null 2>&1 || true
  for _ in $(seq 1 120); do [ -f "$TERMINAL" ] && break; sleep 2; done
  [ -f "$TERMINAL" ] || die "MetaTrader 5 ne s'est pas installé (fichier absent : $TERMINAL)."
  # l'installateur lance le terminal : on le ferme, il sera relancé par gold-ai-mt5
  pkill -f terminal64.exe >/dev/null 2>&1 || true
fi
[ -f "$TERMINAL" ] && ok "MetaTrader 5 : $TERMINAL"

# 3. Python pour Windows + MetaTrader5 ------------------------------------
# Python « embarquable » (simple zip : l'installateur classique se bloque sous Wine) et
# paquets Windows téléchargés côté Linux puis décompressés (pip se bloque sous Wine).
# NumPy 1.26 : NumPy 2 appelle des fonctions de ucrtbase absentes de Wine 9 (plantage).
PYDIR="$WINEPREFIX/drive_c/Python311"
WINE_PY="$PYDIR/python.exe"
SITE="$PYDIR/Lib/site-packages"
if [ ! -f "$WINE_PY" ]; then
  warn "Installation de Python $PYVER pour Windows dans Wine…"
  wget -q -O "$CACHE/python-embed.zip" "$PY_URL" || die "Téléchargement de Python pour Windows impossible."
  mkdir -p "$PYDIR"
  python3 -m zipfile -e "$CACHE/python-embed.zip" "$PYDIR"
  PTH="$(find "$PYDIR" -maxdepth 1 -name 'python3*._pth' | head -n1)"
  printf 'python311.zip\n.\nLib\\site-packages\nimport site\n' > "$PTH"
fi
log "test de Python sous Wine"
WINEDLLOVERRIDES="mscoree,mshtml=" timeout 180 wine "$WINE_PY" -c "import sys; print(sys.version)" </dev/null \
  || die "Python pour Windows ne démarre pas sous Wine (mets Wine à jour : sudo apt install --only-upgrade wine)."
if ! WINEDLLOVERRIDES="mscoree,mshtml=" timeout 180 wine "$WINE_PY" -c "import MetaTrader5" </dev/null >/dev/null 2>&1; then
  log "téléchargement des paquets Windows MetaTrader5 + numpy"
  rm -rf "$CACHE/wheels" && mkdir -p "$CACHE/wheels" "$SITE"
  "$VENV/bin/python" -m pip download $Q --disable-pip-version-check --only-binary=:all: \
      --platform win_amd64 --python-version 3.11 --implementation cp --abi cp311 \
      -d "$CACHE/wheels" MetaTrader5 "numpy<2" || die "Téléchargement du module MetaTrader5 impossible."
  for whl in "$CACHE"/wheels/*.whl; do
    log "décompression $(basename "$whl")"
    python3 -m zipfile -e "$whl" "$SITE"
  done
fi
log "test du module MetaTrader5 sous Wine"
WINEDLLOVERRIDES="mscoree,mshtml=" timeout 180 wine "$WINE_PY" -c "import MetaTrader5 as m; print('MetaTrader5', m.__version__)" </dev/null \
  || die "Le module MetaTrader5 ne se charge pas dans Wine."
ok "Python $PYVER (Windows) + module MetaTrader5"

# 4. Pont + configuration --------------------------------------------------
TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
if [ -f "$PREFIX/mt5-bridge.env" ]; then
  OLD="$(sed -n 's/^MT5_BRIDGE_TOKEN=//p' "$PREFIX/mt5-bridge.env")"; [ -n "$OLD" ] && TOKEN="$OLD"
fi
cat > "$PREFIX/mt5-bridge.env" <<EOF
WINEPREFIX="$WINEPREFIX"
WINE_PY="$WINE_PY"
MT5_TERMINAL="$TERMINAL"
MT5_BRIDGE_PORT=$PORT
MT5_BRIDGE_TOKEN=$TOKEN
EOF
chmod 600 "$PREFIX/mt5-bridge.env"

cat > "$BIN_DIR/gold-ai-mt5" <<EOF
#!/usr/bin/env bash
# Lance MetaTrader 5 (Wine) et le pont pour Gold AI Signal Bot (généré par install.sh)
set -a; . "$PREFIX/mt5-bridge.env"; set +a
export WINEDEBUG=-all
if ! pgrep -f terminal64.exe >/dev/null 2>&1; then
  wine "\$MT5_TERMINAL" >/dev/null 2>&1 &
  sleep 5
fi
SERVER_WIN="\$(winepath -w "$APP_DIR/bridge/mt5_bridge_server.py" 2>/dev/null)"
mkdir -p "$DATA/logs"
LOG_WIN="\$(winepath -w "$DATA/logs/mt5-bridge-server.log" 2>/dev/null)"
echo "Pont MetaTrader 5 sur 127.0.0.1:\$MT5_BRIDGE_PORT (journal : $DATA/logs/mt5-bridge-server.log)"
# Sorties via un tube (| cat) : sous Wine, Python pour Windows refuse de démarrer si sa sortie
# est redirigée directement vers un fichier (« init_sys_streams: Invalid handle »).
WINEDLLOVERRIDES="mscoree,mshtml=" wine "\$WINE_PY" "\$SERVER_WIN" \
  --port "\$MT5_BRIDGE_PORT" --token "\$MT5_BRIDGE_TOKEN" --log "\$LOG_WIN" </dev/null 2>&1 | cat
EOF
chmod +x "$BIN_DIR/gold-ai-mt5"

cat > "$HOME/.local/share/applications/gold-ai-mt5.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=MetaTrader 5 (Gold AI Signal Bot)
Comment=Terminal MetaTrader 5 sous Wine et pont pour Gold AI Signal Bot
Exec=$BIN_DIR/gold-ai-mt5
Icon=$APP_DIR/packaging/icon.png
Terminal=true
Categories=Office;Finance;
EOF

# jeton dans le .env de l'application, adresse du pont dans config.yaml
touch "$DATA/.env"; chmod 600 "$DATA/.env"
grep -v '^MT5_BRIDGE_TOKEN=' "$DATA/.env" > "$DATA/.env.tmp" || true
echo "MT5_BRIDGE_TOKEN=$TOKEN" >> "$DATA/.env.tmp" && mv "$DATA/.env.tmp" "$DATA/.env"
GOLD_AI_HOME="$DATA" "$VENV/bin/python" - "$APP_DIR" "$PORT" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[1])
os.chdir(os.environ["GOLD_AI_HOME"])
from analyzer.paths import prepare_user_dir
prepare_user_dir()
from analyzer.config import load_config, save_config, update_config
cfg = update_config(load_config("config.yaml"), {"mt5_bridge": f"127.0.0.1:{sys.argv[2]}", "source": "auto"})
save_config(cfg, "config.yaml")
PY
ok "Pont configuré sur 127.0.0.1:$PORT (jeton privé dans $DATA/.env)"
echo
echo "  Étape suivante : lance « gold-ai-mt5 », connecte-toi à ton compte dans la fenêtre"
echo "  MetaTrader 5 (Fichier → Se connecter à un compte de trading), active « Algo Trading »,"
echo "  puis lance « gold-ai ». L'assistant détectera le compte et le symbole."
