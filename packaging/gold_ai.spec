# -*- mode: python ; coding: utf-8 -*-
# PyInstaller : construit dist/Gold-AI-Signal-Bot/Gold-AI-Signal-Bot.exe
#   pyinstaller packaging/gold_ai.spec --noconfirm
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent

hidden = (
    collect_submodules("uvicorn")
    + collect_submodules("analyzer")
    + collect_submodules("sklearn")
    + ["MetaTrader5", "yfinance", "plyer.platforms.win.notification"]
)

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "web"), "web"),
        (str(ROOT / "config.yaml"), "."),
        (str(ROOT / ".env.example"), "."),
        (str(ROOT / "docs" / "Guide_Gold_AI_Signal_Bot.pdf"), "docs"),
    ],
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PySide6", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Gold-AI-Signal-Bot",
    icon=str(ROOT / "packaging" / "icon.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
    console=True,  # la fenêtre affiche les notifications (signaux, robot)
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Gold-AI-Signal-Bot", upx=False)
