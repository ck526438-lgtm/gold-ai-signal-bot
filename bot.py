#!/usr/bin/env python3
"""GOLD AI SIGNAL BOT - point d'entree."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from goldbot.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
