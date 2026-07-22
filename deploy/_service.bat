@echo off
REM 작업 스케줄러/NSSM 전용 서버 실행 (pause 없음). 수동 실행은 run_server.bat 사용.
cd /d "%~dp0\.."
call venv\Scripts\activate.bat
set QMS_PORT=5003
python -m uvicorn app.main:app --host 0.0.0.0 --port %QMS_PORT% --workers 1
