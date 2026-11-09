"""PDF d'aperçu des relevés Word, produit à la demande.

Pendant l'indexation, les .docx sont lus directement (python-docx) et les .doc convertis en .docx :
aucun rendu PDF, ce qui rend l'indexation plusieurs dizaines de fois plus rapide. Le PDF, nécessaire
à l'aperçu, à l'impression et à l'export, est produit ici, une seule fois par fichier, à la première
consultation :

    original .doc/.docx ──(.doc : Word → .docx)──> copie .docx ──(corrections de texte)──> PDF (cache)

Fidélité (2.0.5) : quand une correction s'applique, Word rend AUSSI la copie sans correction (« original », gardée
en cache : bouton « Aperçu : original » du guichet) et les deux rendus sont comparés (core/layout_check.py). Si la
correction a déplacé quoi que ce soit d'autre que le texte remplacé, l'aperçu publié est l'original, et l'agent
est averti (« Corrections non appliquées : mise en page à risque »). Sans correction applicable, un seul rendu.

Le rendu de Word peut paginer légèrement autrement que l'estimation faite à l'indexation : les numéros
de page de TOUS les relevés du fichier sont donc recalculés sur le PDF obtenu, en retrouvant chaque
relevé par son matricule. Un relevé introuvable sur le PDF bloque l'aperçu (jamais la page d'un autre
étudiant à l'écran ou à l'imprimante).

Toutes les conversions passent par un unique fil d'exécution dédié qui garde Word ouvert entre deux
demandes (COM exige que l'objet Word soit utilisé depuis le fil qui l'a créé). Le travail est fait PAR FICHIER :
une compilation de 41 pages n'est convertie qu'une fois, quel que soit le nombre de ses relevés demandés, et
deux demandes simultanées sur le même fichier attendent le même travail. Un aperçu demandé au guichet remplace
le précédent encore en file d'attente (l'agent est passé à une autre ligne : inutile de le produire).
"""
from __future__ import annotations

import atexit
import hashlib
import logging
import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Callable
from pathlib import Path

from .config import Settings
from .database import Database, Releve
from .extractors import WORD_TIMEOUT, ExtractionError, WordConverter, pdf_pages_text
from .normalize import norm_matricule
from .parsing import parse_pages
from .text_rules import RewriteReport, compile_rules, rewrite_docx, rules_digest

WORD_TYPES = ("DOC", "DOCX")

_log = logging.getLogger("scolarix.preview")


def cache_name_for(source: str | Path) -> str:
    """Nom de base (dans le dossier de cache) des fichiers dérivés d'un fichier source."""
    digest = hashlib.sha1(str(source).lower().encode("utf-8")).hexdigest()[:20]
    return f"{digest}.pdf"


def intermediate_docx(source: str, cache_dir: Path) -> Path:
    """.docx produit à partir d'un .doc 97-2003 (gardé en cache : sert à l'indexation et à l'aperçu)."""
    return cache_dir / cache_name_for(source).replace(".pdf", ".docx")


def preview_name(source: str, settings: Settings) -> str:
    """Nom du PDF d'aperçu : dépend du fichier ET des corrections actives (modifier une règle refait l'aperçu)."""
    digest = rules_digest(settings.text_rules, settings.text_rules_enabled)
    return cache_name_for(source).replace(".pdf", f"_{digest}.pdf")


ENGINE_VERSION = "5"   # à incrémenter quand le rendu ou les corrections changent : refait les aperçus


def _signature(src: Path, extra: str = "") -> str:
    st = src.stat()
    return f"{st.st_size}:{st.st_mtime_ns}:{extra}:{ENGINE_VERSION}"


def _fresh(dest: Path, sig: str) -> bool:
    """Le fichier dérivé correspond exactement à cette version de la source (taille + date + règles)."""
    meta = dest.with_suffix(dest.suffix + ".sig")
    try:
        return dest.exists() and meta.read_text(encoding="utf-8") == sig
    except OSError:
        return False


def _publish(tmp: Path, dest: Path, sig: str, attempts: int = 25) -> None:
    """Écriture atomique : un export interrompu (Word planté, disque plein) ne laisse jamais un PDF tronqué.
    Sous Windows, un antivirus ou un autre lecteur peut verrouiller l'ancien fichier quelques instants :
    on réessaie (5 s au plus) avant d'abandonner."""
    import time
    for i in range(attempts):
        try:
            os.replace(tmp, dest)
            break
        except PermissionError:
            if i == attempts - 1:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(0.2)
    dest.with_suffix(dest.suffix + ".sig").write_text(sig, encoding="utf-8")


