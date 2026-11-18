@echo off
rem Stops the backend started by start.bat (or by the background launcher).
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do (
  echo Stopping process %%p on port 8000
  taskkill /PID %%p /F >nul 2>&1
)
echo Done.
