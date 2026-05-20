@echo off
taskkill /f /im pythonw.exe >nul 2>&1
taskkill /f /im python.exe /fi "WINDOWTITLE eq whowatch*" >nul 2>&1
echo stopped
