@echo off
REM ============================================================
REM  QMS - Run server (port 5003)
REM  Coexists with other servers via its own venv + port.
REM ============================================================
cd /d "%~dp0\.."
call venv\Scripts\activate.bat

REM Change 5003 below if you need a different port.
set QMS_PORT=5003

echo Starting QMS - http://localhost:%QMS_PORT%
python -m uvicorn app.main:app --host 0.0.0.0 --port %QMS_PORT% --workers 1

pause
