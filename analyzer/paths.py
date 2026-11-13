"""Emplacements des fichiers : ressources de l'application et données de l'utilisateur.

- En développement (python main.py) : tout est dans le dossier du projet.
- Version installée (Gold-AI-Signal-Bot.exe) : l'application est en lecture seule dans
  Program Files, les données (config.yaml, .env, journal) vont dans
  %LOCALAPPDATA%\\Gold AI Signal Bot.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "Gold AI Signal Bot"
VERSION = "2.0.0"
FROZEN = bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def user_dir() -> Path:
    custom = os.getenv("GOLD_AI_HOME")
    if custom:
        return Path(custom)
    if FROZEN:
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    return Path.cwd()


def prepare_user_dir() -> Path:
    """Crée le dossier utilisateur et y copie les fichiers par défaut au premier lancement."""
    d = user_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name in ("config.yaml", ".env.example"):
        src, dst = resource_dir() / name, d / name
        if src.exists() and not dst.exists() and src.resolve() != dst.resolve():
            shutil.copyfile(src, dst)
    return d


def load_env(path: Path) -> None:
    """Charge un fichier .env simple (CLE=valeur) sans écraser l'environnement existant."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
