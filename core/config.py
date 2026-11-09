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

APP_NAME = "Scolarix"
APP_SUBTITLE = "Archives des relevés de notes · FMSP"
APP_VERSION = "1.3.1"
UI_SCALES = (80, 90, 100, 110, 125, 150)    # tailles d'interface proposées (en %)

# Corrections d'en-tête par défaut des relevés Word (modifiables dans Réglages).
DEFAULT_TEXT_RULES = (
    {"actif": True, "regex": True, "description": "Retirer l'ancien numéro de téléphone",
     "chercher": r"Tel\s*:\s*33\s*01\s*41\s*39\s*/\s*33\s*01\s*41\s*29\s*[.,]?", "remplacer": ""},
    {"actif": True, "regex": True, "description": "Ajouter « Ref N° » devant le titre",
     "chercher": r"(?<!N° )RELEVE\s+DE\s+NOTES\s*/\s*TRANSCRIPT", "remplacer": "Ref N° RELEVE DE NOTES /TRANSCRIPT",
     "taille": 8, "gras": False, "sauf_si": r"R[ée]f\.?\s*N\s*[°o]"},
    {"actif": True, "regex": True, "description": "Nouvelle référence des services",
     "chercher": r"/?FMSP(?:/[A-Z]{2,8})+", "remplacer": " /FMSP/VDPSAA/VDSSE/VDRC/CDAASSR/SSE",
     "taille": 7, "sauf_si": r"/FMSP/VDPSAA/VDSSE/VDRC/CDAASSR/SSE"},
)


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
    preview_cache_enabled: bool = True     # aperçu PDF des fichiers Word (produit à la première consultation)
    docx_convert_at_index: bool = False    # préconvertir les .docx pendant l'indexation (aperçu immédiat, très lent)
    differential: bool = True
    index_workers: int = 0
    ui_sounds: bool = True                 # sons discrets (succès, attention, erreur, impression)
    ui_animations: bool = True             # petites animations (messages, écrans, compteurs)
    ui_scale: int = 100                    # taille de l'interface en % (Ctrl+Plus / Ctrl+Moins / Ctrl+0)
    exports_root: str = ""                 # vide = Documents\Exports Scolarix ; sinon dossier (partagé) du service
    exports_retention_days: int = 30       # relevés, cursus, vérifications : supprimés après N jours (0 = jamais)
    delivery_ref_stamp: bool = False       # imprimer le numéro de délivrance en bas de chaque page délivrée                 # processus parallèles ; 0 = automatique (cœurs - 1, au plus 4)
    # Dossiers (chemin absolu ou relatif à la racine) et motifs de noms (*DIPLOME*, Brouillons…) à ignorer
    exclusions: list = field(default_factory=list)
    # Corrections de texte appliquées aux SEULS fichiers Word, sur une copie, à l'aperçu, à l'impression
    # et à l'export (l'original n'est jamais modifié). Voir core/text_rules.py.
    text_rules_enabled: bool = True
    text_rules: list = field(default_factory=lambda: [dict(r) for r in DEFAULT_TEXT_RULES])
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

    def exports_root_dir(self) -> Path:
        """Racine des exports : réglable (dossier partagé du service, ou local). Par défaut
        Documents\\Exports Scolarix — pas le Bureau, souvent synchronisé et visible de tous."""
        if self.exports_root:
            root = Path(self.exports_root)
        else:
            docs = next((d for d in (Path.home() / "Documents", Path.home() / "Mes documents") if d.exists()), None)
            root = (docs / "Exports Scolarix") if docs else data_dir() / "exports"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def export_dir(self, nature: str = "Releves") -> Path:
        """Dossier du mois pour une nature de document : <racine>/<Nature>/<AAAA>/<MM>."""
        from datetime import date
        d = date.today()
        folder = self.exports_root_dir() / nature / f"{d:%Y}" / f"{d:%m}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder


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
    _upgrade_rules(s)
    return s


def _upgrade_rules(s: "Settings") -> None:
    """Règles enregistrées avant l'option « sauf si » : on ajoute la protection des règles d'origine
    (évite « Ref N° » en double sur un relevé déjà corrigé à la main)."""
    defaults = {r["chercher"]: r for r in DEFAULT_TEXT_RULES}
    for rule in s.text_rules or []:
        d = defaults.get(rule.get("chercher"))
        if d and d.get("sauf_si") and "sauf_si" not in rule:
            rule["sauf_si"] = d["sauf_si"]


def save_settings(s: Settings) -> None:
    """Écriture atomique : le robot relit ce fichier à son démarrage, il ne doit jamais le trouver tronqué."""
    path = config_path()
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(asdict(s), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def is_network_path(path: str) -> bool:
    """Chemin sur un partage réseau (UNC ou lecteur réseau) : la base SQLite (WAL) ne doit pas y être."""
    p = str(path or "")
    if p.startswith(("\\\\", "//")):
        return True
    if os.name == "nt" and len(p) >= 2 and p[1] == ":":
        try:
            import ctypes
            return ctypes.windll.kernel32.GetDriveTypeW(p[:2] + "\\") == 4   # DRIVE_REMOTE
        except Exception:
            return False
    return False
