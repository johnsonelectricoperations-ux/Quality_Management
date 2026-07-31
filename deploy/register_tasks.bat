@echo off
REM ============================================================
REM  Register Windows Scheduled Tasks (RUN AS ADMINISTRATOR)
REM   - QMS_Server      : start server on boot (no logon needed)
REM   - QMS_Backup      : daily DB backup at 02:00
REM   - QMS_DataScan    : daily source-folder scan at 02:10 (after backup)
REM   - QMS_PriceUpdate : yearly process-price recalculation on Apr 1, 03:00
REM  (See DEPLOY.md for the Korean guide.)
REM ============================================================
setlocal
set APPDIR=%~dp0

echo [1/4] Registering server auto-start (on boot)...
schtasks /Create /TN "QMS_Server" /TR "\"%APPDIR%_service.bat\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 goto err

echo [2/4] Registering daily DB backup (02:00)...
schtasks /Create /TN "QMS_Backup" /TR "\"%APPDIR%backup_db.bat\"" /SC DAILY /ST 02:00 /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 goto err

echo [3/4] Registering daily source-folder data scan (02:10)...
schtasks /Create /TN "QMS_DataScan" /TR "\"%APPDIR%scan_data.bat\"" /SC DAILY /ST 02:10 /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 goto err

echo [4/4] Registering yearly price update (Apr 1, 03:00)...
schtasks /Create /TN "QMS_PriceUpdate" /TR "\"%APPDIR%update_price.bat\"" /SC MONTHLY /M APR /D 1 /ST 03:00 /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 goto err

echo.
echo Done. Start the server now:  schtasks /Run /TN "QMS_Server"
echo Check status:                schtasks /Query /TN "QMS_Server"
echo.
echo [To remove]
echo   schtasks /Delete /TN "QMS_Server" /F
echo   schtasks /Delete /TN "QMS_Backup" /F
echo   schtasks /Delete /TN "QMS_DataScan" /F
echo   schtasks /Delete /TN "QMS_PriceUpdate" /F
echo.
pause
exit /b 0

:err
echo.
echo [ERROR] Task registration failed. Did you 'Run as administrator'?
pause
exit /b 1
