@echo off
REM ──────────────────────────────────────────────
REM  PDF Studio — Windows Build Script
REM  Creates a standalone .exe via PyInstaller
REM ──────────────────────────────────────────────

echo.
echo ========================================
echo   PDF Studio — Build Script
echo ========================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.10+ and add to PATH.
    pause
    exit /b 1
)

REM Create venv if it doesn't exist
if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)

REM Activate venv
call .venv\Scripts\activate.bat

REM Install dependencies
echo Installing dependencies...
pip install -r requirements.txt
pip install pyinstaller

REM Optional: install OCR support
echo.
echo Installing OCR support (pytesseract)...
pip install pytesseract

REM Build
echo.
echo Building standalone executable...
pyinstaller pdf_studio.spec --clean

echo.
echo ========================================
if exist "dist\PDFStudio\PDFStudio.exe" (
    echo   BUILD SUCCESSFUL
    echo   Output: dist\PDFStudio\PDFStudio.exe
    echo.
    echo   To distribute, zip the entire
    echo   dist\PDFStudio\ folder.
) else (
    echo   BUILD FAILED — check errors above.
)
echo ========================================
echo.
pause
