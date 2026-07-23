@echo off
REM ============================================================
REM  QMS - First-time setup (Windows)
REM  Create venv + install deps + init empty DB (default accounts)
REM  (See DEPLOY.md for the Korean guide.)
REM ============================================================
cd /d "%~dp0\.."

echo [1/4] Creating virtual environment (venv)...
python -m venv venv
if errorlevel 1 goto err

echo [2/4] Installing dependencies...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 goto err

echo [3/4] Initializing DB (qms.db + default accounts admin/editor/viewer)...
python -m app.db
if errorlevel 1 goto err

echo [4/4] Done.
echo.
echo  Next steps:
echo   - Demo data:      python scripts\gen_sample.py  then  python -m app.seed
echo   - Real operation: upload master Excel files from the web UI.
echo   - Change admin password:  python -m app.setpw admin NEWPASSWORD
echo   - Start server:   deploy\run_server.bat
echo.
pause
exit /b 0

:err
echo.
echo [ERROR] Setup failed. Please check the messages above.
pause
exit /b 1
