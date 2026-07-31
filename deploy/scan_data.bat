@echo off
REM ============================================================
REM  QMS - 소스 폴더 데이터 반영 (사내불량/생산량/외주소재/폐기불량)
REM  Recommended: run daily via Task Scheduler (새벽 2시 권장).
REM  DB 백업(QMS_Backup, 02:00)과 겹치지 않도록 이 작업은 02:10에 등록한다
REM  (register_tasks.bat 참고).
REM ============================================================
cd /d "%~dp0\.."
call venv\Scripts\activate.bat
python -m app.scan
