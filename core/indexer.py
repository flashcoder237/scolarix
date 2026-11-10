"""Robot d'indexation.

Il tourne dans un PROCESSUS SÉPARÉ lancé par l'interface (``main.py --index ...``) et
communique sa progression en lignes JSON sur la sortie standard. Avantages : l'interface ne
gèle jamais, un plantage de Word/COM n'emporte pas le guichet, et l'agent peut déjà rechercher
les relevés indexés (SQLite WAL) pendant que le robot travaille.

Vitesse (10 000 fichiers et plus) :

* **Processus parallèles** : la lecture et l'analyse des fichiers sont réparties sur plusieurs
  processus (``ProcessPoolExecutor``). Chaque processus a sa propre instance de Word pour les
  .doc. Le processus principal est le seul à écrire dans la base (SQLite n'accepte qu'un
  écrivain à la fois) : chaque fichier y est enregistré dans sa propre transaction, si bien
  qu'arrêter le robot ne laisse jamais la base incohérente.
* **.docx lus directement** (python-docx) : pas de conversion Word → PDF pendant l'indexation.
  Le PDF d'aperçu est produit plus tard, à la première consultation (voir ``core/preview.py``).
* **Exploration rapide** : ``os.scandir`` (taille et date fournies par le listage du dossier,
  sans appel supplémentaire sur un partage réseau) et élagage des sous-dossiers exclus.
* **Différentiel** : les fichiers inchangés (même taille, même date) sont ignorés — SAUF s'ils ont été
  analysés par une version antérieure des règles de lecture (``PARSER_VERSION``) : ils sont alors relus,
  pour qu'une correction de l'analyse (niveaux, doublons…) s'applique sans « Vider l'index ». Les relevés
  d'un fichier inchangé relu gardent leur identifiant (demandes en cours et journal intacts).

Utilisable aussi en ligne de commande :
    python main.py --index --source "\\\\SRV-FMSP\\Archives" [--only-new] [--no-ocr]
                   [--workers 4] [--exclude "*DIPLOME*" --exclude "Brouillons"]
    python main.py --check-integrity
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .config import Settings, load_settings
from .database import Database
from .extractors import (MIN_TEXT_CHARS, ExtractionError, PageText, WordConverter, configure_tesseract,
                         detect_type, docx_pages_text, kill_orphan_words, ocr_pdf_pages, pdf_pages_text)
from .parsing import PARSER_VERSION, parse_pages
from .preview import cache_name_for, ensure_intermediate_docx, render_preview

EXTENSIONS = {".pdf", ".doc", ".docx"}
ALWAYS_SKIPPED = ("~$*", ".~lock*", "Thumbs.db", "desktop.ini")
Emit = Callable[[dict], None]


@dataclass
class Counters:
    detectes: int = 0
    extraits: int = 0
    blocs: int = 0
    ecrits: int = 0
    ignores: int = 0
    relus: int = 0        # fichiers inchangés relus parce que l'analyse a changé de version
    exclus: int = 0
    echecs: int = 0
    illisibles: list = field(default_factory=list)
    formats: dict = field(default_factory=dict)


# ------------------------------------------------------------------ exclusions
class Exclusions:
    """Règles d'exclusion de l'exploration.

    * un chemin de dossier (absolu, ou relatif au dossier racine) : ce dossier et tout son contenu ;
    * un motif sans séparateur (``*DIPLOME*``, ``Brouillons``, ``*.tmp``) : comparé, sans tenir
      compte des majuscules ni des accents, au NOM de chaque dossier et de chaque fichier.
    """

    def __init__(self, root: Path, rules: Iterable[str] = ()):
        self.root = root
        self.folders: list[str] = []
        self.patterns: list[str] = [_fold(p) for p in ALWAYS_SKIPPED]
        for raw in rules:
            rule = (raw or "").strip().strip('"')
            if not rule:
                continue
            if any(sep in rule for sep in ("/", "\\")) or (len(rule) > 1 and rule[1] == ":"):
                p = Path(rule)
                if not p.is_absolute():
                    p = root / p
                self.folders.append(_fold_path(str(p)))
                try:   # lecteur réseau mappé (Z:\) résolu en \\SRV\partage, comme la racine
                    resolved = _fold_path(str(p.resolve()))
                    if resolved not in self.folders:
                        self.folders.append(resolved)
                except OSError:
                    pass
            else:
                self.patterns.append(_fold(rule))

    def name_excluded(self, name: str) -> bool:
        n = _fold(name)
        return any(fnmatch.fnmatchcase(n, pat) for pat in self.patterns)

    def dir_excluded(self, path: str, name: str) -> bool:
        if self.name_excluded(name):
            return True
        p = _fold_path(path)
        return any(p == f or p.startswith(f + os.sep) for f in self.folders)

    def path_excluded(self, path: str) -> bool:
        """Pour un fichier déjà indexé : est-il (ou un de ses dossiers parents) désormais exclu ?"""
        p = Path(path)
        try:
            rel = p.relative_to(self.root)
        except ValueError:
            return False
        if self.name_excluded(p.name):
            return True
        cur = self.root
        for part in rel.parts[:-1]:
            cur = cur / part
            if self.dir_excluded(str(cur), part):
                return True
        return False


def _fold(text: str) -> str:
    from .normalize import strip_accents
    return strip_accents(text).casefold()


def _fold_path(path: str) -> str:
    return _fold(os.path.normpath(path)).rstrip("\\/")


@dataclass
class SourceFile:
    path: str
    taille: int
    mtime: float


def scan_sources(root: Path, exclusions: Exclusions | None = None,
                 counters: Counters | None = None) -> list[SourceFile]:
    """Liste les .pdf/.doc/.docx sous ``root`` en élaguant les dossiers exclus."""
    exclusions = exclusions or Exclusions(root)
    out: list[SourceFile] = []
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError as exc:
            if counters is not None:     # dossier illisible (réseau, droits, chemin trop long) : signalé
                counters.illisibles.append(f"{current} ({exc.__class__.__name__})")
            continue
        with it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if exclusions.dir_excluded(entry.path, entry.name):
                            if counters is not None:
                                counters.exclus += 1
                            continue
                        stack.append(entry.path)
                    elif entry.is_file():
                        if os.path.splitext(entry.name)[1].lower() not in EXTENSIONS:
                            continue
                        if exclusions.name_excluded(entry.name):
                            if counters is not None:
                                counters.exclus += 1
                            continue
                        st = entry.stat()  # sous Windows : fourni par le listage, sans accès réseau supplémentaire
                        out.append(SourceFile(entry.path, st.st_size, st.st_mtime))
                except OSError:
                    continue
    out.sort(key=lambda f: f.path.casefold())
    return out


def cache_path_for(source: Path, cache_dir: Path) -> Path:
    return cache_dir / cache_name_for(source)


# ------------------------------------------------- traitement d'un fichier (sans base)
@dataclass
class FileResult:
    chemin: str
    taille: int
    mtime: float
    type_fichier: str = "?"
    nb_pages: int = 0
    cache_pdf: str | None = None
    statut: str = "OK"
    message: str | None = None
    releves: list = field(default_factory=list)
    anomalies: list = field(default_factory=list)   # (niveau, message, action)
    duree: float = 0.0


class FileProcessor:
    """Extraction + analyse d'un fichier. N'écrit jamais dans la base : utilisable dans un sous-processus."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.cache_dir = settings.resolved_cache_dir()
        self.ocr_available = settings.ocr_enabled and configure_tesseract(settings)
        self._converter: WordConverter | None = None

    @property
    def converter(self) -> WordConverter:
        if self._converter is None:
            self._converter = WordConverter(self.s)
        return self._converter

    def close(self) -> None:
        if self._converter is not None:
            self._converter.close()
            self._converter = None

    def _pages_for(self, res: FileResult, path: Path, kind: str) -> list[PageText]:
        if kind == "PDF":
            pages = pdf_pages_text(path)
            scanned = [p.number for p in pages if len((p.text or "").strip()) < MIN_TEXT_CHARS]
            if self.ocr_available and not any(re.search(r"MATRICULE|\d{2}\s?[A-Z]{1,4}\s?\d{2,4}", p.text or "", re.I)
                                              for p in pages):
                # Scan portant un peu de texte (tampon, numéro de page) mais aucun matricule : OCR de toutes les pages
                scanned = [p.number for p in pages]
            if scanned and self.ocr_available:
                ocr = ocr_pdf_pages(path, scanned, self.s)
                pages = [ocr.get(p.number, p) for p in pages]
                low = [n for n, pt in ocr.items() if (pt.ocr_confidence or 0) < 70]
                if low:
                    res.anomalies.append(("AVERTISSEMENT",
                                          f"OCR : confiance faible (< 70 %) sur la/les page(s) "
                                          f"{', '.join(map(str, low[:8]))}. À vérifier manuellement.", "Revoir la page"))
                res.type_fichier = "PDF_SCAN"
                return pages
            if scanned and len(scanned) == len(pages):
                res.anomalies.append(("AVERTISSEMENT", "PDF scanné non lu : OCR désactivé ou Tesseract introuvable.",
                                      "Activer l'OCR"))
                res.type_fichier = "PDF_SCAN"
                return pages
            res.type_fichier = "PDF_TEXT"
            return pages

        res.type_fichier = kind
        if self.s.docx_convert_at_index and self.converter.available:
            # Option « préconvertir » : aperçu immédiat au guichet, au prix d'une indexation bien plus lente.
            try:
                name, _ = render_preview(res.chemin, kind, self.s, self.converter)
                res.cache_pdf = name
                return pdf_pages_text(self.cache_dir / name)
            except ExtractionError as exc:
                if kind != "DOCX" or "mot de passe" in str(exc):
                    raise
                res.anomalies.append(("AVERTISSEMENT", f"{exc} Lecture directe du .docx.", "Vérifier Word / LibreOffice"))
                return docx_pages_text(path)
        if kind == "DOCX":
            # Lecture directe, très rapide ; l'aperçu PDF sera produit à la première consultation.
            return docx_pages_text(path)
        # .doc (format binaire 97-2003) : Word/LibreOffice l'enregistre en .docx (bien plus rapide qu'un PDF),
        # puis lecture directe. Le .docx est gardé en cache et resservira pour l'aperçu.
        if not self.converter.available:
            raise ExtractionError("Fichier Word 97-2003 (.doc) : Microsoft Word ou LibreOffice est nécessaire.")
        # Même .docx (et même signature .sig) que celui de l'aperçu : Word ne refait pas la conversion au guichet.
        inter = ensure_intermediate_docx(res.chemin, self.cache_dir, self.converter)
        return docx_pages_text(inter)

    def process(self, src: SourceFile) -> FileResult:
        started = time.time()
        res = FileResult(src.path, src.taille, src.mtime)
        path = Path(src.path)
        try:
            kind = detect_type(path)
            pages = self._pages_for(res, path, kind)
            res.nb_pages = len(pages)
            releves, warnings = parse_pages(pages, res.chemin)
            # Les avertissements de sécurité (matricule illisible, décision à vérifier, blocs mélangés) sont
            # TOUS gardés ; les autres sont limités pour ne pas noyer le journal.
            serious = [w for w in warnings if any(k in w.lower() for k in ("illisible", "vérifier", "mélang", "rattach"))]
            others = [w for w in warnings if w not in serious]
            for w in serious + others[:10]:
                res.anomalies.append(("AVERTISSEMENT", w, "Revoir la page"))
            if len(others) > 10:
                res.anomalies.append(("AVERTISSEMENT", f"… et {len(others) - 10} autres avertissements.", None))
            linked = [r for r in releves if r["page_fin"] > r["numero_page"]]
            if linked:
                ex = linked[0]
                res.anomalies.append(("INFO", f"{len(linked)} relevé(s) sur plusieurs pages jumelé(s) "
                                              f"(ex. {ex['matricule']} : pages {ex['numero_page']}–{ex['page_fin']}).", None))
            if not releves:
                res.anomalies.append(("AVERTISSEMENT", "Aucun relevé détecté dans ce fichier.", "Revoir le fichier"))
            if res.cache_pdf:   # pages lues sur le PDF rendu lui-même : exactes
                for r in releves:
                    r["page_verifiee"] = 1
            res.releves = releves
        except ExtractionError as exc:
            self._fail(res, str(exc))
        except Exception as exc:  # un fichier défectueux ne doit jamais arrêter l'indexation
            self._fail(res, f"Erreur inattendue : {exc.__class__.__name__}: {exc}")
        res.duree = time.time() - started
        return res

    @staticmethod
    def _fail(res: FileResult, message: str) -> None:
        res.statut = "ERREUR"
        res.message = message
        res.type_fichier = Path(res.chemin).suffix.lower().lstrip(".").upper() or "?"
        res.releves = []
        res.anomalies = [("ERREUR", message, "Ouvrir le dossier")]


