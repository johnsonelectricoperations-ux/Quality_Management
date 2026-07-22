@echo off
REM ============================================================
REM  통합품질관리시스템 - 최초 설치 (Windows)
REM  venv 생성 + 의존성 설치 + 빈 DB 초기화(기본 관리자 계정)
REM ============================================================
cd /d "%~dp0\.."

echo [1/4] 가상환경(venv) 생성...
python -m venv venv
if errorlevel 1 goto :err

echo [2/4] 의존성 설치...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 goto :err

echo [3/4] DB 초기화 (qms.db + 기본 계정 admin/editor/viewer)...
python -m app.db
if errorlevel 1 goto :err

echo [4/4] 완료.
echo.
echo  다음 단계:
echo   - 데모 데이터로 시작하려면:  python scripts\gen_sample.py  후  python -m app.seed
echo   - 실제 운영은 웹에서 마스터 Excel을 업로드하세요.
echo   - 관리자 비밀번호 변경:  python -m app.setpw admin ^<새비밀번호^>
echo   - 서버 실행:  deploy\run_server.bat
echo.
pause
exit /b 0

:err
echo.
echo [오류] 설치 중 문제가 발생했습니다. 위 메시지를 확인하세요.
pause
exit /b 1
