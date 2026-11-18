# PyInstaller — construit dist/XAUUSD-Bot.exe (Windows)
#   pyinstaller xauusd_bot.spec --noconfirm
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

datas = [("config.yaml", "."), (".env.example", "."), ("app/icon.ico", "app")]
binaries = []
hiddenimports = collect_submodules("src") + ["MetaTrader5", "sklearn.preprocessing", "matplotlib.backends.backend_tkagg"]
# xgboost : DLL + données + sous-modules, SANS xgboost.testing (qui exige pytest/hypothesis)
datas += collect_data_files("xgboost")
binaries += collect_dynamic_libs("xgboost")
hiddenimports += collect_submodules("xgboost", filter=lambda name: ".testing" not in name and ".dask" not in name and ".spark" not in name)

a = Analysis(
    ["app/xauusd_app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "hypothesis", "IPython", "notebook", "xgboost.testing", "xgboost.dask", "xgboost.spark"],
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
