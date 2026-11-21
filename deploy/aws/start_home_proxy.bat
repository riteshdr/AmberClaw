@echo off
REM Run this on your Windows PC. It starts a local proxy and exposes it to the
REM AWS server over an encrypted SSH reverse tunnel. Keep the window open.
cd /d "%~dp0"
start "AmberClaw home proxy" /min cmd /c "python ""%~dp0home_proxy.py"" || py -3 ""%~dp0home_proxy.py"""
timeout /t 2 >nul
ssh -i "C:\Users\rites\.ssh\amberclaw-key.pem" ^
  -R 0.0.0.0:8888:127.0.0.1:8888 ^
  -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes ^
  ubuntu@13.127.103.201
pause
