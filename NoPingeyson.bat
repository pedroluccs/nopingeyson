@echo off
rem Versao de desenvolvimento. Pede permissao de Administrador (necessaria para otimizar os jogos).
cd /d "%~dp0"
net session >nul 2>&1 || (powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" & exit /b)
python -m nopingeyson %*
