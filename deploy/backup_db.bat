@echo off
REM ============================================================
REM  통합품질관리시스템 - DB 백업 (backups\qms_날짜시간.db)
REM  Windows 작업 스케줄러로 매일 실행 권장
REM ============================================================
cd /d "%~dp0\.."
call venv\Scripts\activate.bat
python deploy\backup_db.py 30
