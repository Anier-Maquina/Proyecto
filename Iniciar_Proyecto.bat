@echo off
cd /d "%~dp0"
start "Servidor de control horario" cmd /k python server.py serve --host 0.0.0.0
powershell -NoProfile -Command "$deadline = (Get-Date).AddSeconds(20); do { try { $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/index.html' -UseBasicParsing -TimeoutSec 1; if ($response.StatusCode -eq 200) { Start-Process 'http://127.0.0.1:8000/index.html?select=1'; exit 0 } } catch {}; Start-Sleep -Milliseconds 500 } while ((Get-Date) -lt $deadline); Write-Error 'El servidor no inició dentro del tiempo esperado.'; exit 1"
if errorlevel 1 pause