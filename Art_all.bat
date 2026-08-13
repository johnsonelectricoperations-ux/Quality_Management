@echo off

timeout /t 60 /nobreak > nul

echo [%date% %time%] Starting Server 1...
start "" "C:\PJT\Powder_blending_management\blending_server.bat"

timeout /t 5 /nobreak > nul

echo [%date% %time%] Starting Server 2...
start "" "C:\PJT\Scrap_management\scrap_server.bat"

timeout /t 5 /nobreak > nul

echo [%date% %time%] Starting Server 3...
start "" "C:\PJT\Incoming_inspection\incoming_server.bat"

timeout /t 5 /nobreak > nul

echo [%date% %time%] Starting Server 4...
start "" "C:\PJT\Quality_management_ver2\deploy\_service.bat"

echo [%date% %time%] All Servers Started.

exit