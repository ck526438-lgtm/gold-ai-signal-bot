"""Data providers."""
from __future__ import annotations

from ..config import get
from .bars import normalize, sanity_report
from .csv_provider import CSVProvider
from .mt5_provider import MT5NotAvailable, MT5Provider


def make_provider(cfg: dict):
    """auto: MT5 si dispo (Windows), sinon bridge si configure, sinon CSV."""
    mode = str(get(cfg, "data.provider", "auto")).lower()
    if mode == "csv":
        return CSVProvider(cfg)
    if mode == "bridge":
        from .bridge import BridgeProvider

        return BridgeProvider(cfg)
    if mode == "mt5":
        return MT5Provider(cfg)
    if mode != "auto":
        raise ValueError(f"data.provider inconnu: {mode}")
    try:
        provider = MT5Provider(cfg)
        provider.connect()
        return provider
    except MT5NotAvailable as exc:
        url = str(get(cfg, "data.bridge.url", ""))
        if url and "127.0.0.1" not in url and "localhost" not in url:
            from .bridge import BridgeProvider

            print(f"MT5 indisponible ({exc}) -> bridge {url}")
            return BridgeProvider(cfg)
        print(f"MT5 indisponible ({exc}) -> CSV local")
        return CSVProvider(cfg)


__all__ = ["make_provider", "normalize", "sanity_report", "MT5Provider", "MT5NotAvailable",
           "CSVProvider", "MT5NotAvailable"]