def ensure_intermediate_docx(source: str, cache_dir: Path, converter: WordConverter) -> Path:
    """.doc 97-2003 -> .docx en cache, refait dès que la source change (même avec une date plus ancienne)."""
    src = Path(source)
    inter = intermediate_docx(source, cache_dir)
    sig = _signature(src, "docx")
    # Word disponible : un .docx intermédiaire écrit par LibreOffice (indexation sur un poste sans Word, ou Word
    # en échec ce jour-là) n'est jamais repris — sa mise en page peut différer de celle de Word.
    word = bool(getattr(converter, "word_available", False))
    if _fresh(inter, sig) and (not word or produced_by_word(inter)):
        return inter
    meta = inter.with_suffix(inter.suffix + ".sig")
    try:
        legacy = (inter.exists() and not meta.exists() and inter.stat().st_mtime >= src.stat().st_mtime
                  and (not word or produced_by_word(inter)))
    except OSError:
        legacy = False
    if legacy:
        # .docx écrit par l'indexeur 2.0.1 (sans signature) d'après lequel l'index a été construit : repris tel
        # quel au lieu de rouvrir le .doc dans Word (les pages restent confirmées sur le PDF rendu).
        meta.write_text(sig, encoding="utf-8")
        return inter
    tmp = inter.with_name(f"{inter.stem}.{os.getpid()}.{threading.get_ident()}.tmp.docx")
    converter.convert(src, tmp, "docx", label=src.name)
    _publish(tmp, inter, sig)
    return inter


def produced_by_word(docx: Path) -> bool:
    """Le .docx a été écrit par Microsoft Word (docProps/app.xml, élément <Application>)."""
    return "microsoft" in docx_producer(docx).lower()


def docx_producer(docx: Path) -> str:
    """Logiciel qui a écrit le .docx (« Microsoft Office Word », « LibreOffice/24.2… ») ; « » si inconnu."""
    import re
    import zipfile
    try:
        with zipfile.ZipFile(docx) as z:
            xml = z.read("docProps/app.xml").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return ""
    m = re.search(r"<(?:\w+:)?Application>([^<]*)</(?:\w+:)?Application>", xml)
    return m.group(1).strip() if m else ""


def original_name(source: str) -> str:
    """PDF du document SANS correction (même nom que l'aperçu quand les corrections sont désactivées)."""
    return cache_name_for(source).replace(".pdf", "_brut.pdf")


