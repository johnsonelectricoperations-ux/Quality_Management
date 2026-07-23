@echo off
REM ============================================================
REM  QMS - DB backup (backups\qms_DATE_TIME.db)
REM  Recommended: run daily via Task Scheduler.
REM ============================================================
cd /d "%~dp0\.."
call venv\Scripts\activate.bat
python deploy\backup_db.py 30
