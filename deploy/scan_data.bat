@echo off
REM ============================================================
REM  QMS - reflect source folder data (internal defect / production /
REM  outsourced material / scrap defect)
REM  Recommended: run daily via Task Scheduler (recommended 2 AM).
REM  Scheduled at 02:10 so it does not overlap with the 02:00 DB
REM  backup (QMS_Backup). See register_tasks.bat.
REM  (Comments kept in English only - Korean text in REM lines can be
REM  misread as commands when the shell's active code page does not
REM  match the file's encoding, e.g. under a different account's
REM  console session. 2026-08-21.)
REM ============================================================
cd /d "%~dp0\.."
call venv\Scripts\activate.bat
python -m app.scan