def preview_meta(pdf: str | Path | None) -> dict:
    """Ce qui a été fait pour produire cet aperçu : {"corrections": n, "repli": bool, "motif": str, …}."""
    if not pdf:
        return {}
    try:
        import json
        return json.loads(Path(pdf).with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_meta(dest: Path, meta: dict) -> None:
    import json
    try:
        dest.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def render_original(source: str, kind: str, settings: Settings, converter: WordConverter,
                    work: Path | None = None) -> Path:
    """PDF du document tel qu'il est, SANS aucune correction de texte (cache séparé, réutilisé)."""
    cache_dir = settings.resolved_cache_dir()
    dest = cache_dir / original_name(source)
    src = Path(source)
    sig = _signature(src, "brut")
    if _fresh(dest, sig):
        return dest
    if not converter.available:
        raise ExtractionError("Aperçu impossible : ni Microsoft Word ni LibreOffice n'est installé sur ce poste.")
    if work is None:
        work = ensure_intermediate_docx(source, cache_dir, converter) if kind == "DOC" else src
    with tempfile.TemporaryDirectory(prefix="scolarix_orig_", ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "releve.docx"
        try:
            rewrite_docx(work, copy, [])          # orthographe déclarée vérifiée seulement (export plus rapide)
        except Exception:
            shutil.copyfile(work, copy)
        out = Path(tmp) / "rendu.pdf"
        converter.convert(copy, out, "pdf", label=src.name)
        part = dest.with_name(f"{dest.stem}.{os.getpid()}.{threading.get_ident()}.tmp.pdf")
        shutil.copyfile(out, part)
        _write_meta(dest, {"corrections": 0, "original": True})
        _publish(part, dest, sig)
    return dest


def render_preview(source: str, kind: str, settings: Settings, converter: WordConverter) -> tuple[str, int]:
    """Produit (ou réutilise) le PDF d'aperçu d'un fichier Word. Renvoie (nom dans le cache, nb de corrections
    réellement visibles : 0 si elles ont été écartées par le contrôle du rendu, -1 si l'aperçu était déjà prêt)."""
    from . import layout_check

    cache_dir = settings.resolved_cache_dir()
    name = preview_name(source, settings)
    dest = cache_dir / name
    src = Path(source)
    sig = _signature(src, rules_digest(settings.text_rules, settings.text_rules_enabled))
    if _fresh(dest, sig):
        return name, -1
    if not converter.available:
        raise ExtractionError("Aperçu impossible : ni Microsoft Word ni LibreOffice n'est installé sur ce poste.")
    work = ensure_intermediate_docx(source, cache_dir, converter) if kind == "DOC" else src
    rules = compile_rules(settings.text_rules) if settings.text_rules_enabled else []
    if not rules or name == original_name(source):  # sans règle active, l'aperçu EST le rendu d'origine
        render_original(source, kind, settings, converter, work)
        return original_name(source), 0
    report = RewriteReport()
    with tempfile.TemporaryDirectory(prefix="scolarix_prev_", ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "releve.docx"
        try:
            n = rewrite_docx(work, copy, rules, report)
        except Exception as exc:  # .docx malformé
            raise ExtractionError(f"Corrections de texte impossibles sur ce fichier : {exc}") from exc
        meta: dict = {"corrections": n, "ecartees": report.skipped[:10]}
        if n == 0:
            # aucune correction ne s'applique : l'aperçu est le rendu d'origine (une seule conversion)
            out = render_original(source, kind, settings, converter, work)
        else:
            original = render_original(source, kind, settings, converter, work)
            out = Path(tmp) / "rendu.pdf"
            converter.convert(copy, out, "pdf", label=src.name)
            try:
                motif = layout_check.compare(original, out, report.replacements)
            except Exception as exc:     # contrôle impossible : on ne prend pas le risque
                motif = f"contrôle du rendu impossible ({exc.__class__.__name__})"
            if motif:
                _log.warning("%s : corrections non appliquées (mise en page à risque) : %s", src.name, motif)
                meta.update(corrections=0, repli=True, motif=motif, prevues=n)
                n = 0
                out = original
        part = dest.with_name(f"{dest.stem}.{os.getpid()}.{threading.get_ident()}.tmp.pdf")
        shutil.copyfile(out, part)
        _write_meta(dest, meta)
        _publish(part, dest, sig)
    return name, n


def _match_key(x) -> tuple:
    """Clé d'appariement complète : un relevé de session normale n'est jamais recalé sur la page du rattrapage."""
    get = (lambda k: x.get(k)) if isinstance(x, dict) else (lambda k: getattr(x, k, None))
    return (norm_matricule(get("matricule")), get("semestre") or 0, (get("session") or "").lower()[:6],
            get("annee_academique") or "")


def remap_pages(pdf: Path, releves: list[Releve], source: str) -> tuple[dict[int, tuple[int, int]], list[Releve]]:
    """Associe chaque relevé indexé à ses pages dans le PDF rendu. Renvoie ({id: (début, fin)}, introuvables).
    Les relevés de même clé (exemplaires multiples dans un fichier collectif) sont appariés dans l'ordre des pages ;
    si le nombre d'exemplaires diffère, les relevés en trop sont déclarés introuvables (donc bloqués)."""
    parsed, _ = parse_pages(pdf_pages_text(pdf), source)
    groups: dict[tuple, list[dict]] = {}
    for p in sorted(parsed, key=lambda b: b["numero_page"]):
        groups.setdefault(_match_key(p), []).append(p)
    mapping: dict[int, tuple[int, int]] = {}
    missing: list[Releve] = []
    by_key: dict[tuple, list[Releve]] = {}
    for rel in sorted(releves, key=lambda r: (r.numero_page, r.id)):
        by_key.setdefault(_match_key(rel), []).append(rel)
    for key, rels in by_key.items():
        cands = groups.get(key, [])
        if len(cands) != len(rels):
            missing.extend(rels)         # absent ou ambigu : on ne devine pas, on bloque
            continue
        for rel, cand in zip(rels, cands):
            # contrôle de cohérence : même moyenne et même décision que le relevé indexé
            if any(getattr(rel, a, None) and cand.get(a) and getattr(rel, a) != cand.get(a)
                   for a in ("moyenne", "decision")):
                missing.append(rel)
                continue
            mapping[rel.id] = (cand["numero_page"], cand["page_fin"])
    return mapping, missing


class PreviewCancelled(ExtractionError):
    """Aperçu abandonné : opération annulée par l'agent, ou aperçu du guichet remplacé par un plus récent."""


class _Job:
    """Rendu d'UN fichier Word dans le fil Word, partagé par toutes les demandes qui le concernent."""

    def __init__(self, key: str, rel: Releve, interactive: bool):
        self.key = key
        self.rel = rel
        self.interactive_only = interactive    # seul le guichet l'attend : remplaçable par un aperçu plus récent
        self.future: Future | None = None
        self.started: float | None = None      # instant de DÉBUT du travail (l'attente en file ne compte pas)
        self.reason = ""                       # « superseded » (remplacé) ou « restart » (fil Word relancé)


ProgressFn = Callable[[int, int, str], None]
CancelFn = Callable[[], bool]


class PreviewService:
    """Point d'entrée de l'interface. ``ensure`` est bloquant : l'appeler depuis une tâche de fond, jamais
    depuis le fil de l'interface."""

    # Un fichier .doc demande deux conversions (.doc → .docx, puis → PDF), chacune limitée par WordConverter
    # (WORD_TIMEOUT, Word arrêté au-delà). Ce délai, compté depuis le DÉBUT du travail, n'est qu'un filet de
    # sécurité si Word n'a pas pu être arrêté : le fil Word bloqué est alors abandonné et remplacé.
    WORD_TIMEOUT = WORD_TIMEOUT
    POLL = 0.2

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.s = settings
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scolarix-word")
        self._converter: WordConverter | None = None
        self._local = threading.local()        # une connexion SQLite par fil
        # réentrant : Future.cancel() appelle tout de suite _forget, qui reprend ce verrou
        self._lock = threading.RLock()
        self._jobs: dict[str, _Job] = {}       # travaux en file ou en cours, par fichier
        atexit.register(self.close)

    @property
    def job_timeout(self) -> float:
        return 2 * float(getattr(self.s, "word_timeout", 0) or self.WORD_TIMEOUT) + 60

    @property
    def _wdb(self) -> Database:
        db = getattr(self._local, "db", None)
        if db is None:
            db = self._local.db = Database(self.db.path, cache_dir=self.db.cache_dir)
        return db

    def needs_render(self, rel: Releve) -> bool:
        if rel.type_fichier not in WORD_TYPES:
            return False
        if not rel.cache_pdf or not Path(rel.cache_pdf).exists() or not rel.page_verifiee:
            return True   # un relevé non retrouvé sur le rendu repasse toujours par la vérification
        return Path(rel.cache_pdf).name != preview_name(rel.chemin, self.s)

    # ------------------------------------------------------------------ file d'attente
    def _key(self, rel: Releve) -> str:
        return f"{rel.fichier_id}:{preview_name(rel.chemin, self.s)}"

    def warm_up(self) -> None:
        """Lance Word à l'avance dans le fil des conversions (au démarrage du moteur), sans rien attendre."""
        def _warm():
            if self._converter is None:
                self._converter = WordConverter(self.s)
            warm = getattr(self._converter, "warm", None)
            if warm:
                warm()
        try:
            self._pool.submit(_warm)
        except RuntimeError:          # moteur en cours d'arrêt
            pass

    def prefetch(self, rels: list[Releve], limit: int = 12) -> int:
        """Prépare en arrière-plan les aperçus des autres fichiers Word de l'étudiant affiché : le cursus ou la
        demande qui suit est alors immédiat. Ces travaux cèdent la place : un nouvel aperçu demandé au guichet
        annule ceux qui n'ont pas commencé (au pire, un fichier déjà commencé se termine). Renvoie le nombre
        de fichiers mis en file."""
        n, seen = 0, set()
        for rel in rels:
            key = self._key(rel)
            if key in seen or not self.needs_render(rel):
                continue
            seen.add(key)
            self._submit(rel, interactive=True, supersede=False)
            n += 1
            if n >= limit:
                break
        return n

    def _submit(self, rel: Releve, interactive: bool, supersede: bool = True) -> _Job:
        """Travail du fichier de ``rel`` : celui déjà en file / en cours s'il existe, sinon un nouveau."""
        key = self._key(rel)
        with self._lock:
            if interactive and supersede:
                # l'agent est passé à une autre ligne : les aperçus du guichet encore en attente sont abandonnés
                for other in list(self._jobs.values()):
                    if (other.key != key and other.interactive_only and other.started is None
                            and other.future is not None and other.future.cancel()):
                        other.reason = "superseded"
                        self._jobs.pop(other.key, None)
            job = self._jobs.get(key)
            if job is not None and job.future is not None and not job.future.done():
                if not interactive:
                    job.interactive_only = False   # attendu aussi par un cursus / une demande : plus remplaçable
                return job
            job = _Job(key, rel, interactive)
            job.future = self._pool.submit(self._run_job, job)
            self._jobs[key] = job
            job.future.add_done_callback(lambda _f, j=job: self._forget(j))
            return job

    def _forget(self, job: _Job) -> None:
        with self._lock:
            if self._jobs.get(job.key) is job:
                del self._jobs[job.key]

    def _run_job(self, job: _Job) -> set[int]:
        job.started = time.monotonic()
        return self._render_file(job.rel)

    def _wait(self, job: _Job, rel: Releve, cancelled: CancelFn | None, interactive: bool) -> set[int]:
        """Attend la fin du travail ; l'annulation est vérifiée toutes les 0,2 s. Renvoie les id introuvables."""
        while True:
            fut = job.future
            assert fut is not None
            try:
                return fut.result(timeout=self.POLL)
            except CancelledError:
                if job.reason == "restart":            # fil Word relancé avant que ce travail ne commence
                    job = self._submit(rel, interactive)
                    continue
                raise PreviewCancelled("Aperçu abandonné : un autre relevé a été sélectionné.")
            except FuturesTimeout:
                pass
            if cancelled is not None and cancelled():
                raise PreviewCancelled("Opération annulée.")
            if job.started is not None and time.monotonic() - job.started > self.job_timeout:
                self._restart_word(job)
                raise ExtractionError("Word ne répond plus (boîte de dialogue cachée ?) : il a été arrêté. Réessayer.")

    def _restart_word(self, stuck: _Job) -> None:
        """Filet de sécurité : Word n'a pas pu être arrêté à temps. On l'arrête (son PID seulement), on abandonne
        le fil bloqué et on en crée un neuf ; les travaux qui attendaient derrière y sont relancés."""
        conv = self._converter
        if conv is not None:
            conv.kill()
        with self._lock:
            old, self._pool = self._pool, ThreadPoolExecutor(max_workers=1, thread_name_prefix="scolarix-word")
            self._converter = None
            for job in list(self._jobs.values()):
                if job is not stuck and job.future is not None and job.started is None and job.future.cancel():
                    job.reason = "restart"
                self._jobs.pop(job.key, None)
        old.shutdown(wait=False)
        _log.error("fil Word abandonné après %.0f s sans réponse ; un nouveau fil est créé", self.job_timeout)

    # ------------------------------------------------------------------ API
    def ensure(self, rel: Releve, timeout: float | None = None, interactive: bool = False,
               cancelled: CancelFn | None = None) -> Releve:
        """Renvoie le relevé avec un aperçu PDF valide (et ses pages exactes), en le produisant si besoin.
        ``interactive`` : aperçu demandé par le guichet (remplaçable par un plus récent)."""
        if not self.needs_render(rel):
            return rel
        missing = self._wait(self._submit(rel, interactive), rel, cancelled, interactive)
        if rel.id in missing:
            raise ExtractionError(f"Le relevé {rel.matricule} n'a pas été retrouvé dans l'aperçu produit par Word : "
                                  "impression bloquée par sécurité. Ouvrir le fichier d'origine.")
        out = self._wdb.get(rel.id)
        if out is None:
            raise ExtractionError("Relevé supprimé de l'index pendant la préparation de l'aperçu.")
        return out

    def original(self, rel: Releve) -> tuple[str, int, int]:
        """Rendu SANS correction du fichier de ``rel`` (cache séparé) et pages du relevé sur ce rendu.
        Renvoie (PDF, page de début, page de fin). Exécuté dans le fil Word (affinité COM)."""
        if rel.type_fichier not in WORD_TYPES:
            return rel.preview_pdf or rel.chemin, rel.numero_page, rel.page_fin

        def job():
            if self._converter is None:
                self._converter = WordConverter(self.s)
            pdf = render_original(rel.chemin, rel.type_fichier, self.s, self._converter)
            mapping, _missing = remap_pages(pdf, self._wdb.releves_of_file(rel.fichier_id), rel.chemin)
            return pdf, mapping
        try:
            pdf, mapping = self._pool.submit(job).result(timeout=self.job_timeout)
        except FuturesTimeout as exc:
            raise ExtractionError("Word ne répond plus : aperçu d'origine abandonné. Réessayer.") from exc
        if rel.id not in mapping:
            raise ExtractionError(f"Le relevé {rel.matricule} n'a pas été retrouvé dans le rendu d'origine.")
        first, last = mapping[rel.id]
        return str(pdf), first, last

    def ensure_many(self, rels: list[Releve], progress: ProgressFn | None = None,
                    cancelled: CancelFn | None = None) -> tuple[dict[int, Releve], dict[int, str]]:
        """Prépare les aperçus de plusieurs relevés : UN travail par fichier source (pas par relevé), l'un après
        l'autre, annulation vérifiée entre deux fichiers et pendant l'attente. Un fichier en échec n'arrête pas
        les autres. Renvoie ({id: relevé prêt}, {id: motif d'échec}) ; lève PreviewCancelled si annulé."""
        ready: dict[int, Releve] = {}
        errors: dict[int, str] = {}
        groups: dict[str, list[Releve]] = {}
        for r in rels:
            if self.needs_render(r):
                groups.setdefault(self._key(r), []).append(r)
            else:
                ready[r.id] = r
        n = len(groups)
        for i, group in enumerate(groups.values(), start=1):
            if cancelled is not None and cancelled():
                raise PreviewCancelled("Opération annulée.")
            if progress is not None:
                progress(i, n, Path(group[0].chemin).name)
            try:
                missing = self._wait(self._submit(group[0], False), group[0], cancelled, False)
            except PreviewCancelled:
                raise
            except Exception as exc:
                for r in group:
                    errors[r.id] = str(exc)
                continue
            for r in group:
                fresh = self._wdb.get(r.id)
                if r.id in missing or fresh is None or self.needs_render(fresh):
                    errors[r.id] = "relevé non retrouvé dans l'aperçu produit par Word (impression bloquée par sécurité)"
                else:
                    ready[r.id] = fresh
        return ready, errors

    # ------------------------------------------------------------------ fil Word
    def _render_file(self, rel: Releve) -> set[int]:
        """Rendu PDF du fichier de ``rel`` et recalage des pages de TOUS ses relevés. Renvoie les id introuvables."""
        db = self._wdb
        fresh = db.get(rel.id) or rel
        siblings = db.releves_of_file(fresh.fichier_id)
        if not any(self.needs_render(x) for x in siblings) and not self.needs_render(fresh):
            return set()                               # déjà fait par un travail précédent
        if self._converter is None:
            self._converter = WordConverter(self.s)
        name, n = render_preview(fresh.chemin, fresh.type_fichier, self.s, self._converter)
        pdf = self.s.resolved_cache_dir() / name
        mapping, missing = remap_pages(pdf, siblings, fresh.chemin)
        sig = (fresh.fichier_taille, fresh.fichier_mtime) if fresh.fichier_taille is not None else None
        db.attach_preview(fresh.fichier_id, name, mapping, sig)
        if missing:
            db.add_anomaly("AVERTISSEMENT", fresh.chemin,
                           f"Aperçu Word : {len(missing)} relevé(s) introuvable(s) dans le PDF rendu "
                           f"({', '.join(r.matricule for r in missing[:5])}). Impression bloquée pour ce(s) "
                           "relevé(s) : vérifier le fichier.", "Revoir le fichier")
        return {m.id for m in missing}

    def close(self) -> None:
        """À appeler AVANT l'arrêt de l'interpréteur : ferme Word proprement."""
        def _close():
            if self._converter is not None:
                self._converter.close()
                self._converter = None
        try:
            self._pool.submit(_close).result(timeout=20)
        except Exception:
            if self._converter is not None:
                self._converter.kill()
                self._converter = None
        self._pool.shutdown(wait=False)
