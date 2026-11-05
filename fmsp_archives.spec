# -*- mode: python ; coding: utf-8 -*-
# Construction de l'exécutable Windows :  pyinstaller --noconfirm fmsp_archives.spec
# Résultat : dist\FMSP_Archives\FMSP_Archives.exe (dossier autonome, sans Python à installer).
# Le même .exe sert d'interface ET de robot d'indexation (lancé avec --index par l'interface).

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("ui/theme_fmsp.qss", "ui")],
    hiddenimports=["pypdfium2", "olefile", "win32com.client", "pythoncom", "pywintypes"],
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FMSP_Archives",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="FMSP_Archives", upx=False)
