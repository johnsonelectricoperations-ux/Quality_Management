@echo off
REM ============================================================
REM  QMS - annual process unit-cost recalculation (runs every April 1)
REM  Recalculates unit cost per process (forming/sintering/sizing/
REM  machining/etc.) from the last 3 months of intake results, and
REM  saves it to take effect from that year's April 1 only (past
REM  cost stays unchanged).
REM  (Comments kept in English only - Korean text in REM lines can be
REM  misread as commands when the shell's active code page does not
REM  match the file's encoding. 2026-08-21.)
REM ============================================================
cd /d "%~dp0\.."
call venv\Scripts\activate.bat
python -m app.price_calc
