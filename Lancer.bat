@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" goto lancer

echo Premier lancement : installation de Pillow et pystray dans .venv ...
where uv >nul 2>nul
if not errorlevel 1 (
  uv venv .venv --python 3.12 || goto erreur
  uv pip install --python .venv\Scripts\python.exe -r requirements.txt || goto erreur
  goto lancer
)
set "PY=py -3"
where py >nul 2>nul || set "PY=python"
%PY% -m venv .venv || goto erreur
.venv\Scripts\python.exe -m pip install -r requirements.txt || goto erreur

:lancer
start "" ".venv\Scripts\pythonw.exe" "%~dp0farm.py"
exit /b 0

:erreur
if exist ".venv" rmdir /s /q ".venv"
echo.
echo Installation impossible. Installe Python 3.10 ou plus recent depuis python.org
echo (coche "Add python.exe to PATH"), puis relance Lancer.bat.
pause
exit /b 1
