@echo off
rem Starts the No-Parking backend + dashboard. Close the window (or run stop.bat) to stop it.
cd /d "%~dp0backend"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment missing. Run:  py -3.14 -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -r requirements.lock.txt
  pause & exit /b 1
)
echo.
echo  Dashboard on this laptop : http://localhost:8000/
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do echo  From other devices       : http://%%a:8000/   ^(same Wi-Fi^)
echo  Sign in: admin@local + the ADMIN_PASSWORD in backend\.env
echo  If the camera cannot reach this laptop, allow Python through Windows Firewall (Private/Public) for port 8000.
echo.
start "" http://localhost:8000/
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
