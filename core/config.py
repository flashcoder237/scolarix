"""Configuration persistante de l'application.

Le fichier ``config.json`` est stocké dans le dossier de données de l'utilisateur
(``%APPDATA%\\FMSP_Archives`` sous Windows, ``~/.fmsp_archives`` ailleurs).
On peut forcer un autre dossier avec la variable d'environnement ``FMSP_DATA_DIR``
(utile pour les tests ou une installation portable sur clé USB).
"""
from __future__ import annotations

import getpass
import json
import os
import platform
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "Archives Relevés FMSP"
APP_VERSION = "1.0.0"


def data_dir() -> Path:
    override = os.environ.get("FMSP_DATA_DIR")
    if override:
        base = Path(override)
    elif platform.system() == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home())) / "FMSP_Archives"
    else:
        base = Path.home() / ".fmsp_archives"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _default_operator() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - environnement exotique
        return "operateur"


@dataclass
class Settings:
    # Dossier racine des archives (lu en lecture seule, jamais modifié)
    source_root: str = ""
    # Base d'index et cache d'aperçu (sur le poste sécurisé, jamais sur un cloud)
    db_path: str = ""
    cache_dir: str = ""
    # Identité inscrite dans le journal de traçabilité
    operateur: str = field(default_factory=_default_operator)
    poste: str = field(default_factory=socket.gethostname)
    # Options d'indexation
    ocr_enabled: bool = True
    ocr_preprocess: bool = True
    ocr_lang: str = "fra"
    word_com_enabled: bool = True
    preview_cache_enabled: bool = True
    differential: bool = True
    # Outils externes (laisser vide = détection automatique)
    tesseract_cmd: str = ""
    soffice_cmd: str = ""
    # Commande d'impression silencieuse. Variables : {file}, {printer}
    # Exemples : r'C:\\Outils\\SumatraPDF.exe -print-to-default -silent "{file}"'
    #            r'C:\\Outils\\PDFtoPrinter.exe "{file}"'
    # Vide = impression via Qt (QPrinter, imprimante par défaut, sans dialogue).
    print_command: str = ""
    printer_name: str = ""
    duplex: bool = True
    # Recherche
    fuzzy_threshold: int = 80
    max_results: int = 500

    def resolved_db_path(self) -> Path:
        return Path(self.db_path) if self.db_path else data_dir() / "releves.db"

    def resolved_cache_dir(self) -> Path:
        p = Path(self.cache_dir) if self.cache_dir else data_dir() / "cache" / "preview_pdfs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def export_dir(self) -> Path:
        desktop = Path.home() / "Desktop"
        if not desktop.exists():
            desktop = Path.home() / "Bureau"
        if not desktop.exists():
            desktop = data_dir() / "exports"
            desktop.mkdir(parents=True, exist_ok=True)
        return desktop


def config_path() -> Path:
    return data_dir() / "config.json"


def load_settings() -> Settings:
    path = config_path()
    s = Settings()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for k, v in raw.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except (OSError, ValueError):
            pass
    return s


def save_settings(s: Settings) -> None:
    config_path().write_text(json.dumps(asdict(s), indent=2, ensure_ascii=False), encoding="utf-8")
