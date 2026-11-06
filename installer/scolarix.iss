; Installateur Windows de Scolarix (Inno Setup 6) — construit par GitHub Actions :
;   iscc /DAppVersion=1.1.0 installer\scolarix.iss
; Produit : Output\Scolarix_Setup_<version>.exe
; Installe dans Program Files, crée les raccourcis (menu Démarrer, bureau en option) et un
; désinstallateur (Paramètres > Applications). Les données (index, cache, configuration) restent
; dans %APPDATA%\FMSP_Archives et ne sont PAS supprimées à la désinstallation.

#ifndef AppVersion
  #define AppVersion "1.2.0"
#endif
#define AppName "Scolarix"
#define AppExe "Scolarix.exe"

[Setup]
#ifdef SIGN
; Signature de l'installateur ET du désinstallateur (outil « signtool » défini sur la ligne de commande /S)
SignTool=signtool
SignedUninstaller=yes
#endif
AppId={{6F1C2B7E-5A2D-4E8B-9C41-3B7D8E2F9A10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Cellule Informatique FMSP - Université de Douala
AppComments=Archives des relevés de notes : recherche, impression, duplicata, cursus, vérification
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\Output
OutputBaseFilename=Scolarix_Setup_{#AppVersion}
SetupIconFile=..\assets\scolarix.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} — Archives des relevés
WizardStyle=modern
WizardImageFile=..\assets\installer_side.bmp
WizardSmallImageFile=..\assets\installer_small.bmp
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le bureau"; GroupDescription: "Raccourcis :"

[Files]
Source: "..\dist\Scolarix\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; DestName: "LISEZMOI.md"; Flags: ignoreversion
Source: "certificat\*"; DestDir: "{app}\certificat"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Archives des relevés de notes"
Name: "{group}\Lisez-moi"; Filename: "{app}\LISEZMOI.md"
Name: "{group}\Certificat de signature (administrateurs)"; Filename: "{app}\certificat"
Name: "{group}\Désinstaller {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Lancer {#AppName}"; Flags: nowait postinstall skipifsilent
