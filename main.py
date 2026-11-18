"""Point d'entrée live (compte DÉMO uniquement).

    python main.py                 # utilise config.yaml
    python main.py --config autre.yaml
"""
import argparse
import sys

from src.bot import run_bot
from src.config import ConfigError


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot XAUUSD — trading DÉMO")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()
    try:
        run_bot(args.config)
    except (ConfigError, RuntimeError, FileNotFoundError) as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
