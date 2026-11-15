@echo off
setlocal
cd /d "%~dp0"
rem Construit dist\SteamHoursFarm\SteamHoursFarm.exe : une version qui n'a pas besoin de Python.
if exist ".venv-build\Scripts\python.exe" goto construire

echo Preparation : installation de PyInstaller, Pillow et pystray dans .venv-build ...
where uv >nul 2>nul
if not errorlevel 1 (
  uv venv .venv-build --python 3.12 || goto erreur
  uv pip install --python .venv-build\Scripts\python.exe -r requirements.txt pyinstaller || goto erreur
  goto construire
)
set "PY=py -3"
where py >nul 2>nul || set "PY=python"
%PY% -m venv .venv-build || goto erreur
.venv-build\Scripts\python.exe -m pip install -r requirements.txt pyinstaller || goto erreur

:construire
.venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --onedir --name SteamHoursFarm --icon icone.ico --add-data "logo.png;." --hidden-import pystray._win32 farm.py || goto erreur
rem Zip avec une date fixe : il ne garde ni l'heure ni le fuseau horaire de la construction.
.venv-build\Scripts\python.exe -c "import os, zipfile; z = zipfile.ZipFile(r'dist\SteamHoursFarm-windows.zip', 'w'); [z.writestr(zipfile.ZipInfo(os.path.relpath(os.path.join(d, f), 'dist').replace(os.sep, '/'), (2026, 1, 1, 0, 0, 0)), open(os.path.join(d, f), 'rb').read(), zipfile.ZIP_DEFLATED, 9) for d, _, fs in sorted(os.walk(r'dist\SteamHoursFarm')) for f in sorted(fs)]; z.close()" || goto erreur
echo.
echo Termine : dist\SteamHoursFarm\SteamHoursFarm.exe
echo Archive : dist\SteamHoursFarm-windows.zip
pause
exit /b 0

:erreur
echo.
echo La construction a echoue.
pause
exit /b 1
