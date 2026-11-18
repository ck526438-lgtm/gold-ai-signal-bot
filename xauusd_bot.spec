# PyInstaller — construit dist/XAUUSD-Bot.exe (Windows)
#   pyinstaller xauusd_bot.spec --noconfirm
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [("config.yaml", "."), (".env.example", "."), ("app/icon.ico", "app")]
binaries = []
hiddenimports = collect_submodules("src") + ["MetaTrader5", "sklearn.preprocessing", "matplotlib.backends.backend_tkagg"]
for pkg in ("xgboost",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["app/xauusd_app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "IPython", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="XAUUSD-Bot",
    console=False,
    icon="app/icon.ico",
    upx=False,
)
