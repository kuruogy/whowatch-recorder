@echo off
cd /d "%~dp0"
pip install flask flask-socketio requests websockets streamlink --quiet >nul 2>&1
python "%~dp0app.py"
