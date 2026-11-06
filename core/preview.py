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
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
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


def render_preview(source: str, kind: str, settings: Settings, converter: WordConverter) -> tuple[str, int]:
    """Produit (ou réutilise) le PDF d'aperçu d'un fichier Word. Renvoie (nom dans le cache, nb de corrections)."""
    cache_dir = settings.resolved_cache_dir()
    name = preview_name(source, settings)
    dest = cache_dir / name
    src = Path(source)
    mtime = src.stat().st_mtime
    if dest.exists() and dest.stat().st_mtime >= mtime:
        return name, -1
    if not converter.available:
        raise ExtractionError("Aperçu impossible : ni Microsoft Word ni LibreOffice n'est installé sur ce poste.")
    work = src
    if kind == "DOC":
        inter = intermediate_docx(source, cache_dir)
        if not inter.exists() or inter.stat().st_mtime < mtime:
            converter.convert(src, inter, "docx")
        work = inter
    rules = compile_rules(settings.text_rules) if settings.text_rules_enabled else []
    with tempfile.TemporaryDirectory(prefix="scolarix_prev_") as tmp:
        copy = Path(tmp) / "releve.docx"
        try:
            n = rewrite_docx(work, copy, rules)
        except Exception as exc:  # .docx malformé : on rend l'original, sans correction
            shutil.copyfile(work, copy)
            n = 0
            if rules:
                raise ExtractionError(f"Corrections de texte impossibles sur ce fichier : {exc}") from exc
        converter.convert(copy, dest, "pdf")
    return name, n


def remap_pages(pdf: Path, releves: list[Releve], source: str) -> tuple[dict[int, tuple[int, int]], list[Releve]]:
    """Associe chaque relevé indexé à ses pages dans le PDF rendu. Renvoie ({id: (début, fin)}, introuvables)."""
    parsed, _ = parse_pages(pdf_pages_text(pdf), source)
    used: set[int] = set()
    mapping: dict[int, tuple[int, int]] = {}
    missing: list[Releve] = []
    for rel in releves:
        mat = norm_matricule(rel.matricule)
        best = None
        for k, p in enumerate(parsed):
            if k in used or norm_matricule(p["matricule"]) != mat:
                continue
            if rel.semestre and p.get("semestre") and rel.semestre != p.get("semestre"):
                continue
            # à matricule égal, on garde le bloc le plus proche de la page estimée à l'indexation
            if best is None or abs(p["numero_page"] - rel.numero_page) < abs(parsed[best]["numero_page"] - rel.numero_page):
                best = k
        if best is None:
            missing.append(rel)
        else:
            used.add(best)
            mapping[rel.id] = (parsed[best]["numero_page"], parsed[best]["page_fin"])
    return mapping, missing


class PreviewService:
    """Point d'entrée de l'interface. ``ensure`` est bloquant : l'appeler depuis une tâche de fond."""

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.s = settings
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scolarix-word")
        self._converter: WordConverter | None = None
        atexit.register(self.close)

    def needs_render(self, rel: Releve) -> bool:
        if rel.type_fichier not in WORD_TYPES:
            return False
        if not rel.cache_pdf or not Path(rel.cache_pdf).exists():
            return True
        return Path(rel.cache_pdf).name != preview_name(rel.chemin, self.s)

    def ensure(self, rel: Releve) -> Releve:
        """Renvoie le relevé avec un aperçu PDF valide (et ses pages exactes), en le produisant si besoin."""
        if not self.needs_render(rel):
            return rel
        return self._pool.submit(self._ensure, rel).result()

    def _ensure(self, rel: Releve) -> Releve:
        fresh = self.db.get(rel.id) or rel
        if not self.needs_render(fresh):
            return fresh
        if self._converter is None:
            self._converter = WordConverter(self.s)
        name, n = render_preview(fresh.chemin, fresh.type_fichier, self.s, self._converter)
        pdf = self.s.resolved_cache_dir() / name
        siblings = self.db.releves_of_file(fresh.fichier_id)
        mapping, missing = remap_pages(pdf, siblings, fresh.chemin)
        self.db.attach_preview(fresh.fichier_id, name, mapping)
        if missing:
            self.db.add_anomaly("AVERTISSEMENT", fresh.chemin,
                                f"Aperçu Word : {len(missing)} relevé(s) introuvable(s) dans le PDF rendu "
                                f"({', '.join(r.matricule for r in missing[:5])}). Vérifier le fichier.", "Revoir le fichier")
        if any(m.id == fresh.id for m in missing):
            raise ExtractionError(f"Le relevé {fresh.matricule} n'a pas été retrouvé dans l'aperçu produit par Word : "
                                  "impression bloquée par sécurité. Ouvrir le fichier d'origine.")
        out = self.db.get(fresh.id)
        if out is None:
            raise ExtractionError("Relevé supprimé de l'index pendant la préparation de l'aperçu.")
        return out

    def close(self) -> None:
        def _close():
            if self._converter is not None:
                self._converter.close()
                self._converter = None
        try:
            self._pool.submit(_close).result(timeout=20)
        except Exception:
            pass
        self._pool.shutdown(wait=False)
