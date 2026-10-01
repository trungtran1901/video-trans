@echo off
setlocal

if not exist .venv (
    python -m venv .venv
)
call .venv\Scripts\activate.bat

pip install -r requirements-desktop.txt

rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul

pyinstaller --log-level=DEBUG video_translator.spec

echo.
echo Build xong. File chay o: dist\VideoTranslator\VideoTranslator.exe
echo Nho copy them thu muc ffmpeg\bin (chua ffmpeg.exe, ffprobe.exe) vao ben trong dist\VideoTranslator\
echo.

endlocal