@echo off
setlocal
cd /d %~dp0

if not exist .venv (
  py -m venv .venv
)
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt

if not exist ffmpeg.exe (
  echo.
  echo Descarga ffmpeg.exe desde un build confiable y colocalo en esta carpeta.
  echo El build de GitHub Actions lo descarga automaticamente.
  echo.
  pause
  exit /b 1
)

pyinstaller --noconfirm --clean --windowed --name FlowRecorder --add-binary "ffmpeg.exe;." app.py

echo.
echo Build terminado: dist\FlowRecorder\FlowRecorder.exe
pause
