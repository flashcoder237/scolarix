"""PDF d'aperçu des relevés Word, produit à la demande.

Pendant l'indexation, les .docx sont lus directement (python-docx) et les .doc convertis en .docx :
aucun rendu PDF, ce qui rend l'indexation plusieurs dizaines de fois plus rapide. Le PDF, nécessaire
à l'aperçu, à l'impression et à l'export, est produit ici, une seule fois par fichier, à la première
consultation :

    original .doc/.docx ──(.doc : Word → .docx)──> copie .docx ──(corrections de texte)──> PDF (cache)

Le rendu de Word peut paginer légèrement autrement que l'estimation faite à l'indexation : les numéros
de page de TOUS les relevés du fichier sont donc recalculés sur le PDF obtenu, en retrouvant chaque
relevé par son matricule. Un relevé introuvable sur le PDF bloque l'aperçu (jamais la page d'un autre
étudiant à l'écran ou à l'imprimante).

Toutes les conversions passent par un unique fil d'exécution dédié qui garde Word ouvert entre deux
demandes (COM exige que l'objet Word soit utilisé depuis le fil qui l'a créé).
"""
from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

from .config import Settings
from .database import Database, Releve
from .extractors import ExtractionError, WordConverter, pdf_pages_text
from .normalize import norm_matricule
from .parsing import parse_pages
from .text_rules import compile_rules, rewrite_docx, rules_digest

WORD_TYPES = ("DOC", "DOCX")


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


ENGINE_VERSION = "4"   # à incrémenter quand le rendu ou les corrections changent : refait les aperçus


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
    if not _fresh(inter, sig):
        tmp = inter.with_name(f"{inter.stem}.{os.getpid()}.{threading.get_ident()}.tmp.docx")
        converter.convert(src, tmp, "docx")
        _publish(tmp, inter, sig)
    return inter


def render_preview(source: str, kind: str, settings: Settings, converter: WordConverter) -> tuple[str, int]:
    """Produit (ou réutilise) le PDF d'aperçu d'un fichier Word. Renvoie (nom dans le cache, nb de corrections)."""
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
    with tempfile.TemporaryDirectory(prefix="scolarix_prev_", ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "releve.docx"
        try:
            n = rewrite_docx(work, copy, rules)
        except Exception as exc:  # .docx malformé
            if rules:
                raise ExtractionError(f"Corrections de texte impossibles sur ce fichier : {exc}") from exc
            shutil.copyfile(work, copy)
            n = 0
        out = Path(tmp) / "rendu.pdf"
        converter.convert(copy, out, "pdf")
        part = dest.with_name(f"{dest.stem}.{os.getpid()}.{threading.get_ident()}.tmp.pdf")
        shutil.copyfile(out, part)
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


class PreviewService:
    """Point d'entrée de l'interface. ``ensure`` est bloquant : l'appeler depuis une tâche de fond, jamais
    depuis le fil de l'interface."""

    TIMEOUT = 180   # secondes : au-delà, Word est considéré bloqué (boîte de dialogue cachée…) et arrêté

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.s = settings
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scolarix-word")
        self._converter: WordConverter | None = None
        self._db: Database | None = None      # connexion propre au fil Word
        atexit.register(self.close)

    @property
    def _wdb(self) -> Database:
        if self._db is None:
            self._db = Database(self.db.path, cache_dir=self.db.cache_dir)
        return self._db

    def needs_render(self, rel: Releve) -> bool:
        if rel.type_fichier not in WORD_TYPES:
            return False
        if not rel.cache_pdf or not Path(rel.cache_pdf).exists() or not rel.page_verifiee:
            return True   # un relevé non retrouvé sur le rendu repasse toujours par la vérification
        return Path(rel.cache_pdf).name != preview_name(rel.chemin, self.s)

    def ensure(self, rel: Releve, timeout: float | None = None) -> Releve:
        """Renvoie le relevé avec un aperçu PDF valide (et ses pages exactes), en le produisant si besoin."""
        if not self.needs_render(rel):
            return rel
        fut = self._pool.submit(self._ensure, rel)
        try:
            return fut.result(timeout=timeout or self.TIMEOUT)
        except FuturesTimeout:
            conv = self._converter
            if conv is not None:
                conv.kill()          # Word bloqué : on l'arrête sans COM pour libérer le fil
            self._converter = None
            raise ExtractionError("Word ne répond plus (boîte de dialogue cachée ?) : il a été arrêté. Réessayer.")

    def _ensure(self, rel: Releve) -> Releve:
        db = self._wdb
        fresh = db.get(rel.id) or rel
        if not self.needs_render(fresh):
            return fresh
        if self._converter is None:
            self._converter = WordConverter(self.s)
        name, n = render_preview(fresh.chemin, fresh.type_fichier, self.s, self._converter)
        pdf = self.s.resolved_cache_dir() / name
        siblings = db.releves_of_file(fresh.fichier_id)
        mapping, missing = remap_pages(pdf, siblings, fresh.chemin)
        sig = (fresh.fichier_taille, fresh.fichier_mtime) if fresh.fichier_taille is not None else None
        db.attach_preview(fresh.fichier_id, name, mapping, sig)
        if missing:
            db.add_anomaly("AVERTISSEMENT", fresh.chemin,
                           f"Aperçu Word : {len(missing)} relevé(s) introuvable(s) dans le PDF rendu "
                           f"({', '.join(r.matricule for r in missing[:5])}). Impression bloquée pour ce(s) "
                           "relevé(s) : vérifier le fichier.", "Revoir le fichier")
        if any(m.id == fresh.id for m in missing):
            raise ExtractionError(f"Le relevé {fresh.matricule} n'a pas été retrouvé dans l'aperçu produit par Word : "
                                  "impression bloquée par sécurité. Ouvrir le fichier d'origine.")
        out = db.get(fresh.id)
        if out is None:
            raise ExtractionError("Relevé supprimé de l'index pendant la préparation de l'aperçu.")
        return out

    def close(self) -> None:
        """À appeler AVANT l'arrêt de l'interpréteur (QApplication.aboutToQuit) : ferme Word proprement."""
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
