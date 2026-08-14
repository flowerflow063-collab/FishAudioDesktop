@echo off
setlocal
cd /d "%~dp0"
echo ==========================================
echo   Fish Audio Desktop - Windows Builder
echo ==========================================
py -3 -m pip install --upgrade pip
py -3 -m pip install -r requirements.txt
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name FishAudioDesktop app.py
echo.
echo EXE creado en:
echo %CD%\dist\FishAudioDesktop.exe
pause