# ------------------------------------------------------- sous-processus (workers)
_PROCESSOR: FileProcessor | None = None


def _worker_init(settings: Settings, parallel: bool) -> None:
    global _PROCESSOR
    if parallel:
        # Tesseract lance ses propres fils d'exécution : avec plusieurs processus, un seul par OCR est plus rapide.
        os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    _PROCESSOR = FileProcessor(settings)
    import multiprocessing.util as mpu
    mpu.Finalize(None, _worker_close, exitpriority=10)   # ferme Word à la sortie normale du processus
    # Si le robot principal est tué, on ne laisse ni processus ni Word orphelins.
    import multiprocessing
    parent = multiprocessing.parent_process()
    if parent is not None:
        def watchdog():
            parent.join()
            if _PROCESSOR is not None and _PROCESSOR._converter is not None:
                _PROCESSOR._converter.kill()   # pas d'appel COM depuis ce fil : arrêt par PID
            os._exit(0)
        threading.Thread(target=watchdog, daemon=True).start()


def _worker_close() -> None:
    if _PROCESSOR is not None:
        _PROCESSOR.close()


def _worker_process(src: SourceFile) -> FileResult:
    assert _PROCESSOR is not None
    return _PROCESSOR.process(src)


def _path_key(path: str) -> str:
    """Identité d'un chemin (insensible à la casse sous Windows)."""
    return os.path.normcase(os.path.normpath(path))


