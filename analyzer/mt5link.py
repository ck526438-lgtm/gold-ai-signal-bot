"""Connexion unique et persistante au terminal MetaTrader 5 (partagée données + trading).

Recherche du terminal, dans l'ordre :
  1. chemin choisi dans les réglages (`mt5_path`) ou variable MT5_PATH (.env) ;
  2. terminal déjà ouvert / dernier utilisé (initialize() sans chemin) ;
  3. installations trouvées automatiquement (Program Files, dossiers MetaQuotes).
Identifiants optionnels dans .env : MT5_LOGIN, MT5_PASSWORD, MT5_SERVER.
Sans identifiants, le compte déjà connecté dans le terminal est utilisé.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)


def find_terminals() -> list[str]:
    """Chemins de terminal64.exe installés sur ce PC (Windows), sans doublon."""
    found: list[Path] = []
    roots = [os.getenv("ProgramFiles"), os.getenv("ProgramFiles(x86)"), os.getenv("ProgramW6432"),
             os.path.join(os.getenv("LOCALAPPDATA", ""), "Programs")]
    for root in filter(None, roots):
        try:
            for d in Path(root).iterdir():
                exe = d / "terminal64.exe"
                if exe.is_file():
                    found.append(exe)
        except OSError:
            continue
    # chaque terminal lancé au moins une fois laisse son chemin dans origin.txt
    data = Path(os.getenv("APPDATA", "")) / "MetaQuotes" / "Terminal"
    if os.getenv("APPDATA") and data.is_dir():
        for origin in data.glob("*/origin.txt"):
            for enc in ("utf-16", "utf-8"):
                try:
                    exe = Path(origin.read_text(encoding=enc).strip().strip("﻿")) / "terminal64.exe"
                    if exe.is_file():
                        found.append(exe)
                    break
                except (OSError, UnicodeError):
                    continue
    seen, out = set(), []
    for p in found:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(str(p))
    return out


class MT5Link:
    def __init__(self, module=None):
        self._module = module
        self.lock = threading.RLock()
        self.connected = False
        self.preferred_path = ""
        self.path_in_use = ""
        self.bridge = ""            # "127.0.0.1:18822" : pont Linux/Wine (voir bridge/)

    @property
    def mt5(self):
        if self._module is None:
            if self.bridge:
                from .mt5bridge import BridgeMT5
                self._module = BridgeMT5(self.bridge)
            else:
                try:
                    import MetaTrader5 as mt5  # Windows uniquement
                except ImportError as e:
                    raise RuntimeError(
                        "MetaTrader 5 n'est pas disponible sur ce système. Sous Linux, installe le pont "
                        "MetaTrader 5 (./install.sh --with-mt5) ou utilise le Simulateur et Yahoo Finance."
                    ) from e
                self._module = mt5
        return self._module

    @property
    def available(self) -> bool:
        if self.bridge or self._module is not None:
            return True
        try:
            import MetaTrader5  # noqa: F401
            return True
        except ImportError:
            return False

    def set_bridge(self, address: str) -> None:
        with self.lock:
            if (address or "") != self.bridge:
                self.shutdown()
                self.bridge = address or ""
                self._module = None

    def set_path(self, path: str) -> None:
        with self.lock:
            if (path or "") != self.preferred_path:
                self.preferred_path = path or ""
                self.shutdown()

    def _credentials(self) -> dict:
        if not os.getenv("MT5_LOGIN"):
            return {}
        return {"login": int(os.environ["MT5_LOGIN"]), "password": os.getenv("MT5_PASSWORD", ""),
                "server": os.getenv("MT5_SERVER", "")}

    def ensure(self):
        """Renvoie le module MetaTrader5 connecté (se reconnecte si besoin)."""
        with self.lock:
            mt5 = self.mt5
            if self.connected and mt5.terminal_info() is not None:
                return mt5
            creds = self._credentials()
            explicit = self.preferred_path or os.getenv("MT5_PATH", "")
            attempts = [explicit] if explicit else [""] + ([] if self.bridge else find_terminals())
            last = None
            for path in attempts:
                kwargs = dict(creds, **({"path": path} if path else {}))
                if mt5.initialize(**kwargs):
                    info = mt5.terminal_info()
                    self.connected = True
                    self.path_in_use = path or (getattr(info, "path", "") if info else "")
                    log.info("Connecté à MetaTrader 5 (%s)", self.path_in_use or "terminal par défaut")
                    return mt5
                last = mt5.last_error()
            self.connected = False
            raise RuntimeError(f"Connexion à MetaTrader 5 impossible ({last}). "
                               "Vérifie que le terminal MT5 est installé, ouvert et connecté à ton compte.")

    def shutdown(self) -> None:
        with self.lock:
            if self.connected and self._module is not None:
                self._module.shutdown()
            self.connected = False


LINK = MT5Link()
