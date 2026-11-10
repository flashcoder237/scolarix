"""État des outils du poste et diagnostic (Réglages > Outils du poste, Diagnostic).

Détection en lecture seule : rien n'est installé ni modifié. Chaque outil est cherché comme le font
l'indexation et l'aperçu (chemin des Réglages, puis PATH, puis emplacement d'installation par défaut), pour que
l'état affiché soit celui que Scolarix utilisera réellement.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

TESSERACT_DEFAULTS = (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                      r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")
SOFFICE_DEFAULTS = (r"C:\Program Files\LibreOffice\program\soffice.exe",
                    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe")
_VERSION_CACHE: dict[tuple, str | None] = {}


def _run(cmd: list[str], timeout: float = 8.0) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or b"").decode("utf-8", "replace") + (proc.stderr or b"").decode("utf-8", "replace")


def _locate(setting: str, names: tuple[str, ...], defaults: tuple[str, ...]) -> tuple[str | None, str]:
    """(chemin, origine) : « réglage », « PATH », « installation » ; (None, motif) si introuvable."""
    setting = (setting or "").strip().strip('"')
    if setting:
        if Path(setting).is_file():
            return setting, "réglage"
        return None, f"chemin indiqué introuvable : {setting}"
    for n in names:
        found = shutil.which(n)
        if found:
            return found, "PATH"
    if platform.system() == "Windows":
        for cand in defaults:
            if Path(cand).exists():
                return cand, "installation"
    return None, ""


def _version(path: str, args: list[str], pattern: str) -> str | None:
    try:
        st = Path(path).stat()
        key = (path, st.st_size, st.st_mtime_ns)
    except OSError:
        return None
    if key not in _VERSION_CACHE:
        m = re.search(pattern, _run([path, *args]))
        _VERSION_CACHE[key] = m.group(1) if m else None
    return _VERSION_CACHE[key]


def tesseract_status(cmd: str = "") -> dict:
    path, origin = _locate(cmd, ("tesseract",), TESSERACT_DEFAULTS)
    if not path:
        where = "PATH" + (" ni dans C:\\Program Files\\Tesseract-OCR" if platform.system() == "Windows" else "")
        return {"found": False, "path": None, "origin": "", "detail": origin or f"Aucun tesseract dans {where}"}
    langs: list[str] = []
    out = _run([path, "--list-langs"])
    for line in out.splitlines()[1:]:
        line = line.strip()
        if re.fullmatch(r"[A-Za-z_]{3,12}", line):
            langs.append(line)
    return {"found": True, "path": path, "origin": origin,
            "version": _version(path, ["--version"], r"tesseract\s+v?(\d+(?:\.\d+)*)"),
            "langs": langs, "fra": "fra" in langs}


def soffice_status(cmd: str = "") -> dict:
    path, origin = _locate(cmd, ("soffice", "libreoffice"), SOFFICE_DEFAULTS)
    if not path:
        return {"found": False, "path": None, "origin": "", "detail": origin or "LibreOffice introuvable"}
    return {"found": True, "path": path, "origin": origin,
            "version": _version(path, ["--version"], r"LibreOffice\s+(\d+(?:\.\d+){0,3})")}


def word_status(enabled: bool, probe: bool = False) -> dict:
    """Microsoft Word par COM : Windows seulement. Sans ``probe``, on lit le registre (chemin de WINWORD.EXE) ;
    avec ``probe`` (bouton « Tester »), une instance invisible est lancée puis refermée."""
    if platform.system() != "Windows":
        return {"found": False, "enabled": bool(enabled), "platform": False,
                "detail": "Passerelle COM disponible sous Windows seulement"}
    path = None
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Winword.exe") as k:
                    path = winreg.QueryValue(k, None)
                    break
            except OSError:
                continue
    except ImportError:
        pass
    res = {"found": bool(path), "enabled": bool(enabled), "platform": True, "path": path}
    if probe:
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            try:
                word = win32com.client.DispatchEx("Word.Application")
                try:
                    res["version"] = str(word.Version)
                    res["found"] = True
                finally:
                    word.Quit(0)
            finally:
                pythoncom.CoUninitialize()
            res["probe"] = "ok"
        except Exception as exc:          # Word absent, licence, COM bloqué : message montré tel quel
            res["probe"] = f"échec : {exc.__class__.__name__} {str(exc)[:160]}"
    if not res["found"]:
        res["detail"] = "Microsoft Word introuvable sur ce poste"
    return res


def dir_size(path: Path, pattern: str | None = None) -> tuple[int, int]:
    """(octets, nombre de fichiers) d'un dossier, sans descendre dans les sous-dossiers."""
    total = count = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_file() and (pattern is None or e.name.endswith(pattern)):
                        total += e.stat().st_size
                        count += 1
                except OSError:
                    continue
    except OSError:
        pass
    return total, count


def file_size(path: Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def runtime() -> dict:
    return {"python": platform.python_version(), "systeme": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(), "fige": bool(getattr(sys, "frozen", False)), "pid": os.getpid()}