def default_workers() -> int:
    cpu = os.cpu_count() or 2
    return max(1, min(4, cpu - 1))


# ------------------------------------------------------------------ robot
class Indexer:
    def __init__(self, settings: Settings, db: Database, emit: Emit | None = None):
        self.s = settings
        self.db = db
        self.emit = emit or (lambda e: None)
        self.counters = Counters()
        self._stop = threading.Event()
        self._last_emit = 0.0

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # Compatibilité : indexer un fichier isolé, dans le processus courant.
    def index_file(self, path: Path) -> int:
        st = path.stat()
        proc = FileProcessor(self.s)
        try:
            res = proc.process(SourceFile(str(path.resolve()), st.st_size, st.st_mtime))
        finally:
            proc.close()
        self._store(res)
        return len(res.releves)

    def _store(self, res: FileResult) -> None:
        import sqlite3
        for attempt in range(5):     # base occupée par le guichet : on patiente plutôt que d'arrêter l'indexation
            try:
                self.db.store_result(res.chemin, res.type_fichier, res.taille, res.mtime, res.nb_pages,
                                     res.cache_pdf, res.statut, res.message, res.releves, res.anomalies,
                                     version=PARSER_VERSION)
                break
            except sqlite3.OperationalError:
                if attempt == 4:
                    raise
                time.sleep(1 + attempt)
        c = self.counters
        if res.statut == "OK":
            c.extraits += 1
            c.formats[res.type_fichier] = c.formats.get(res.type_fichier, 0) + 1
            c.blocs += len(res.releves)
            c.ecrits += len(res.releves)
        else:
            c.echecs += 1

    def run(self, root: Path, only_new: bool | None = None, workers: int | None = None,
            paths: list[str] | None = None) -> Counters:
        """Une passe du robot. ``paths`` : relecture de ces seuls fichiers (écran Anomalies, « Relire »),
        sans exploration ni différentiel ; ``root`` sert alors seulement à l'historique."""
        only_new = self.s.differential if only_new is None else only_new
        started = time.time()
        relecture = paths is not None
        if not relecture and not root.exists():
            raise FileNotFoundError(f"Dossier introuvable : {root}")
        if not relecture or root.exists():
            root = root.resolve()
        mode = "RELECTURE" if relecture else ("DIFFERENTIELLE" if only_new else "COMPLETE")
        pass_id, before = self._pass_begin(mode, str(root))
        purged = 0
        if relecture:
            files = self._sources_for(paths)
            self.counters.detectes = len(files)
            todo = list(files)
        else:
            exclusions = Exclusions(root, self.s.exclusions)
            self.emit({"type": "scan", "root": str(root)})
            files = scan_sources(root, exclusions, self.counters)
            self.counters.detectes = len(files)

            purged = self._purge_excluded(root, exclusions)
            todo: list[SourceFile] = []
            if self.counters.illisibles:
                msg = (f"{len(self.counters.illisibles)} dossier(s) illisible(s), ignoré(s) cette fois : "
                       + "; ".join(self.counters.illisibles[:5]))
                self.emit({"type": "warning", "message": msg})
                self.db.add_anomaly("AVERTISSEMENT", str(root), msg, "Vérifier l'accès réseau")
            if only_new:
                known = {_path_key(k): v for k, v in self.db.file_signatures(with_version=True).items()}
                for f in files:
                    state = known.get(_path_key(f.path))
                    if state is not None and state[0] == f.taille and abs((state[1] or 0) - f.mtime) < 1:
                        if state[2] == PARSER_VERSION:
                            self.counters.ignores += 1   # inchangé depuis la dernière passe (même en erreur)
                            continue
                        self.counters.relus += 1         # inchangé, mais lu par une version antérieure de l'analyse
                    todo.append(f)
                if self.counters.relus:
                    self.emit({"type": "warning", "message": f"{self.counters.relus} fichier(s) inchangé(s) relu(s) : "
                                                             f"les règles de lecture ont été mises à jour "
                                                             f"({PARSER_VERSION})."})
            else:
                todo = files

        n_workers = workers if workers is not None else (self.s.index_workers or default_workers())
        n_workers = max(1, min(int(n_workers), 16, max(1, len(todo))))
        self.emit({"type": "start", "total": len(files), "a_traiter": len(todo), "workers": n_workers,
                   "exclus": self.counters.exclus, "retires": purged, "mode": mode})
        done = self.counters.ignores
        self._progress(done, None, started, force=True)

        if n_workers == 1:
            proc = FileProcessor(self.s)
            try:
                for f in todo:
                    if self.stopped:
                        break
                    self.emit({"type": "stage", "stage": "lecture", "file": f.path})
                    self._store(proc.process(f))
                    done += 1
                    self._progress(done, f.path, started)
            finally:
                proc.close()
        else:
            done = self._run_parallel(todo, n_workers, done, started)

        if not relecture:
            self.db.set_meta("derniere_indexation", time.strftime("%d/%m/%Y %H:%M"))
            self.db.set_meta("source_root", str(root))
        summary = {k: v for k, v in self.counters.__dict__.items() if k != "illisibles"} | {
            "illisibles": len(self.counters.illisibles), "duree_s": round(time.time() - started, 1), "stopped": self.stopped,
                                            "workers": n_workers, "mode": mode}
        self._pass_end(pass_id, before, summary)
        self.emit({"type": "done", **summary})
        return self.counters

    def _sources_for(self, paths: list[str]) -> list[SourceFile]:
        """Fichiers à relire (liste choisie dans l'écran Anomalies) : ceux qui existent encore."""
        out, seen, missing = [], set(), 0
        for p in paths:
            key = _path_key(p)
            if not p or key in seen:
                continue
            seen.add(key)
            try:
                st = os.stat(p)
            except OSError:
                missing += 1
                continue
            if os.path.isfile(p) and os.path.splitext(p)[1].lower() in EXTENSIONS:
                out.append(SourceFile(p, st.st_size, st.st_mtime))
        if missing:
            self.emit({"type": "warning", "message": f"{missing} fichier(s) à relire introuvable(s) (déplacés, "
                                                     "renommés ou supprimés) : ignorés."})
        return out

    def _pass_begin(self, mode: str, source: str) -> tuple[int | None, tuple[int, int]]:
        """Historique des passes (jamais bloquant : une base d'une version antérieure ne doit rien empêcher)."""
        try:
            c = self.db.conn
            before = (c.execute("SELECT COUNT(*) FROM etudiants_releves").fetchone()[0],
                      c.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0])
            return self.db.pass_begin(mode, source), before
        except Exception:
            return None, (0, 0)

    def _pass_end(self, pass_id: int | None, before: tuple[int, int], summary: dict) -> None:
        if pass_id is None:
            return
        try:
            c = self.db.conn
            after = (c.execute("SELECT COUNT(*) FROM etudiants_releves").fetchone()[0],
                     c.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0])
            k = self.counters
            self.db.pass_end(pass_id, fichiers=k.detectes, lus=k.extraits + k.echecs, ecrits=k.ecrits,
                             echecs=k.echecs, ignores=k.ignores, exclus=k.exclus, releves_delta=after[0] - before[0],
                             anomalies_delta=after[1] - before[1], duree_s=summary.get("duree_s"),
                             arretee=1 if self.stopped else 0, workers=summary.get("workers"))
        except Exception:
            pass

    def _run_parallel(self, todo: list[SourceFile], n_workers: int, done: int, started: float) -> int:
        import multiprocessing
        from concurrent.futures.process import BrokenProcessPool
        pending = deque(todo)
        in_flight: dict[Future, SourceFile] = {}
        # « spawn » partout (c'est le seul mode sous Windows) : même comportement sur tous les systèmes,
        # et pas de fork d'un processus qui a déjà des fils d'exécution.
        ctx = multiprocessing.get_context("spawn")
        try:
            with ProcessPoolExecutor(max_workers=n_workers, initializer=_worker_init, initargs=(self.s, True),
                                     mp_context=ctx) as pool:
                try:
                    while pending or in_flight:
                        # File d'attente courte : un arrêt demandé prend effet en quelques secondes.
                        while pending and len(in_flight) < n_workers * 2 and not self.stopped:
                            f = pending.popleft()
                            try:
                                in_flight[pool.submit(_worker_process, f)] = f
                            except BrokenProcessPool:
                                pending.appendleft(f)
                                raise
                        if self.stopped:   # arrêt : on abandonne ce qui n'a pas commencé
                            for fut in [f for f in in_flight if f.cancel()]:
                                in_flight.pop(fut)
                        if not in_flight:
                            break
                        finished, _ = wait(in_flight, timeout=0.5, return_when=FIRST_COMPLETED)
                        for fut in finished:
                            src = in_flight.pop(fut)
                            try:
                                res = fut.result()
                            except BrokenProcessPool:
                                pending.appendleft(src)
                                raise
                            except Exception as exc:  # erreur inattendue dans un processus : on note et on continue
                                res = FileResult(src.path, src.taille, src.mtime)
                                FileProcessor._fail(res, f"Processus de lecture interrompu : "
                                                         f"{exc.__class__.__name__}: {exc}")
                            self._store(res)
                            done += 1
                            self._progress(done, src.path, started)
                finally:
                    for fut in in_flight:
                        fut.cancel()
        except BrokenProcessPool:
            # Un processus de lecture a été tué (Word planté, mémoire, antivirus…) : on termine en séquentiel.
            rest = list(in_flight.values()) + list(pending)
            self.emit({"type": "warning", "message": "Processus parallèles interrompus : fin de l'indexation "
                                                     f"en mode séquentiel ({len(rest)} fichier(s))."})
            proc = FileProcessor(self.s)
            try:
                for f in rest:
                    if self.stopped:
                        break
                    self._store(proc.process(f))
                    done += 1
                    self._progress(done, f.path, started)
            finally:
                proc.close()
        finally_kill = kill_orphan_words()
        if finally_kill:
            self.emit({"type": "warning", "message": f"{finally_kill} instance(s) de Word restée(s) ouverte(s) "
                                                     "ont été fermées."})
        return done

    def _purge_excluded(self, root: Path, exclusions: Exclusions) -> int:
        """Retire de l'index les fichiers déjà indexés qui relèvent désormais d'une exclusion
        (les fichiers eux-mêmes ne sont jamais touchés)."""
        if not self.s.exclusions:
            return 0
        n = 0
        prefix = _fold_path(str(root))
        for chemin in list(self.db.file_signatures()):
            if _fold_path(chemin).startswith(prefix) and exclusions.path_excluded(chemin):
                self.db.remove_file(chemin)
                n += 1
        return n

    def _progress(self, done: int, path: str | None, started: float, force: bool = False) -> None:
        now = time.time()
        total = max(1, self.counters.detectes)
        if not force and done < total and now - self._last_emit < 0.15:
            return  # pas plus de ~7 messages par seconde vers l'interface
        self._last_emit = now
        elapsed = now - started
        processed = done - self.counters.ignores
        rate = processed / elapsed if elapsed > 0 and processed > 0 else None
        eta = (total - done) / rate if rate else None
        self.emit({"type": "progress", "done": done, "total": total, "file": path,
                   "elapsed_s": round(elapsed, 1), "eta_s": round(eta, 1) if eta is not None else None,
                   "rate": round(rate, 2) if rate else None,
                   "counters": {k: v for k, v in self.counters.__dict__.items() if k not in ("formats", "illisibles")}})


