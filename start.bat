@echo off
REM Lance l espace d analyse XAUUSD M5 (garde MetaTrader 5 ouvert et connecte)
cd /d "%~dp0"
if not exist .venv (
    echo Installation de l environnement, une seule fois...
    python -m venv .venv
    call .venv\Scripts\activate
    pip install -r requirements.txt MetaTrader5 plyer
) else (
    call .venv\Scripts\activate
)
python main.py %*
pause
