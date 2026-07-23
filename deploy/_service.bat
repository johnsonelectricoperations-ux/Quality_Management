@echo off
REM Server launcher for Task Scheduler / NSSM (no pause). Manual run: run_server.bat
cd /d "%~dp0\.."
call venv\Scripts\activate.bat
set QMS_PORT=5003
python -m uvicorn app.main:app --host 0.0.0.0 --port %QMS_PORT% --workers 1
