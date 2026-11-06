@echo off
REM Installe le certificat de signature de la Cellule Informatique FMSP sur CE poste.
REM Après cela, Windows reconnaît Scolarix comme publié par « FMSP - Cellule Informatique (Scolarix) »
REM et n'affiche plus l'avertissement SmartScreen. À lancer une fois, en tant qu'administrateur
REM (clic droit > Exécuter en tant qu'administrateur). Pour tout un parc : voir LISEZMOI_SIGNATURE.txt (GPO).
net session >nul 2>&1
if errorlevel 1 (
  echo Ce script doit etre lance en tant qu'administrateur.
  pause
  exit /b 1
)
set CER=%~dp0FMSP_Cellule_Informatique.cer
certutil -addstore -f Root "%CER%" || goto :err
certutil -addstore -f TrustedPublisher "%CER%" || goto :err
echo.
echo Certificat installe : Scolarix est reconnu comme logiciel de confiance sur ce poste.
pause
exit /b 0
:err
echo Echec de l'installation du certificat.
pause
exit /b 1
