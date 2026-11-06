@echo off
REM Construit dist\Scolarix\Scolarix.exe puis, si Inno Setup 6 est installé, Output\Scolarix_Setup_x.y.z.exe (Windows 10/11, Python 3.11+ requis pour la construction)
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
if exist tools\SumatraPDF.exe copy /y tools\SumatraPDF.exe dist\Scolarix\tools\ >nul
if exist tools\PDFtoPrinter.exe copy /y tools\PDFtoPrinter.exe dist\Scolarix\tools\ >nul
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
    %ISCC% installer\scolarix.iss || goto :error
    echo Installateur : Output\
) else (
    echo Inno Setup 6 introuvable : installateur non construit ^(https://jrsoftware.org/isinfo.php^)
)
echo.
echo Termine : dist\Scolarix\Scolarix.exe
goto :eof
:error
echo La construction a echoue.
exit /b 1
