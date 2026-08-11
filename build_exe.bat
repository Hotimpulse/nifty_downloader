@echo off
setlocal

REM Windows shortcut for the cross-platform native build command.
REM Run this from the project folder: yt_downloader\

where uv >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
  echo [ERROR] uv is not installed or is not available on PATH.
  echo Install it with:
  echo   winget install --id=astral-sh.uv -e
  exit /b 1
)

uv run --locked --group dev python build_app.py

if %ERRORLEVEL% NEQ 0 (
  echo.
  echo [ERROR] Build failed.
  exit /b %ERRORLEVEL%
)

echo.
echo Build complete!
echo EXE location: dist\yt_downloader.exe
echo.
echo Tip: copy dist\yt_downloader.exe anywhere and double-click it.
endlocal
