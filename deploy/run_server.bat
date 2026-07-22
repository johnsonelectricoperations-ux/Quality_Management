@echo off
REM ============================================================
REM  통합품질관리시스템 - 서버 실행 (포트 5003)
REM  기존 서버 프로그램과 독립 venv/포트로 공존
REM ============================================================
cd /d "%~dp0\.."
call venv\Scripts\activate.bat

REM 포트 변경이 필요하면 아래 5003을 수정
set QMS_PORT=5003

echo 통합품질관리시스템 시작 - http://localhost:%QMS_PORT%
python -m uvicorn app.main:app --host 0.0.0.0 --port %QMS_PORT% --workers 1

REM (서버가 종료되면 창 유지)
pause