def check_integrity(db: Database) -> list[str]:
    """Liens orphelins : fichiers indexés dont le chemin n'existe plus (S4.3), et aperçus Word manquants."""
    missing = [r["chemin"] for r in db.orphan_files()]
    for chemin in missing:
        db.add_anomaly("AVERTISSEMENT", chemin, "Lien orphelin : fichier déplacé, renommé ou supprimé.", "Relocaliser")
    return missing


def _ensure_stdio() -> None:
    """Dans l'exécutable fenêtré (PyInstaller --windowed), sys.stdin/stdout valent None. Quand l'interface
    lance le robot avec QProcess, les tubes existent pourtant au niveau du système : on les rouvre."""
    if sys.stdin is None:
        try:
            sys.stdin = open(0, "r", encoding="utf-8", closefd=False)
        except OSError:
            pass
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


def _listen_for_stop(indexer: Indexer) -> None:
    """L'interface écrit « stop » sur l'entrée standard : arrêt propre (fichiers en cours terminés, Word fermé)."""
    def reader():
        try:
            for line in sys.stdin:
                if line.strip().lower() == "stop":
                    indexer.stop()
                    return
            indexer.stop()      # fin de l'entrée standard : l'interface a été fermée ou a planté
        except (OSError, ValueError, AttributeError, TypeError):
            return
    threading.Thread(target=reader, daemon=True).start()


def cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="fmsp-indexer", description="Indexation des relevés FMSP")
    parser.add_argument("--index", action="store_true")
    parser.add_argument("--check-integrity", action="store_true")
    parser.add_argument("--source", help="Dossier racine des archives")
    parser.add_argument("--db", help="Chemin de la base (par défaut : configuration)")
    parser.add_argument("--only-new", action="store_true", help="Indexation différentielle")
    parser.add_argument("--full", action="store_true", help="Tout réindexer")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--workers", type=int, help="Processus parallèles (0 = automatique)")
    parser.add_argument("--exclude", action="append", default=[],
                        help="Dossier ou motif à exclure (répétable), en plus de ceux de la configuration")
    parser.add_argument("--stdin-control", action="store_true", help="Écouter « stop » sur l'entrée standard")
    parser.add_argument("--json", action="store_true", help="Progression en lignes JSON (utilisé par l'interface)")
    parser.add_argument("--paths-file", help="Relire seulement ces fichiers (un chemin par ligne, UTF-8)")
    args = parser.parse_args(argv)
    _ensure_stdio()

    settings = load_settings()
    if args.no_ocr:
        settings.ocr_enabled = False
    if args.exclude:
        settings.exclusions = list(settings.exclusions) + args.exclude
    db = Database(args.db or settings.resolved_db_path(), cache_dir=settings.resolved_cache_dir())

    def emit(event: dict) -> None:
        if args.json:
            sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        elif event["type"] in ("start", "done", "warning"):
            print(event)
        elif event["type"] == "progress" and event.get("file"):
            print(f"[{event['done']}/{event['total']}] {event['file']}")

    if args.check_integrity:
        missing = check_integrity(db)
        emit({"type": "integrity", "orphans": missing})
        return 0
    source = Path(args.source or settings.source_root or ".")
    paths = None
    if args.paths_file:
        # relecture d'une liste de fichiers (écran Anomalies) : la liste est lue puis le fichier supprimé
        with open(args.paths_file, encoding="utf-8") as fh:
            paths = [line.rstrip("\r\n") for line in fh if line.strip()]
        try:
            os.unlink(args.paths_file)
        except OSError:
            pass
    elif not str(source) or not source.exists():
        print("Dossier source introuvable. Utilisez --source.", file=sys.stderr)
        return 2
    only_new = True if args.only_new else (False if args.full else None)
    indexer = Indexer(settings, db, emit)
    if args.stdin_control:
        _listen_for_stop(indexer)
    indexer.run(source, only_new=only_new, workers=args.workers, paths=paths)
    return 0
