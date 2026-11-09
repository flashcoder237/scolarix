# -*- mode: python ; coding: utf-8 -*-
# Moteur de Scolarix 2 (sans interface Qt) :  pyinstaller --noconfirm engine/scolarix-engine.spec
# Résultat : dist\scolarix-engine\scolarix-engine.exe, embarqué par l'installateur Electron (resources\engine).
# Le même .exe sert de moteur (JSON sur l'entrée/sortie standard), de robot d'indexation (--index) et de
# processus de lecture parallèles (multiprocessing.freeze_support dans engine/server.py).
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(ROOT, "engine", "server.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=["pypdfium2", "olefile", "lxml", "lxml.etree", "win32com.client", "pythoncom", "pywintypes",
                   "core.service", "core.indexer", "core.alerts"],
    excludes=["PySide6", "shiboken6", "tkinter", "matplotlib", "IPython", "pytest", "ui", "scipy", "pandas"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="scolarix-engine",
    console=True,          # entrée/sortie standard fiables ; lancé caché par Electron (windowsHide)
    upx=False,
    icon=os.path.join(ROOT, "assets", "scolarix.ico"),
    version=os.path.join(ROOT, "installer", "version_info_engine.txt"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="scolarix-engine", upx=False)
