@echo off
REM ============================================================
REM  Register Windows Scheduled Tasks (RUN AS ADMINISTRATOR)
REM   - QMS_Server : start server on boot (no logon needed)
REM   - QMS_Backup : daily DB backup at 02:00
REM  (See DEPLOY.md for the Korean guide.)
REM ============================================================
setlocal
set APPDIR=%~dp0

echo [1/2] Registering server auto-start (on boot)...
schtasks /Create /TN "QMS_Server" /TR "\"%APPDIR%_service.bat\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 goto err

echo [2/2] Registering daily DB backup (02:00)...
schtasks /Create /TN "QMS_Backup" /TR "\"%APPDIR%backup_db.bat\"" /SC DAILY /ST 02:00 /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 goto err

echo.
echo Done. Start the server now:  schtasks /Run /TN "QMS_Server"
echo Check status:                schtasks /Query /TN "QMS_Server"
echo.
echo [To remove]
echo   schtasks /Delete /TN "QMS_Server" /F
echo   schtasks /Delete /TN "QMS_Backup" /F
echo.
pause
exit /b 0

:err
echo.
echo [ERROR] Task registration failed. Did you 'Run as administrator'?
pause
exit /b 1
