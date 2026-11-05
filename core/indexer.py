"""Robot d'indexation.

Il tourne dans un PROCESSUS SÉPARÉ lancé par l'interface (``main.py --index ...``) et
communique sa progression en lignes JSON sur la sortie standard. Avantages : l'interface ne
gèle jamais, un plantage de Word/COM n'emporte pas le guichet, et l'agent peut déjà rechercher
les relevés indexés (SQLite WAL) pendant que le robot travaille.

Chaque fichier est enregistré dans sa propre transaction : arrêter le robot ne laisse jamais
la base dans un état incohérent, et un lancement « différentiel » reprend là où il s'était arrêté.

Utilisable aussi en ligne de commande :
    python main.py --index --source "\\\\SRV-FMSP\\Archives" [--only-new] [--no-ocr]
    python main.py --check-integrity
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Settings, load_settings
from .database import Database
from .extractors import (MIN_TEXT_CHARS, ExtractionError, PageText, WordConverter, configure_tesseract,
                         detect_type, docx_pages_text, file_signature, ocr_pdf_pages, pdf_pages_text)
from .parsing import parse_pages

EXTENSIONS = {".pdf", ".doc", ".docx"}
Emit = Callable[[dict], None]


@dataclass
class Counters:
    detectes: int = 0
    extraits: int = 0
    blocs: int = 0
    ecrits: int = 0
    ignores: int = 0
    echecs: int = 0
    formats: dict = field(default_factory=dict)


def scan_sources(root: Path) -> list[Path]:
    files = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in EXTENSIONS and not p.name.startswith(("~$", ".~lock")):
            files.append(p)
    files.sort()
    return files


def cache_path_for(source: Path, cache_dir: Path) -> Path:
    digest = hashlib.sha1(str(source.resolve()).lower().encode("utf-8")).hexdigest()[:20]
    return cache_dir / f"{digest}.pdf"


class Indexer:
    def __init__(self, settings: Settings, db: Database, emit: Emit | None = None):
        self.s = settings
        self.db = db
        self.emit = emit or (lambda e: None)
        self.counters = Counters()
        self.cache_dir = settings.resolved_cache_dir()
        self.ocr_available = settings.ocr_enabled and configure_tesseract(settings)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    # ------------------------------------------------------------ par fichier
    def _pages_for(self, path: Path, kind: str, converter: WordConverter) -> tuple[list[PageText], str, str | None]:
        """Renvoie (pages, type_fichier, cache_pdf)."""
        if kind == "PDF":
            pages = pdf_pages_text(path)
            scanned = [p.number for p in pages if len((p.text or "").strip()) < MIN_TEXT_CHARS]
            if scanned and self.ocr_available:
                self.emit({"type": "stage", "stage": "ocr", "file": str(path), "pages": len(scanned)})
                ocr = ocr_pdf_pages(path, scanned, self.s)
                pages = [ocr.get(p.number, p) for p in pages]
                low = [n for n, pt in ocr.items() if (pt.ocr_confidence or 0) < 70]
                if low:
                    self.db.add_anomaly("AVERTISSEMENT", str(path),
                                        f"OCR : confiance faible (< 70 %) sur la/les page(s) {', '.join(map(str, low[:8]))}. "
                                        "À vérifier manuellement.", "Revoir la page")
                return pages, "PDF_SCAN", None
            if scanned and len(scanned) == len(pages):
                self.db.add_anomaly("AVERTISSEMENT", str(path),
                                    "PDF scanné non lu : OCR désactivé ou Tesseract introuvable.", "Activer l'OCR")
                return pages, "PDF_SCAN", None
            return pages, "PDF_TEXT", None

        # Word : conversion en PDF dans le cache (pages exactes + aperçu identique)
        cache = cache_path_for(path, self.cache_dir)
        need_cache = self.s.preview_cache_enabled or kind == "DOC"
        if need_cache and converter.available:
            try:
                if not cache.exists() or cache.stat().st_mtime < path.stat().st_mtime:
                    self.emit({"type": "stage", "stage": "conversion", "file": str(path), "backend": converter.backend()})
                    converter.convert(path, cache)
                return pdf_pages_text(cache), kind, str(cache)
            except ExtractionError as exc:
                if kind != "DOCX" or "mot de passe" in str(exc):
                    raise
                # .docx : on peut encore le lire directement (sans aperçu) plutôt que de l'abandonner
                self.db.add_anomaly("AVERTISSEMENT", str(path), f"{exc} Lecture directe du .docx, aperçu indisponible.",
                                    "Vérifier Word / LibreOffice")
                return docx_pages_text(path), kind, None
        if kind == "DOCX":
            if need_cache:
                self.db.add_anomaly("INFO", str(path),
                                    "Aucun convertisseur Word → PDF : lecture directe, aperçu indisponible "
                                    "et numéros de page approximatifs.", "Installer Word ou LibreOffice")
            return docx_pages_text(path), kind, None
        raise ExtractionError("Fichier Word 97-2003 (.doc) : Microsoft Word ou LibreOffice est nécessaire.")

    def index_file(self, path: Path, converter: WordConverter) -> int:
        chemin = str(path.resolve())
        taille, mtime = file_signature(path)
        self.db.clear_anomalies_for(chemin)
        try:
            kind = detect_type(path)
            pages, type_fichier, cache = self._pages_for(path, kind, converter)
            self.counters.extraits += 1
            self.counters.formats[type_fichier] = self.counters.formats.get(type_fichier, 0) + 1
            releves, warnings = parse_pages(pages, chemin)
            self.counters.blocs += len(releves)
            for w in warnings[:5]:
                self.db.add_anomaly("AVERTISSEMENT", chemin, w, "Revoir la page")
            if len(warnings) > 5:
                self.db.add_anomaly("AVERTISSEMENT", chemin, f"… et {len(warnings) - 5} autres avertissements.")
            linked = [r for r in releves if r["page_fin"] > r["numero_page"]]
            if linked:
                ex = linked[0]
                self.db.add_anomaly("INFO", chemin,
                                    f"{len(linked)} relevé(s) sur plusieurs pages jumelé(s) "
                                    f"(ex. {ex['matricule']} : pages {ex['numero_page']}–{ex['page_fin']}).")
            if not releves:
                self.db.add_anomaly("AVERTISSEMENT", chemin, "Aucun relevé détecté dans ce fichier.", "Revoir le fichier")
            self.db.save_file(chemin, type_fichier, taille, mtime, len(pages), cache, "OK", None, releves)
            self.counters.ecrits += len(releves)
            return len(releves)
        except ExtractionError as exc:
            self._fail(chemin, taille, mtime, str(exc))
        except Exception as exc:  # un fichier défectueux ne doit jamais arrêter l'indexation
            self._fail(chemin, taille, mtime, f"Erreur inattendue : {exc.__class__.__name__}: {exc}")
        return 0

    def _fail(self, chemin: str, taille: int, mtime: float, message: str) -> None:
        self.counters.echecs += 1
        ext = Path(chemin).suffix.lower().lstrip(".").upper() or "?"
        self.db.save_file(chemin, ext, taille, mtime, 0, None, "ERREUR", message, [])
        self.db.add_anomaly("ERREUR", chemin, message, "Ouvrir le dossier")

    # ---------------------------------------------------------------- global
    def run(self, root: Path, only_new: bool | None = None) -> Counters:
        only_new = self.s.differential if only_new is None else only_new
        started = time.time()
        if not root.exists():
            raise FileNotFoundError(f"Dossier introuvable : {root}")
        self.emit({"type": "scan", "root": str(root)})
        files = scan_sources(root)
        self.counters.detectes = len(files)
        self.emit({"type": "start", "total": len(files)})
        with WordConverter(self.s) as converter:
            if not converter.available:
                self.emit({"type": "warning", "message": "Aucun convertisseur Word → PDF (Word ou LibreOffice)."})
            for i, path in enumerate(files, start=1):
                if self._stop:
                    break
                chemin = str(path.resolve())
                if only_new:
                    state = self.db.file_state(chemin)
                    if state is not None:  # inchangé depuis la dernière passe (même en erreur)
                        taille, mtime = file_signature(path)
                        if state["taille"] == taille and abs((state["mtime"] or 0) - mtime) < 1:
                            self.counters.ignores += 1
                            self._progress(i, path, started, skipped=True)
                            continue
                self._progress(i - 1, path, started)
                self.index_file(path, converter)
                self._progress(i, path, started)
        self.db.set_meta("derniere_indexation", time.strftime("%d/%m/%Y %H:%M"))
        self.db.set_meta("source_root", str(root))
        summary = self.counters.__dict__ | {"duree_s": round(time.time() - started, 1), "stopped": self._stop}
        self.emit({"type": "done", **summary})
        return self.counters

    def _progress(self, done: int, path: Path, started: float, skipped: bool = False) -> None:
        total = max(1, self.counters.detectes)
        elapsed = time.time() - started
        eta = (elapsed / done * (total - done)) if done else None
        self.emit({"type": "progress", "done": done, "total": total, "file": str(path), "skipped": skipped,
                   "elapsed_s": round(elapsed, 1), "eta_s": round(eta, 1) if eta is not None else None,
                   "counters": {k: v for k, v in self.counters.__dict__.items() if k != "formats"}})


def check_integrity(db: Database) -> list[str]:
    """Liens orphelins : fichiers indexés dont le chemin n'existe plus (S4.3)."""
    missing = [r["chemin"] for r in db.orphan_files()]
    for chemin in missing:
        db.add_anomaly("AVERTISSEMENT", chemin, "Lien orphelin : fichier déplacé, renommé ou supprimé.", "Relocaliser")
    return missing


