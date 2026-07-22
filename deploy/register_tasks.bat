@echo off
REM ============================================================
REM  Windows 작업 스케줄러 등록 (관리자 권한으로 실행)
REM   - QMS_Server : 부팅 시 서버 자동 시작 (로그온 불필요)
REM   - QMS_Backup : 매일 02:00 DB 백업
REM  제거: 이 파일 하단의 unregister 참고
REM ============================================================
setlocal
set APPDIR=%~dp0

echo [1/2] 서버 자동시작 작업 등록 (부팅 시)...
schtasks /Create /TN "QMS_Server" /TR "\"%APPDIR%_service.bat\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 goto :err

echo [2/2] DB 백업 작업 등록 (매일 02:00)...
schtasks /Create /TN "QMS_Backup" /TR "\"%APPDIR%backup_db.bat\"" /SC DAILY /ST 02:00 /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 goto :err

echo.
echo 완료. 지금 바로 서버를 시작하려면:  schtasks /Run /TN "QMS_Server"
echo 상태 확인:  schtasks /Query /TN "QMS_Server"
echo.
echo [제거하려면]
echo   schtasks /Delete /TN "QMS_Server" /F
echo   schtasks /Delete /TN "QMS_Backup" /F
echo.
pause
exit /b 0

:err
echo.
echo [오류] 작업 등록 실패. '관리자 권한으로 실행'했는지 확인하세요.
pause
exit /b 1
