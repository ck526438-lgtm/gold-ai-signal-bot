"""Mode autonome : l'application tourne seule, sans que tu l'ouvres.

* démarrage automatique à l'ouverture de session (Windows : dossier Démarrage ;
  Linux : ~/.config/autostart), sans ouvrir le navigateur ;
* robot activé automatiquement au lancement (`start_enabled`) ;
* l'ordinateur ne se met pas en veille tant que l'application tourne (Windows) ;
* surveillance : alerte si les données MT5 n'arrivent plus, alerte au retour, bilan du jour.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

from .paths import FROZEN

log = logging.getLogger(__name__)
APP = "Gold AI Signal Bot"


def launch_command() -> list[str]:
    """Commande qui relance l'application sans navigateur."""
    if FROZEN:
        return [sys.executable, "--no-browser"]
    launcher = shutil.which("gold-ai")
    if launcher:
        return [launcher, "--no-browser"]
    main = Path(__file__).resolve().parent.parent / "main.py"
    return [sys.executable, str(main), "start", "--no-browser"]


def _startup_file() -> Path | None:
    if os.name == "nt":
        appdata = os.getenv("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup" / "Gold AI Signal Bot (autonome).vbs"
    if sys.platform.startswith("linux"):
        base = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
        return base / "autostart" / "gold-ai-autonome.desktop"
    return None


def is_autostart() -> bool:
    f = _startup_file()
    return bool(f and f.exists())


def set_autostart(enabled: bool) -> str:
    """Crée ou supprime le lancement automatique. Renvoie une description lisible."""
    f = _startup_file()
    if f is None:
        return "démarrage automatique non pris en charge sur ce système"
    if not enabled:
        f.unlink(missing_ok=True)
        return "démarrage automatique retiré"
    f.parent.mkdir(parents=True, exist_ok=True)
    cmd = launch_command()
    if os.name == "nt":
        # .vbs : lance sans fenêtre de console
        line = " ".join(f'""{c}""' if " " in c or "\\" in c else c for c in cmd)
        f.write_text(f'CreateObject("WScript.Shell").Run "{line}", 0, False\r\n', encoding="utf-8")
    else:
        exec_line = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        f.write_text("[Desktop Entry]\nType=Application\nName=Gold AI Signal Bot (autonome)\n"
                     f"Exec={exec_line}\nX-GNOME-Autostart-enabled=true\nNoDisplay=true\n", encoding="utf-8")
    return f"démarre avec la session ({f.name})"


def keep_awake(enabled: bool) -> bool:
    """Empêche la mise en veille de Windows tant que l'application tourne."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception as e:  # noqa: BLE001
        log.info("Anti-veille indisponible : %s", e)
        return False
