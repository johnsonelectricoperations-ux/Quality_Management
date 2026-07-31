@echo off
REM ============================================================
REM  Register Windows Scheduled Tasks (RUN AS ADMINISTRATOR)
REM   - QMS_Backup      : daily DB backup at 02:00
REM   - QMS_DataScan    : daily source-folder scan at 02:10 (after backup)
REM   - QMS_PriceUpdate : yearly process-price recalculation on Apr 1, 03:00
REM
REM  서버 자동시작(QMS_Server)은 이 스크립트에 없다 — 매일 자동 재부팅 후 서버를
REM  띄우는 별도 통합 bat 파일에 deploy\_service.bat 호출을 포함시키는 방식으로
REM  대체한다(2026-07-31, 사용자 환경에 맞춰 변경). 그 통합 bat에서는 uvicorn이
REM  포그라운드로 계속 실행되니 반드시 START로 새 창/프로세스로 띄워야 한다:
REM    start "QMS" "C:\apps\qms\deploy\_service.bat"
REM  (경로는 실제 설치 위치에 맞게 바꿀 것)
REM
REM  (See DEPLOY.md for the Korean guide.)
REM ============================================================
setlocal
set APPDIR=%~dp0
set FAILED=0

echo [1/3] Registering daily DB backup (02:00)...
schtasks /Create /TN "QMS_Backup" /TR "\"%APPDIR%backup_db.bat\"" /SC DAILY /ST 02:00 /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 set FAILED=1

echo [2/3] Registering daily source-folder data scan (02:10)...
schtasks /Create /TN "QMS_DataScan" /TR "\"%APPDIR%scan_data.bat\"" /SC DAILY /ST 02:10 /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 set FAILED=1

echo [3/3] Registering yearly price update (Apr 1, 03:00)...
schtasks /Create /TN "QMS_PriceUpdate" /TR "\"%APPDIR%update_price.bat\"" /SC MONTHLY /M APR /D 1 /ST 03:00 /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 set FAILED=1

if "%FAILED%"=="1" goto err

echo.
echo Done. Check status:  schtasks /Query /TN "QMS_Backup"
echo                      schtasks /Query /TN "QMS_DataScan"
echo                      schtasks /Query /TN "QMS_PriceUpdate"
echo.
echo [To remove]
echo   schtasks /Delete /TN "QMS_Backup" /F
echo   schtasks /Delete /TN "QMS_DataScan" /F
echo   schtasks /Delete /TN "QMS_PriceUpdate" /F
echo.
pause
exit /b 0

:err
echo.
echo [ERROR] One or more tasks failed to register. Did you 'Run as administrator'?
echo         (Right-click this .bat file and choose "Run as administrator",
echo          or open Command Prompt as Administrator and run it from there.)
pause
exit /b 1