def _ensure_stdio() -> None:
    """Dans l'exécutable fenêtré (PyInstaller --windowed), sys.stdout vaut None. Quand l'interface lance
    le robot avec QProcess, les tubes existent pourtant au niveau du système : on les rouvre."""
    for name, fd in (("stdout", 1), ("stderr", 2)):
        stream = getattr(sys, name)
        if stream is None:
            try:
                setattr(sys, name, open(fd, "w", encoding="utf-8", buffering=1, closefd=False))
            except OSError:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        elif hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="fmsp-indexer", description="Indexation des relevés FMSP")
    parser.add_argument("--index", action="store_true")
    parser.add_argument("--check-integrity", action="store_true")
    parser.add_argument("--source", help="Dossier racine des archives")
    parser.add_argument("--db", help="Chemin de la base (par défaut : configuration)")
    parser.add_argument("--only-new", action="store_true", help="Indexation différentielle")
    parser.add_argument("--full", action="store_true", help="Tout réindexer")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--json", action="store_true", help="Progression en lignes JSON (utilisé par l'interface)")
    args = parser.parse_args(argv)
    _ensure_stdio()

    settings = load_settings()
    if args.no_ocr:
        settings.ocr_enabled = False
    db = Database(args.db or settings.resolved_db_path())

    def emit(event: dict) -> None:
        if args.json:
            sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        elif event["type"] in ("start", "done", "warning"):
            print(event)
        elif event["type"] == "progress" and event["done"] and not event.get("skipped"):
            print(f"[{event['done']}/{event['total']}] {event['file']}")

    if args.check_integrity:
        missing = check_integrity(db)
        emit({"type": "integrity", "orphans": missing})
        return 0
    source = Path(args.source or settings.source_root)
    if not str(source) or not source.exists():
        print("Dossier source introuvable. Utilisez --source.", file=sys.stderr)
        return 2
    only_new = True if args.only_new else (False if args.full else None)
    Indexer(settings, db, emit).run(source, only_new=only_new)
    return 0
