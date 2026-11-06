"""Organisation du dossier des exports et registre des documents délivrés.

    <racine>/                           (Réglages › Exports ; dossier partagé du service conseillé)
    ├── Releves/      AAAA/MM/  FMSP_<matricule>_<NOM>_<année>_Niv<n>_S<s>_<AAAAMMJJ-HHMM>.pdf
    ├── Duplicatas/   AAAA/MM/  …_DUPLICATA_<horodatage>.pdf
    ├── Cursus/       AAAA/MM/  FMSP_<matricule>_<NOM>_Cursus_<horodatage>.pdf
    ├── Extractions/  AAAA/MM/  Decanat_<filtres>_<horodatage>.xlsx / .csv
    ├── Verifications/AAAA/MM/  Verification_<matricule>_<horodatage>.pdf
    └── Journal/                Journal_<horodatage>.csv

Principes :
* classement par nature de demande puis par mois : « où est le duplicata de ce matin ? » ;
* aucun fichier n'est jamais écrasé (horodatage, puis suffixe -2, -3… au besoin) ;
* chaque document délivré reçoit un numéro de délivrance (00042/<poste>/2026) et son empreinte
  SHA-256 est enregistrée : l'écran Vérification reconnaît ensuite un fichier sorti du guichet ;
* conservation : les relevés, cursus et vérifications (recopiables à tout moment depuis les
  archives) sont supprimés après N jours ; duplicatas, extractions et journaux sont conservés.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import Settings

RELEVES, DUPLICATAS, CURSUS, EXTRACTIONS, VERIFICATIONS, JOURNAL = (
    "Releves", "Duplicatas", "Cursus", "Extractions", "Verifications", "Journal")
PURGEABLE = (RELEVES, CURSUS, VERIFICATIONS, "Demandes")   # recopiables depuis les archives ; le registre reste


def stamp_now() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M")


def unique_path(folder: Path, filename: str, timestamp: bool = True) -> Path:
    """Chemin libre dans ``folder`` : horodatage ajouté au nom, puis -2, -3… si besoin (jamais d'écrasement)."""
    stem, ext = os.path.splitext(filename)
    if timestamp:
        stem = f"{stem}_{stamp_now()}"
    path = folder / f"{stem}{ext}"
    n = 2
    while path.exists():
        path = folder / f"{stem}-{n}{ext}"
        n += 1
    return path


def dest_for(settings: Settings, nature: str, filename: str) -> Path:
    return unique_path(settings.export_dir(nature), filename)


def short_location(settings: Settings, path: Path) -> str:
    """« Releves\\2026\\11 » plutôt que le chemin complet, quand c'est possible."""
    try:
        return str(Path(path).parent.relative_to(settings.exports_root_dir()))
    except (ValueError, OSError):
        return str(Path(path).parent)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def purge_old(settings: Settings, now: float | None = None) -> int:
    """Supprime les exports « recopiables » plus anciens que la durée de conservation, et les dossiers vidés."""
    days = int(settings.exports_retention_days or 0)
    if days <= 0:
        return 0
    try:
        root = settings.exports_root_dir()
    except OSError:
        return 0
    limit = (now or time.time()) - days * 86400
    n = 0
    for nature in PURGEABLE:
        base = root / nature
        if not base.exists():
            continue
        for f in base.rglob("*"):
            try:
                if f.is_file() and f.stat().st_mtime < limit:
                    f.unlink()
                    n += 1
            except OSError:
                pass
        for d in sorted((p for p in base.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
            try:
                d.rmdir()          # seulement s'il est vide
            except OSError:
                pass
    return n


def reveal(path: str | Path) -> None:
    """Ouvre l'explorateur sur le fichier (sélectionné) ou sur le dossier."""
    path = Path(path)
    if sys.platform.startswith("win"):
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))   # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R" if path.is_file() else "", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)])
