# -*- mode: python ; coding: utf-8 -*-
# Construction de l'exécutable Windows :  pyinstaller --noconfirm fmsp_archives.spec
# Résultat : dist\Scolarix\Scolarix.exe (dossier autonome, sans Python à installer).
# Le même .exe sert d'interface, de robot d'indexation (--index) et de processus de lecture parallèles
# (multiprocessing.freeze_support dans main.py).
# L'installateur (installer\scolarix.iss, Inno Setup) empaquette ensuite ce dossier.

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("ui/theme_fmsp.qss", "ui"), ("assets/scolarix.ico", "assets"), ("assets/scolarix_256.png", "assets"),
           ("assets/scolarix.svg", "assets"), ("assets/fonts", "assets/fonts"), ("assets/sounds", "assets/sounds")],
    hiddenimports=["PySide6.QtMultimedia", "pypdfium2", "olefile", "lxml", "lxml.etree", "win32com.client", "pythoncom", "pywintypes"],
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Scolarix",
    console=False,
    upx=False,
    icon="assets/scolarix.ico",
    version="installer/version_info.txt",
)
coll = COLLECT(exe, a.binaries, a.datas, name="Scolarix", upx=False)
