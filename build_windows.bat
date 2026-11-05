@echo off
REM Construit dist\FMSP_Archives\FMSP_Archives.exe (Windows 10/11, Python 3.11+ requis pour la construction)
setlocal
cd /d %~dp0
if not exist .venv (
    py -3 -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller || goto :error
python -m pytest -q tests || goto :error
pyinstaller --noconfirm fmsp_archives.spec || goto :error
if exist tools\SumatraPDF.exe copy /y tools\SumatraPDF.exe dist\FMSP_Archives\tools\ >nul
if exist tools\PDFtoPrinter.exe copy /y tools\PDFtoPrinter.exe dist\FMSP_Archives\tools\ >nul
echo.
echo Termine : dist\FMSP_Archives\FMSP_Archives.exe
goto :eof
:error
echo La construction a echoue.
exit /b 1
