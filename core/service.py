"""Services du guichet, sans aucune interface : ce que le moteur expose à l'interface Electron.

Chaque méthode publique prend et renvoie des types JSON (dict, list, str, nombres). Les opérations longues
(aperçu Word, fusion, préparation de demande) sont bloquantes : le moteur les exécute dans ses fils de travail,
jamais dans l'interface — c'est ce qui empêche l'application de se figer.

Une connexion SQLite par fil (threading.local) : plusieurs demandes simultanées sans se gêner.
"""
from __future__ import annotations

import contextlib
import dataclasses
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from . import alerts, demandes, exports, pdf_tools, verification
from . import anomalies as anomaly_causes
from .config import (APP_NAME, APP_VERSION, DEFAULT_TEXT_RULES, UI_SCALES, Settings, config_path, is_network_path,
                     save_settings)
from .database import Database, Releve, missing_periods
from .extractors import ExtractionError
from .guard import PrintBlocked, VisualCheckRequired, check_before_print
from .preview import preview_meta
from .normalize import best_display_name, looks_like_matricule, norm_matricule, norm_text
from .text_rules import RuleError, apply_text, compile_rules, rule_warnings

_log = logging.getLogger("scolarix.service")

RELEVE_PROPS = ("nb_pages", "pages_label", "preview_pdf", "is_success", "periode", "sem_label", "annuel", "is_word",
                "decision_deduite", "date_connue")


class ServiceError(Exception):
    """Erreur expliquée à l'agent (message affiché tel quel)."""


def releve_dict(rel: Releve | None) -> dict | None:
    if rel is None:
        return None
    d = dataclasses.asdict(rel)
    for p in RELEVE_PROPS:
        d[p] = getattr(rel, p)
    d["pages_span"] = page_span_label(rel)      # « 1 (p. 41) » : colonne Pages des demandes et du cursus
    return d


def page_span_label(rel: Releve) -> str:
    """Colonne « Pages » du cursus : le nombre de pages DU RELEVÉ, suivi de sa place dans le fichier
    quand c'est une compilation (« 1 (p. 41) », « 2 (p. 41–42) ») ; jamais la seule position (« 41 »),
    qui se lisait comme 41 pages."""
    n = rel.nb_pages
    if rel.numero_page <= 1 and rel.page_fin <= max(1, n):
        return str(n)
    where = f"p. {rel.numero_page}–{rel.page_fin}" if n > 1 else f"p. {rel.numero_page}"
    return f"{n} ({where})"


def cursus_releve_dict(rel: Releve, **extra) -> dict:
    d = releve_dict(rel)
    d["pages_fichier"] = d["pages_label"]          # « Page 41 » : position dans le fichier (traçabilité)
    d["pages_label"] = page_span_label(rel)
    d.update(extra)
    return d


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


class _Prepared:
    """Aperçus déjà préparés (un travail par fichier) présentés comme un PreviewService à core/demandes.py."""

    def __init__(self, ready: dict[int, Releve], errors: dict[int, str]):
        self.ready, self.errors = ready, errors

    def ensure(self, rel: Releve) -> Releve:
        if rel.id in self.errors:
            raise ExtractionError(self.errors[rel.id])
        return self.ready.get(rel.id, rel)


class Service:
    def __init__(self, settings: Settings, previews=None, db_factory=None):
        self.s = settings
        self.previews = previews
        self.emit = None                       # emit(événement, données) : progression vers l'interface (moteur)
        self._ops: dict[str, threading.Event] = {}
        self._ops_lock = threading.Lock()
        self._local = threading.local()
        # Base et cache fixés au démarrage : un nouveau chemin choisi dans les Réglages s'applique au redémarrage
        # (sinon les fils de travail ouvriraient des bases différentes au milieu d'une session).
        self.db_path = settings.resolved_db_path()
        self.cache_dir = settings.resolved_cache_dir()
        self._db_factory = db_factory or (lambda: Database(self.db_path, cache_dir=self.cache_dir))
        self._consulted: set[int] = set()
        self._consulted_lock = threading.Lock()

    # ------------------------------------------------------------------ socle
    @property
    def db(self) -> Database:
        db = getattr(self._local, "db", None)
        if db is None:
            db = self._local.db = self._db_factory()
        return db

    @property
    def rules_active(self) -> bool:
        return bool(self.s.text_rules_enabled and any(r.get("actif", True) for r in self.s.text_rules))

    def _get(self, releve_id: int) -> Releve:
        rel = self.db.get(int(releve_id))
        if rel is None:
            raise ServiceError("Ce relevé n'existe plus dans l'index (réindexation ?) : relancez la recherche.")
        return rel

    def _log(self, action: str, rel: Releve | None, detail: str, matricule: str | None = None) -> None:
        self.db.log(action, self.s.operateur, self.s.poste, rel.id if rel else None,
                    rel.matricule if rel else matricule, detail)

    def _trace(self, rel: Releve) -> str:
        return " · en-tête corrigé" if rel.is_word and self.rules_active else ""

    # ------------------------------------------------------------------ opérations longues (progression, annulation)
    def cancel(self, op: str) -> dict:
        """Annulation coopérative d'une opération longue (cursus, demande) : vérifiée entre deux fichiers Word
        et pendant chaque attente. Traitée immédiatement par le moteur (hors file des fils de travail)."""
        with self._ops_lock:
            ev = self._ops.setdefault(str(op), threading.Event())   # annulation arrivée avant le début : retenue
        ev.set()
        return {"cancelled": True}

    @contextlib.contextmanager
    def _operation(self, op: str | None):
        if not op:
            yield lambda: False
            return
        with self._ops_lock:
            ev = self._ops.setdefault(str(op), threading.Event())
        try:
            yield ev.is_set
        finally:
            with self._ops_lock:
                self._ops.pop(str(op), None)

    def _progress(self, op: str | None, phase: str, i: int, n: int, message: str) -> None:
        if op and self.emit is not None:
            try:
                self.emit("progress", {"op": op, "phase": phase, "i": i, "n": n, "message": message})
            except Exception:
                pass                           # la progression n'est jamais une raison d'échouer

    def _prepare_word(self, rels: list[Releve], op: str | None, cancelled,
                      what: str) -> tuple[dict[int, Releve], dict[int, str]]:
        """Aperçus Word de plusieurs relevés : une conversion par FICHIER source, progression, annulation.
        Renvoie ({id: relevé prêt}, {id: motif d'échec}) ; un fichier en échec n'arrête pas les autres."""
        from .preview import PreviewCancelled
        if self.previews is None:
            return {r.id: r for r in rels}, {}

        def progress(i: int, n: int, name: str) -> None:
            self._progress(op, "conversion", i, n, f"Conversion {i}/{n} : {name}…")
        try:
            ready, errors = self.previews.ensure_many(rels, progress, cancelled)
        except PreviewCancelled as exc:
            raise ServiceError(f"{what} annulée : rien n'a été produit.") from exc
        if cancelled():
            raise ServiceError(f"{what} annulée : rien n'a été produit.")
        return ready, errors

    # ------------------------------------------------------------------ accueil
    def info(self) -> dict:
        import os
        from .indexer import default_workers
        try:
            exports_dir = str(self.s.exports_root_dir())
        except OSError:
            exports_dir = ""
        return {"app": APP_NAME, "version": APP_VERSION, "operateur": self.s.operateur, "poste": self.s.poste,
                "rules_active": self.rules_active, "ui_scales": list(UI_SCALES), "stats": self.stats(),
                "settings": self.settings_get(), "config_path": str(config_path()), "db_path": str(self.db_path),
                "cache_dir": str(self.cache_dir), "exports_dir": exports_dir, "cpu_count": os.cpu_count() or 0,
                "default_workers": default_workers()}

    def stats(self) -> dict:
        db = self.db
        st = db.stats()
        today = datetime.now().strftime("%Y-%m-%d")
        counts = {a: n for a, n in db.conn.execute(
            "SELECT action, COUNT(*) FROM journal WHERE horodatage >= ? AND action IN ('IMPRESSION','DUPLICATA') "
            "GROUP BY action", (today,)).fetchall()}
        # « à revoir » : erreurs et avertissements que l'agent n'a pas encore marqués « vus » (écran Indexation)
        anomalies, anomalies_fichiers = db.conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT a.chemin) FROM anomalies a LEFT JOIN anomalies_vues v "
            "ON v.chemin = COALESCE(a.chemin, '') AND v.code = a.code "
            "WHERE a.niveau IN ('ERREUR','AVERTISSEMENT') AND v.chemin IS NULL").fetchone()
        return {"releves": st["releves"], "etudiants": st["etudiants"], "fichiers": st["fichiers"],
                "pages": st["pages"], "echecs": st["echecs"], "multi_pages": st.get("multi_pages", 0), "formats": st["formats"], "annees": st["annees"],
                "imprimes": counts.get("IMPRESSION", 0) + counts.get("DUPLICATA", 0),
                "duplicatas": counts.get("DUPLICATA", 0), "anomalies": anomalies,
                "anomalies_fichiers": anomalies_fichiers,
                "derniere_indexation": db.get_meta("derniere_indexation")}

    # ------------------------------------------------------------------ recherche
    def search(self, q: str, tolerant: bool = False, demande_id: int | None = None) -> dict:
        t0 = time.perf_counter()
        q = (q or "").strip()
        rels = self.db.search(q, None, bool(tolerant), self.s.fuzzy_threshold, self.s.max_results) if q else []
        in_demande = set()
        if demande_id:
            in_demande = {r.id for _i, r in self.db.demande_items(int(demande_id)) if r}
        out = []
        for r in rels:
            d = releve_dict(r)
            if r.id in in_demande and "✓ DEMANDE" not in d["flags"]:
                d["flags"].insert(0, "✓ DEMANDE")
            out.append(d)
        counts: dict[str, int] = {}
        for r in rels:
            counts[r.filiere_code or "?"] = counts.get(r.filiere_code or "?", 0) + 1
        mat = norm_matricule(q) if q and looks_like_matricule(q) else ""
        return {"q": q, "results": out, "counts": counts, "ms": round((time.perf_counter() - t0) * 1000),
                "students": len({r.matricule for r in rels}),
                # « 18 m 042 » recherché comme « 18M042 » : affiché à l'agent, comme dans la 1.x
                "norm": mat if mat and mat != q.upper() else ""}

    def groups(self, releve_ids: list[int]) -> list[dict]:
        """Vue « groupée par étudiant » : tout le dossier de chaque étudiant trouvé, périodes manquantes."""
        rels = [r for r in (self.db.get(int(i)) for i in releve_ids) if r]
        by_mat: dict[str, list[Releve]] = {}
        for r in rels:
            by_mat.setdefault(r.matricule, []).append(r)
        dossiers = self.db.dossiers(by_mat) if by_mat else {}
        out = []
        for mat, found in by_mat.items():
            found = sorted(found, key=lambda r: (r.niveau_num, r.semestre or 0, r.annee_academique or ""))
            dossier = dossiers.get(mat) or found
            missing = missing_periods(dossier)
            n_ok = sum(1 for r in dossier if r.is_success)
            latest = max(dossier, key=lambda r: (r.annee_academique or "", r.niveau_num, r.semestre or 0))
            nom = best_display_name([r.nom_complet for r in dossier]) or latest.nom_complet
            out.append({"matricule": mat, "nom": nom, "found": [releve_dict(r) for r in found],
                        "missing": missing, "valides": n_ok, "total": len(dossier),
                        "tone": "red" if missing else ("green" if n_ok == len(dossier) else "amber")})
        return out

    # ------------------------------------------------------------------ relevé
    def releve(self, releve_id: int, consult: bool = False) -> dict:
        rel = self._get(releve_id)
        if consult:
            self._consultation(rel)
        siblings = self.db.level_siblings(rel)
        needs = bool(self.previews is not None and self.previews.needs_render(rel))
        apercu = preview_meta(rel.cache_pdf) if rel.is_word and not needs else {}
        if apercu:
            # « Aperçu : original » proposé quand l'aperçu affiché diffère du document d'origine
            apercu = dict(apercu, original_disponible=bool(apercu.get("corrections")))
        return {"releve": releve_dict(rel), "siblings": [releve_dict(s) for s in siblings],
                "alerts": [{"text": t, "tone": tone}
                           for t, tone in alerts.compute(rel, siblings, self.rules_active, apercu)],
                "needs_render": needs, "preview_pdf": None if needs else rel.preview_pdf, "apercu": apercu}

    def _consultation(self, rel: Releve) -> None:
        """Traçabilité « qui a consulté quoi » : une ligne CONSULTATION par relevé affiché au guichet et par
        session (comme la 1.x), pas une par passage de la flèche ↓."""
        with self._consulted_lock:
            if rel.id in self._consulted:
                return
            self._consulted.add(rel.id)
        self._log("CONSULTATION", rel, f"{rel.periode} · {rel.annee_academique} · {rel.statut or '?'} · {rel.pages_label}")

    def preview(self, releve_id: int) -> dict:
        """Aperçu Word produit au besoin (long : Word ou LibreOffice). Renvoie le relevé à jour."""
        rel = self._get(releve_id)
        if self.previews is not None and self.previews.needs_render(rel):
            rel = self.previews.ensure(rel, interactive=True)   # remplace l'aperçu précédent encore en attente
        if not rel.preview_pdf or not Path(rel.preview_pdf).exists():
            raise ServiceError("Aperçu indisponible : fichier source introuvable ou conversion impossible.")
        self._prefetch_student(rel)
        return self.releve(rel.id)

    def preview_original(self, releve_id: int) -> dict:
        """Rendu du document SANS aucune correction de texte (bouton « Aperçu : original » du guichet), dans un
        cache séparé. Pour consultation seulement : l'impression et l'export utilisent toujours l'aperçu normal."""
        rel = self._get(releve_id)
        if not rel.is_word:
            return {"preview_pdf": rel.preview_pdf, "numero_page": rel.numero_page, "page_fin": rel.page_fin}
        if self.previews is None:
            raise ServiceError("Aperçu d'origine indisponible sur ce poste.")
        pdf, first, last = self.previews.original(rel)
        return {"preview_pdf": pdf, "numero_page": first, "page_fin": last}

    def _prefetch_student(self, rel: Releve) -> None:
        """Après l'aperçu demandé : les autres fichiers Word du même étudiant sont préparés en arrière-plan
        (réglage « prefetch_student_previews »), pour que « Cursus » ou « Ajouter à la demande » soient immédiats."""
        if self.previews is None or not getattr(self.s, "prefetch_student_previews", True) or not rel.matricule:
            return
        try:
            others = [r for r in self.db.cursus(rel.matricule) if r.is_word and r.fichier_id != rel.fichier_id]
            n = self.previews.prefetch(others)
            if n:
                _log.info("aperçus préparés en arrière-plan : %d fichier(s) Word du même étudiant", n)
        except Exception as exc:     # jamais bloquant : ce n'est qu'une anticipation
            _log.warning("préparation anticipée impossible : %s", exc)

    # ------------------------------------------------------------------ contrôle, impression, export
    def check(self, releve_id: int, visually_checked: bool = False) -> dict:
        rel = self._get(releve_id)
        if self.previews is not None and self.previews.needs_render(rel):
            return {"ok": False, "kind": "render", "message": "Aperçu Word en préparation."}
        try:
            check_before_print(rel, visually_checked=bool(visually_checked))
        except VisualCheckRequired as exc:
            return {"ok": False, "kind": "visual", "message": str(exc), "pages": exc.pages,
                    "nom": rel.nom_complet, "matricule": rel.matricule}
        except PrintBlocked as exc:
            self._log("IMPRESSION BLOQUÉE", rel, str(exc)[:300])
            return {"ok": False, "kind": "blocked", "message": str(exc)}
        return {"ok": True}

    def visual_refused(self, releve_id: int, message: str = "") -> dict:
        """L'agent n'a pas confirmé le contrôle visuel : la délivrance n'a pas eu lieu, c'est tracé."""
        rel = self._get(releve_id)
        self._log("IMPRESSION BLOQUÉE", rel, (message or "Contrôle visuel non confirmé par l'agent")[:300])
        return {}

    def _guarded(self, releve_id: int, visually_checked: bool) -> Releve:
        res = self.check(releve_id, visually_checked)
        if not res["ok"]:
            raise ServiceError(res["message"])
        rel = self._get(releve_id)
        if visually_checked:
            self._log("CONTRÔLE VISUEL", rel, "Page(s) confirmée(s) à l'œil par l'agent avant délivrance")
        return rel

    def print_prepare(self, releve_id: int, duplicata: bool = False, visually_checked: bool = False) -> dict:
        """Fichier à envoyer à l'imprimante (pages du relevé, timbre, pied de page) + référence de délivrance."""
        rel = self._guarded(releve_id, visually_checked)
        ref = self.db.new_delivery("DUPLICATA IMPRIMÉ" if duplicata else "IMPRESSION", self.s.poste,
                                   self.s.operateur, rel.matricule, rel.id)
        tmp = pdf_tools.extract_pages(rel.preview_pdf, rel.numero_page, rel.page_fin,
                                      stamp=pdf_tools.stamp_text() if duplicata else None,
                                      footer=pdf_tools.footer_text(ref) if self.s.delivery_ref_stamp else None)
        self.db.complete_delivery(ref, None, exports.sha256_file(tmp))
        return {"file": str(tmp), "ref": ref, "pages_label": rel.pages_label, "nb_pages": rel.nb_pages,
                "duplex": bool(self.s.duplex and rel.nb_pages > 1)}

    def print_done(self, releve_id: int, ref: str, duplicata: bool, how: str, file: str | None = None) -> dict:
        rel = self._get(releve_id)
        self._log("DUPLICATA" if duplicata else "IMPRESSION", rel,
                  f"Réf. {ref} · {rel.periode} · {rel.annee_academique} · {rel.pages_label}"
                  + (" · recto-verso" if rel.nb_pages > 1 else "") + f" · {how}" + self._trace(rel))
        _unlink_temp(file)
        return {"stats": self.stats()}

    def print_failed(self, releve_id: int, ref: str, error: str, file: str | None = None) -> dict:
        rel = self.db.get(int(releve_id))
        self._log("IMPRESSION ÉCHOUÉE", rel, f"Réf. {ref} · {error[:200]}")
        _unlink_temp(file)
        return {}

    def export(self, releve_id: int, duplicata: bool = False, visually_checked: bool = False) -> dict:
        rel = self._guarded(releve_id, visually_checked)
        dest = exports.dest_for(self.s, exports.DUPLICATAS if duplicata else exports.RELEVES,
                                pdf_tools.export_filename(rel, "_DUPLICATA" if duplicata else ""))
        ref = self.db.new_delivery("DUPLICATA" if duplicata else "EXPORT PDF", self.s.poste, self.s.operateur,
                                   rel.matricule, rel.id)
        pdf_tools.extract_pages(rel.preview_pdf, rel.numero_page, rel.page_fin, dest,
                                stamp=pdf_tools.stamp_text() if duplicata else None,
                                footer=pdf_tools.footer_text(ref) if self.s.delivery_ref_stamp else None)
        self.db.complete_delivery(ref, str(dest), exports.sha256_file(dest))
        self._log("EXPORT PDF", rel, f"Réf. {ref} · {dest}" + (" · DUPLICATA" if duplicata else "") + self._trace(rel))
        return {"path": str(dest), "name": dest.name, "ref": ref, "location": exports.short_location(self.s, dest)}

    # ------------------------------------------------------------------ cursus
    def cursus_info(self, matricule: str) -> dict:
        rels = self.db.cursus(matricule)
        if not rels:
            raise ServiceError("Aucun relevé pour ce matricule.")
        # Un relevé présent en plusieurs exemplaires (compilation + fichier individuel, « UPDATE »…) n'y figure
        # qu'une fois (Database.cursus) ; les autres exemplaires sont décrits dans « doublons ».
        default = {r.id for r in pdf_tools.select_for_cursus(rels)}
        corr = self.db.correlated_matricules(matricule)
        return {"matricule": rels[0].matricule,
                "nom": best_display_name([r.nom_complet for r in rels]) or rels[0].nom_complet,
                "releves": [cursus_releve_dict(r, selected=r.id in default) for r in rels],
                "missing": missing_periods(rels), "correlated": corr,
                "correlated_releves": [cursus_releve_dict(r) for c in corr for r in self.db.cursus(c["matricule"])],
                "sans_matricule": [cursus_releve_dict(r) for r in self.db.releves_sans_matricule(matricule)]}

    def cursus_build(self, matricule: str, releve_ids: list[int], cover: bool = True, duplicata: bool = False,
                     mode: str = "export", op: str | None = None) -> dict:
        """Fusion du cursus. Les aperçus Word sont produits d'abord, UNE fois par fichier source (progression
        « Conversion 3/7 : EM4 S1.docx… », annulable avec ``cancel(op)``) ; un relevé dont l'aperçu échoue est
        écarté avec son motif, sans bloquer les autres. Le numéro de délivrance n'est réservé qu'ensuite.
        ``mode="preview"`` : aperçu fusionné de la revue du cursus, sans numéro ni journal (voir _cursus_preview)."""
        if mode == "preview":
            return self._cursus_preview(matricule, releve_ids, cover, duplicata, op)
        rels = self._cursus_rels(releve_ids)
        with self._operation(op) as cancelled:
            ready, errors = self._prepare_word(rels, op, cancelled, "Fusion")
            self._progress(op, "fusion", len(rels), len(rels), "Contrôle et fusion des relevés…")
            base = self.db.cursus(matricule) or rels
            nature = "CURSUS" if mode == "export" else "CURSUS IMPRIMÉ"
            ref = self.db.new_delivery(nature, self.s.poste, self.s.operateur, base[0].matricule)
            if mode == "export":
                nom = (norm_text(base[0].nom or base[0].nom_complet).split() or ["ETUDIANT"])[0]
                dest = exports.dest_for(self.s, exports.CURSUS, f"FMSP_{base[0].matricule}_{nom}_Cursus.pdf")
            else:
                dest = pdf_tools.temp_pdf("cursus_")

            def prepare(rel):
                if rel.id in errors:
                    raise ExtractionError(errors[rel.id])
                rel = ready.get(rel.id, rel)
                check_before_print(rel)
                return rel
            try:
                path, n, missing = pdf_tools.merge_releves(
                    rels, dest, cover=cover, stamp=pdf_tools.stamp_text() if duplicata else None, prepare=prepare,
                    absent=missing_periods(base),
                    footer=pdf_tools.footer_text(ref) if self.s.delivery_ref_stamp else None)
            except ValueError as exc:
                raise ServiceError(f"Échec de la fusion : {exc}") from exc
        self.db.complete_delivery(ref, str(path) if mode == "export" else None, exports.sha256_file(path))
        if mode == "export":
            self._log("CURSUS", None, f"Réf. {ref} · {path} · {n} pages" + (" · DUPLICATA" if duplicata else "")
                      + (f" · écartés : {', '.join(missing)}" if missing else ""), base[0].matricule)
        return {"path": str(path), "name": Path(path).name, "pages": n, "missing": missing, "ref": ref,
                "location": exports.short_location(self.s, Path(path)) if mode == "export" else ""}

    def cursus_printed(self, matricule: str, ref: str, pages: int, how: str, file: str | None = None) -> dict:
        self._log("IMPRESSION", None, f"Réf. {ref} · Cursus {pages} pages ({how})", matricule)
        _unlink_temp(file)
        return {"stats": self.stats()}

    # ------------------------------------------------------------------ demandes
    def _demande(self, did: int) -> dict:
        d = self.db.demande(int(did))
        if d is None:
            raise ServiceError("Demande introuvable.")
        items = [{"item": _row(item), "releve": releve_dict(rel)} for item, rel in self.db.demande_items(int(did))]
        return {"demande": _row(d), "items": items}

    def demandes_list(self, statuts: list[str] | None = None) -> list[dict]:
        return [_row(r) for r in self.db.demandes(statuts or None)]

    def demande_get(self, did: int) -> dict:
        return self._demande(did)

    def demande_create(self, matricule: str | None = None, nom: str | None = None, demandeur: str = "",
                       motif: str = "") -> dict:
        did = self.db.create_demande(self.s.poste, self.s.operateur, matricule, nom, demandeur, motif)
        self._log("DEMANDE", None, f"{self.db.demande(did)['numero']} ouverte · demandeur : {demandeur or '—'}",
                  matricule)
        return self._demande(did)

    def demande_update(self, did: int, fields: dict) -> dict:
        allowed = {k: v for k, v in (fields or {}).items() if k in ("demandeur", "motif", "duplicata", "nom",
                                                                     "matricule", "telephone")}
        d = self.db.demande(int(did))
        if d is None:
            raise ServiceError("Demande introuvable.")
        if "duplicata" in allowed and bool(allowed["duplicata"]) != bool(d["duplicata"]) \
                and d["statut"] in ("PRÊTE", "IMPRIMÉE"):
            # le timbre change : le dossier préparé (ou déjà imprimé) est à refaire, sinon une réimpression
            # partirait avec l'ancien timbre ; la revue de la demande le demande et l'explique avant
            allowed["statut"] = "EN COURS"
        if allowed:
            self.db.update_demande(int(did), **allowed)
        return self._demande(did)

    def demande_add(self, did: int, releve_id: int) -> dict:
        rel = self._get(releve_id)
        d = self.db.demande(int(did))
        if d is None:
            raise ServiceError("Demande introuvable.")
        if d["statut"] not in demandes.OUVERTES:
            raise ServiceError(f"La demande {d['numero']} est {d['statut'].lower()} : ouvrez-en une nouvelle.")
        added = self.db.add_demande_item(int(did), rel.id)
        if not d["matricule"]:
            self.db.update_demande(int(did), matricule=rel.matricule, nom=rel.nom_complet)
        res = self._demande(did)
        res["added"] = bool(added)
        if not added and rel.doublons:
            res["doublon"] = True       # un autre exemplaire du même relevé est déjà dans la demande
        return res

    def demande_add_cursus(self, did: int) -> dict:
        """« Ajouter tout le cursus validé » : le dernier relevé validé de chaque semestre de l'étudiant."""
        d = self.db.demande(int(did))
        if d is None:
            raise ServiceError("Demande introuvable.")
        if d["statut"] not in demandes.OUVERTES:
            raise ServiceError(f"La demande {d['numero']} est {d['statut'].lower()} : ouvrez-en une nouvelle.")
        if not d["matricule"]:
            raise ServiceError("Ajoutez d'abord un relevé de l'étudiant.")
        n = sum(bool(self.db.add_demande_item(int(did), r.id))
                for r in pdf_tools.select_for_cursus(self.db.cursus(d["matricule"])))
        res = self._demande(did)
        res["added_count"] = n
        return res

    def demande_remove(self, did: int, releve_id: int) -> dict:
        self.db.remove_demande_item(int(did), int(releve_id))
        return self._demande(did)

    def demande_reorder(self, did: int, releve_ids: list[int]) -> dict:
        self.db.reorder_demande(int(did), [int(i) for i in releve_ids])
        return self._demande(did)

    def demande_prepare(self, did: int, op: str | None = None) -> dict:
        """Dossier de la demande. Comme pour le cursus : aperçus Word d'abord, une fois par fichier source,
        avec progression et annulation ; un relevé en échec est « bloqué » avec son motif."""
        if self.db.demande(int(did)) is None:
            raise ServiceError("Demande introuvable.")
        rels = [rel for _item, rel in self.db.demande_items(int(did)) if rel]
        with self._operation(op) as cancelled:
            ready, errors = self._prepare_word(rels, op, cancelled, "Préparation de la demande")
            self._progress(op, "fusion", len(rels), len(rels), "Contrôle et assemblage du dossier…")
            prep = demandes.prepare(self.db, self.s, int(did), _Prepared(ready, errors))
        return {"dossier": str(prep.dossier), "a_imprimer": str(prep.a_imprimer) if prep.a_imprimer else None,
                "bordereau": str(prep.bordereau), "pages": prep.pages,
                "prets": [{"releve_id": r.id, "ref": ref, "pages": n} for r, ref, n in prep.prets],
                "bloques": [{"releve_id": r.id if r else None, "periode": r.periode if r else "?", "motif": m}
                            for r, m in prep.bloques], **self._demande(did)}

    def demande_files_to_print(self, did: int, with_bordereau: bool = True, op: str | None = None) -> dict:
        """Fichiers prêts de la demande ; refait le dossier s'il est absent ou périmé."""
        d = self.db.demande(int(did))
        folder = demandes.demande_folder(self.s, d) if d and d["dossier"] else None
        if not (d and d["statut"] in ("PRÊTE", "IMPRIMÉE") and folder and (folder / demandes.A_IMPRIMER).exists()):
            prep = self.demande_prepare(did, op)
            if not prep["a_imprimer"]:
                raise ServiceError("Aucun relevé imprimable dans cette demande : " +
                                   "; ".join(f"{b['periode']} ({b['motif']})" for b in prep["bloques"]))
            folder = Path(prep["dossier"])
        files = ([str(folder / demandes.BORDEREAU)] if with_bordereau else []) + [str(folder / demandes.A_IMPRIMER)]
        return {"files": files, **self._demande(did)}

    def demande_printed(self, did: int, how: str) -> dict:
        d = self.db.demande(int(did))
        prets = [(item, rel) for item, rel in self.db.demande_items(int(did)) if rel and item["etat"] == "PRÊT"]
        for item, rel in prets:
            self._log("DUPLICATA" if d["duplicata"] else "IMPRESSION", rel,
                      f"Réf. {item['reference']} · {rel.periode} · {rel.annee_academique} · {rel.pages_label}"
                      f" · demande {d['numero']} · {how}")
        pages = sum(rel.nb_pages for _i, rel in prets)
        demandes.mark(self.db, self.s, int(did), "IMPRIMÉE", f"{len(prets)} relevé(s), {pages} page(s) imprimés")
        return {**self._demande(did), "stats": self.stats()}

    def demande_mark(self, did: int, statut: str, detail: str = "") -> dict:
        if statut not in demandes.STATUTS:
            raise ServiceError(f"Statut inconnu : {statut}")
        demandes.mark(self.db, self.s, int(did), statut, detail)
        return self._demande(did)

    # ------------------------------------------------------------------ extraction et journal
    def years(self) -> list[int]:
        return self.db.distinct_years()

    def extraction(self, filiere: str | None, year_from: int | None, year_to: int | None, statut: str = "tous",
                   dernier_niveau: bool = False) -> dict:
        from . import exporter
        rows = self.db.extraction(filiere or None, year_from or None, year_to or None, statut, bool(dernier_niveau))
        mentions: dict[str, int] = {}
        per_year: dict[str, int] = {}
        for r in rows:
            m = exporter.mention(r.moyenne, r.grade)
            if m:
                mentions[m] = mentions.get(m, 0) + 1
            per_year[r.annee_academique or "?"] = per_year.get(r.annee_academique or "?", 0) + 1
        self._extraction_cache = rows
        return {"count": len(rows), "mentions": mentions, "per_year": sorted(per_year.items()),
                "rows": [releve_dict(r) for r in rows[:500]]}

    def extraction_export(self, filiere: str | None, year_from: int | None, year_to: int | None, statut: str,
                          dernier_niveau: bool, kind: str, path: str | None, scope: str) -> dict:
        from . import exporter
        rows = self.db.extraction(filiere or None, year_from or None, year_to or None, statut, bool(dernier_niveau))
        if not rows:
            raise ServiceError("Aucune ligne à exporter avec ces filtres.")
        if not path:
            name = f"Decanat_{filiere or 'Toutes'}_{statut}_{year_from or 'debut'}-{(year_to + 1) if year_to else 'fin'}.{kind}"
            path = str(exports.dest_for(self.s, exports.EXTRACTIONS, name))
        if kind == "xlsx":
            exporter.export_xlsx(rows, path, f"Extraction FMSP — {scope}")
        else:
            exporter.export_csv(rows, path)
        self.db.log("EXTRACTION", self.s.operateur, self.s.poste, None, None,
                    f"{scope} · {statut} · {len(rows)} lignes · {path} · SHA-256 {exports.sha256_file(path)[:16]}…")
        return {"path": path, "name": Path(path).name, "count": len(rows)}

    def journal(self, matricule: str | None = None, since: str | None = None, limit: int = 1000) -> list[dict]:
        return [_row(r) for r in self.db.journal(matricule or None, since or None, int(limit))]

    def journal_export(self, path: str, matricule: str | None = None, since: str | None = None) -> dict:
        """« Exporter le journal (CSV) » : les lignes affichées (mêmes filtres)."""
        from . import exporter
        if not path:
            path = str(exports.dest_for(self.s, exports.JOURNAL, "Journal.csv"))
        rows = self.db.journal(matricule or None, since or None, 100000)
        exporter.export_journal_csv(rows, path)
        return {"path": str(path), "name": Path(path).name, "count": len(rows)}

    SUGGEST = {"extraction": exports.EXTRACTIONS, "journal": exports.JOURNAL}

    def suggest_path(self, nature: str, filename: str) -> str:
        """Chemin proposé dans la boîte « Enregistrer sous » : le dossier des exports du mois, nom horodaté."""
        if nature not in self.SUGGEST:
            raise ServiceError(f"Nature d'export inconnue : {nature}")
        return str(exports.dest_for(self.s, self.SUGGEST[nature], Path(filename).name))

    def exports_dir(self) -> str:
        """« Ouvrir le dossier des exports »."""
        try:
            return str(self.s.exports_root_dir())
        except OSError as exc:
            raise ServiceError(f"Dossier des exports inaccessible : {exc}") from exc

    def check_integrity(self) -> dict:
        """Vérification d'intégrité : fichiers indexés déplacés, renommés ou supprimés (liens orphelins)."""
        import json
        missing = [r["chemin"] for r in self.db.orphan_files()]
        with self.db._lock:     # une seule ligne par fichier orphelin, même après plusieurs vérifications
            self.db.conn.execute("DELETE FROM anomalies WHERE message LIKE 'Lien orphelin%'")
            self.db.conn.commit()
        for chemin in missing:
            self.db.add_anomaly("AVERTISSEMENT", chemin, "Lien orphelin : fichier déplacé, renommé ou supprimé.",
                                "Relocaliser")
        self.db.set_meta("integrite", json.dumps({"date": datetime.now().strftime("%d/%m/%Y %H:%M"),
                                                  "orphelins": len(missing)}))
        return {"orphelins": len(missing), "fichiers": missing[:50]}

    def anomalies(self, niveau: str | None = None, offset: int = 0, limit: int | None = None) -> list[dict]:
        """Liste brute (compatibilité) : sans plafond, paginée si ``limit`` est donné."""
        return [_row(r) for r in self.db.anomalies(niveau or None, limit, offset)]

    def index_reset(self, confirm: str = "") -> dict:
        """Vide l'index (pas le journal, ni le registre, ni les demandes) et le cache des aperçus Word."""
        if confirm != "VIDER":
            raise ServiceError("Confirmation manquante.")
        import shutil
        counts = self.db.reset_index()
        cache = Path(self.s.resolved_cache_dir())
        removed = 0
        if cache.exists():
            for f in cache.iterdir():
                try:
                    if f.is_dir():
                        shutil.rmtree(f)
                    else:
                        f.unlink()
                    removed += 1
                except OSError:
                    pass       # fichier en cours d'utilisation : il sera remplacé à la prochaine passe
        open_items = self.db.conn.execute(
            "SELECT COUNT(*) FROM demande_items i JOIN demandes d ON d.id = i.demande_id "
            "WHERE d.statut IN ('EN COURS','PRÊTE','IMPRIMÉE')").fetchone()[0]
        self._log("INDEX VIDÉ", None, f"{counts['releves']} relevé(s), {counts['fichiers']} fichier(s), "
                                      f"{counts['anomalies']} anomalie(s) retirés ; {removed} élément(s) du cache "
                                      "supprimés ; journal, registre et demandes conservés")
        return {**counts, "cache": removed, "demandes_items_ouverts": open_items, "stats": self.stats()}

    def index_impact(self) -> dict:
        """Ce que « Vider l'index » retirerait et conserverait (affiché avant confirmation)."""
        c = self.db.conn
        one = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        return {"releves": one("SELECT COUNT(*) FROM etudiants_releves"), "fichiers": one("SELECT COUNT(*) FROM fichiers"),
                "anomalies": one("SELECT COUNT(*) FROM anomalies"), "journal": one("SELECT COUNT(*) FROM journal"),
                "delivrances": one("SELECT COUNT(*) FROM delivrances"), "demandes": one("SELECT COUNT(*) FROM demandes"),
                "demandes_ouvertes": one("SELECT COUNT(*) FROM demandes WHERE statut IN ('EN COURS','PRÊTE','IMPRIMÉE')")}

    # ------------------------------------------------------------------ vérification
    def verify_identify(self, path: str) -> dict:
        """Registre des délivrances (à l'octet près) puis lecture du document : l'original le plus probable."""
        try:
            row = self.db.find_delivery(exports.sha256_file(path))
        except OSError as exc:
            raise ServiceError(f"Fichier illisible : {exc}") from exc
        delivered = _row(row)
        lecture = self._verify_read(path)
        ident = verification.identify(lecture.text)
        mat = (delivered or {}).get("matricule") or ident.get("matricule")
        rels = self.db.cursus(mat) if mat else []
        target = None
        if delivered and delivered.get("releve_id"):
            target = next((r for r in rels if r.id == delivered["releve_id"]), None)
        target = target or verification.best_match(rels, ident)
        return {"delivered": delivered, "ident": ident, "matricule": mat, "soumis": lecture.info(),
                "releves": [releve_dict(r) for r in rels], "best": target.id if target else None}

    def verify_compare(self, path: str, releve_id: int, demandeur: str = "", reference: str = "") -> dict:
        """Comparaison champ par champ, journalisée (VERIFICATION) comme dans la 1.x."""
        res, _rows, orig = self._verify_compare(path, releve_id)
        bad = res["total"] - res["conformes"]
        self._log("VERIFICATION", orig, f"{demandeur or 'demandeur ?'} · réf. {reference or '?'} · {bad} divergence(s)")
        return res

    def _verify_compare(self, path: str, releve_id: int) -> tuple[dict, list, Releve]:
        from .extractors import pdf_pages_text
        orig = self._get(releve_id)
        if self.previews is not None and self.previews.needs_render(orig):
            orig = self.previews.ensure(orig)
        lecture = self._verify_read(path)
        pages = pdf_pages_text(Path(orig.preview_pdf)) if orig.preview_pdf else []
        orig_text = "\n".join(p.text for p in pages[orig.numero_page - 1:orig.page_fin])
        rows = verification.compare(lecture.text, orig_text, orig)
        pos = self._verify_positions(rows, lecture, orig)
        res = {"original": releve_dict(orig), "soumis": lecture.info(),
               "rows": [dict(dataclasses.asdict(r), conforme=r.conforme, pos=p) for r, p in zip(rows, pos)],
               "conformes": sum(1 for r in rows if r.conforme), "total": len(rows)}
        return res, rows, orig

    def verify_report(self, path: str, releve_id: int, demandeur: str = "", reference: str = "") -> dict:
        """Rapport de vérification (PDF) pour l'organisme qui a soumis le document."""
        res, rows, orig = self._verify_compare(path, releve_id)
        dest = exports.dest_for(self.s, exports.VERIFICATIONS, f"Verification_{orig.matricule}.pdf")
        verification.report_pdf(rows, orig, dest, demandeur, reference, self.s.operateur)
        bad = res["total"] - res["conformes"]
        self._log("VERIFICATION", orig, f"{demandeur or 'demandeur ?'} · réf. {reference or '?'} · {bad} divergence(s)"
                                        f" · rapport {dest}")
        return {"path": str(dest), "name": dest.name}

    def verify_print_prepare(self, releve_id: int) -> dict:
        """« Imprimer l'original (timbre « Vérifié ») » : pages de l'original contrôlées, timbre apposé."""
        orig = self._get(releve_id)
        if self.previews is not None and self.previews.needs_render(orig):
            orig = self.previews.ensure(orig)
        if not orig.preview_pdf:
            raise ServiceError("Aucun original sélectionné.")
        check_before_print(orig)
        tmp = pdf_tools.extract_pages(orig.preview_pdf, orig.numero_page, orig.page_fin,
                                      stamp=pdf_tools.stamp_text("VÉRIFIÉ"))
        return {"file": str(tmp), "pages_label": orig.pages_label,
                "duplex": bool(self.s.duplex and orig.nb_pages > 1)}

    def verify_printed(self, releve_id: int, how: str = "", file: str | None = None) -> dict:
        orig = self._get(releve_id)
        self._log("IMPRESSION", orig, f"Original vérifié · {orig.pages_label}" + (f" · {how}" if how else ""))
        _unlink_temp(file)
        return {"stats": self.stats()}

    # ------------------------------------------------------------------ réglages
    PUBLIC = ("operateur", "poste", "source_root", "exports_root", "exports_retention_days", "printer_name",
              "print_command", "duplex", "fuzzy_threshold", "max_results", "ocr_enabled", "differential",
              "index_workers", "exclusions", "text_rules_enabled", "text_rules", "ui_sounds", "ui_animations",
              "ui_scale", "delivery_ref_stamp", "docx_convert_at_index", "prefetch_student_previews", "ocr_preprocess", "word_com_enabled",
              "tesseract_cmd", "soffice_cmd", "db_path", "cache_dir")

    def settings_get(self) -> dict:
        return {k: getattr(self.s, k) for k in self.PUBLIC if hasattr(self.s, k)}

    def settings_save(self, values: dict) -> dict:
        if "text_rules" in (values or {}):
            self._check_rules(values["text_rules"])
        db_path = str((values or {}).get("db_path") or "").strip()
        if db_path and is_network_path(db_path):
            raise ServiceError("La base d'index doit rester sur le disque local du poste (pas sur un partage réseau : "
                               "risque de corruption avec plusieurs guichets).")
        for key in ("db_path", "cache_dir", "tesseract_cmd", "soffice_cmd", "print_command", "exports_root"):
            if isinstance((values or {}).get(key), str):
                values[key] = values[key].strip()
        for k, v in (values or {}).items():
            if k in self.PUBLIC and hasattr(self.s, k):
                setattr(self.s, k, v)
        save_settings(self.s)
        return self.settings_get()


    # ------------------------------------------------------------------ corrections Word
    @staticmethod
    def _check_rules(rules) -> list:
        try:
            return compile_rules(rules or [])
        except RuleError as exc:
            raise ServiceError(f"Corrections Word : {exc}") from exc

    def rules_defaults(self) -> list:
        """Les règles livrées avec Scolarix (bouton « Rétablir les règles d'origine »)."""
        return [dict(r) for r in DEFAULT_TEXT_RULES]

    def rules_test(self, rules: list, text: str) -> dict:
        """Essai des règles sur une phrase : rien n'est enregistré ni appliqué aux fichiers."""
        compiled = self._check_rules(rules)
        out, n = apply_text(text or "", compiled)
        return {"text": out, "count": n, "avertissements": rule_warnings(compiled)}

    # ---- vérification ----
    # Lecture du document soumis gardée en mémoire (identification puis comparaison : un seul OCR), et
    # positions des champs comparés sur les deux documents (surlignage, défilement vers la divergence).
    _VERIFY_CACHE_MAX = 4

    def _verify_read(self, path: str) -> "verification.Lecture":
        p = Path(path)
        try:
            st = p.stat()
        except OSError as exc:
            raise ServiceError(f"Fichier illisible : {exc}") from exc
        key = (str(p), st.st_mtime_ns, st.st_size, bool(self.s.ocr_preprocess), self.s.ocr_lang or "fra")
        lock = self.__dict__.setdefault("_verify_lock", threading.Lock())
        cache: dict = self.__dict__.setdefault("_verify_cache", {})
        with lock:
            hit = cache.get(key)
        if hit is not None:
            return hit
        try:
            lecture = verification.read_layout(p, self.s)
        except (RuntimeError, ExtractionError) as exc:
            raise ServiceError(str(exc)) from exc
        with lock:
            cache[key] = lecture
            while len(cache) > self._VERIFY_CACHE_MAX:
                cache.pop(next(iter(cache)))
        return lecture

    def _verify_positions(self, rows: list, lecture: "verification.Lecture", orig: Releve) -> list[dict]:
        """Ligne de chaque champ sur le soumis (couche texte ou boîtes OCR) et sur l'original (couche texte)."""
        try:
            lignes_orig = (verification.pdf_lines(orig.preview_pdf, orig.numero_page, orig.page_fin)
                           if orig.preview_pdf else [])
            return verification.locate(rows, lecture.lignes, lignes_orig)
        except Exception:                      # jamais une raison de refuser la comparaison elle-même
            _log.exception("positions de la vérification")
            return [{"soumis": None, "original": None} for _ in rows]

    # ---- indexation ----
    # Écran Indexation (2.1) : état du fonds, tri des anomalies par cause, relecture d'une liste de fichiers.
    # Toutes les listes sont paginées côté moteur (un poste réel a 9 000+ anomalies).

    ANOM_VUES = ("a_traiter", "vues", "tout")
    _VU_JOIN = ("LEFT JOIN anomalies_vues v ON v.chemin = COALESCE(a.chemin, '') AND v.code = a.code")

    def _tesseract_found(self) -> bool:
        import os
        import shutil
        cmd = (self.s.tesseract_cmd or "").strip()
        if cmd:
            return Path(cmd).exists() or bool(shutil.which(cmd))
        if shutil.which("tesseract"):
            return True
        return os.name == "nt" and any(Path(p).exists() for p in (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"))

    def _rel_folder(self, dossier: str | None) -> str:
        """Dossier affiché relativement au dossier racine des archives (« 2004-2005\\Pharmacie\\Scans »)."""
        d = dossier or ""
        root = (self.s.source_root or "").rstrip("\\/")
        if root and len(d) > len(root) and d[:len(root)].casefold() == root.casefold() and d[len(root)] in "\\/":
            return d[len(root) + 1:]
        return d or "(sans dossier)"

    def _anom_where(self, f: dict | None, skip: tuple = ()) -> tuple[str, list]:
        """Filtre commun (cause ou niveau, vue, texte, dossier, année) des requêtes d'anomalies."""
        f = f or {}
        where, args = ["1=1"], []
        cause = str(f.get("cause") or "")
        if cause.startswith("@"):
            where.append("a.niveau = ?")
            args.append(cause[1:])
        elif cause:
            where.append("a.code = ?")
            args.append(cause)
        vue = f.get("vue") or "a_traiter"
        if vue == "a_traiter":
            where.append("v.chemin IS NULL")
        elif vue == "vues":
            where.append("v.chemin IS NOT NULL")
        q = str(f.get("q") or "").strip()
        if q:
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(a.chemin LIKE ? ESCAPE '\\' OR a.message LIKE ? ESCAPE '\\')")
            args += [f"%{esc}%", f"%{esc}%"]
        if "dossier" not in skip and f.get("dossier") is not None and f.get("dossier") != "":
            where.append("a.dossier = ?")
            args.append(f["dossier"])
        if "annee" not in skip and f.get("annee"):
            if f["annee"] == "-":
                where.append("a.annee IS NULL")
            else:
                where.append("a.annee = ?")
                args.append(f["annee"])
        return " AND ".join(where), args

    def _anom_counts(self) -> dict:
        """Effectifs par cause pour chaque vue (À traiter / Vues / Tout), en une passe sur la table."""
        rows = self.db.conn.execute(
            "SELECT a.code, a.niveau, COUNT(*), COUNT(DISTINCT a.chemin), SUM(v.chemin IS NOT NULL), "
            "COUNT(DISTINCT CASE WHEN v.chemin IS NULL THEN a.chemin END), "
            "COUNT(DISTINCT CASE WHEN v.chemin IS NOT NULL THEN a.chemin END) "
            f"FROM anomalies a {self._VU_JOIN} GROUP BY a.code, a.niveau").fetchall()
        out = {}
        for code, niveau, n, nf, vus, f_todo, f_vus in rows:
            vus = vus or 0
            out[code or ""] = {"code": code or "", "niveau": niveau, "label": anomaly_causes.label(code),
                               "fix": getattr(anomaly_causes.BY_CODE.get(code or ""), "fix", ""),
                               "tout": n, "tout_fichiers": nf, "vues": vus, "vues_fichiers": f_vus,
                               "a_traiter": n - vus, "a_traiter_fichiers": f_todo}
        return out

    def _anom_totals(self, vue_filter: str) -> tuple[int, int]:
        cond = {"a_traiter": "v.chemin IS NULL", "vues": "v.chemin IS NOT NULL"}.get(vue_filter, "1=1")
        n, f = self.db.conn.execute(f"SELECT COUNT(*), COUNT(DISTINCT a.chemin) FROM anomalies a {self._VU_JOIN} "
                                    f"WHERE {cond}").fetchone()
        return n or 0, f or 0

    def _common_fix(self, counts: dict) -> dict | None:
        """La correction à plus fort levier (« 1 correction règle 3 104 anomalies »)."""
        fixes = []
        ocr = counts.get("ocr_off")
        if ocr and ocr["a_traiter"]:
            if not self.s.ocr_enabled:
                fixes.append({"code": "ocr_off", "titre": "l'OCR est désactivé sur ce poste",
                              "texte": "Activez l'OCR des scans, puis relisez ces fichiers en une passe "
                                       "(le guichet reste utilisable).", "action": "ocr_enable",
                              "bouton": "Activer l'OCR"})
            elif not self._tesseract_found():
                fixes.append({"code": "ocr_off", "titre": "Tesseract est introuvable sur ce poste",
                              "texte": "Installez-le ou indiquez son chemin dans les Réglages, puis relisez ces "
                                       "fichiers en une passe (le guichet reste utilisable).",
                              "action": "settings", "bouton": "Configurer Tesseract…"})
            else:
                fixes.append({"code": "ocr_off", "titre": "l'OCR est maintenant disponible sur ce poste",
                              "texte": "Ces scans ont été lus sans OCR : relisez-les en une passe.",
                              "action": "reread", "bouton": None})
            fixes[-1].update(n=ocr["a_traiter"], fichiers=ocr["a_traiter_fichiers"])
        word = counts.get("word_absent")
        if word and word["a_traiter"]:
            fixes.append({"code": "word_absent", "titre": "Word ou LibreOffice est absent de ce poste",
                          "texte": "Installez LibreOffice (ou activez la passerelle COM Word), puis relisez "
                                   "ces fichiers.", "action": "settings", "bouton": "Ouvrir les Réglages…",
                          "n": word["a_traiter"], "fichiers": word["a_traiter_fichiers"]})
        return max(fixes, key=lambda x: x["n"]) if fixes else None

    @staticmethod
    def _year_series(annees: list) -> list[dict]:
        """Relevés par année académique, années manquantes comprises ; « trou » = année anormalement creuse
        par rapport à ses voisines (dossier manquant ?)."""
        import re
        import statistics
        by_y: dict[int, tuple[str, int]] = {}
        others = []
        for a, n in annees:
            m = re.match(r"\s*((?:19|20)\d{2})", str(a or ""))
            if m:
                y = int(m.group(1))
                label, prev = by_y.get(y, (f"{y}-{y + 1}", 0))
                by_y[y] = (label, prev + n)
            else:
                others.append((a, n))
        out = []
        if by_y:
            lo, hi = min(by_y), max(by_y)
            years = range(lo, hi + 1) if hi - lo <= 60 else sorted(by_y)
            series = [(y, by_y.get(y, (f"{y}-{y + 1}", 0))) for y in years]
            counts = [n for _, (_, n) in series]
            for i, (y, (label, n)) in enumerate(series):
                neigh = [counts[j] for j in range(max(0, i - 3), min(len(counts), i + 4)) if j != i]
                med = statistics.median(neigh) if neigh else 0
                out.append({"annee": label, "n": n, "trou": bool(med >= 20 and n < 0.25 * med)})
        return out

    def index_overview(self) -> dict:
        """Tout l'écran « État du fonds » en un appel : chiffres, santé, formats, années, causes, passes."""
        import json
        db = self.db
        db.anomalies_backfill()
        c = db.conn
        st = db.stats()
        one = lambda sql, *a: c.execute(sql, a).fetchone()[0] or 0  # noqa: E731
        avert = one("SELECT COUNT(DISTINCT a.chemin) FROM anomalies a JOIN fichiers f ON f.chemin = a.chemin "
                    "WHERE a.niveau = 'AVERTISSEMENT' AND COALESCE(f.statut, 'OK') <> 'ERREUR'")
        echecs = st["echecs"]
        today = datetime.now().strftime("%Y-%m-%d")
        jour = {a: n for a, n in c.execute(
            "SELECT action, COUNT(*) FROM journal WHERE horodatage >= ? AND action IN "
            "('IMPRESSION','DUPLICATA','VERIFICATION','EXTRACTION') GROUP BY action", (today,)).fetchall()}
        counts = self._anom_counts()
        ocr_files = one("SELECT COUNT(DISTINCT a.chemin) FROM anomalies a JOIN fichiers f ON f.chemin = a.chemin "
                        "WHERE a.code = 'ocr_off'")
        ocr_why = ("OCR désactivé" if not self.s.ocr_enabled else
                   "Tesseract introuvable" if not self._tesseract_found() else "lus sans OCR")
        labels = {"PDF_SCAN": "PDF scanné", "DOC": "Word .doc", "PDF_TEXT": "PDF texte", "DOCX": "Word .docx",
                  "PDF": "PDF illisible"}
        fmts: dict[str, dict] = {}
        for t, statut, n in c.execute("SELECT type_fichier, COALESCE(statut, 'OK'), COUNT(*) FROM fichiers "
                                      "GROUP BY type_fichier, statut").fetchall():
            d = fmts.setdefault(t or "?", {"type": t or "?", "label": labels.get(t or "", t or "?"), "n": 0,
                                           "echecs": 0, "note": ""})
            d["n"] += n
            if statut == "ERREUR":
                d["echecs"] += n
        for d in fmts.values():
            notes = []
            if d["type"] == "PDF_SCAN" and ocr_files:
                notes.append(f"{ocr_files:,} non lu(s) · {ocr_why}".replace(",", "\u202f"))
            if d["echecs"]:
                notes.append(f"{d['echecs']:,} en échec".replace(",", "\u202f"))
            d["note"] = " · ".join(notes)
        passes = []
        for r in db.passes(8):
            p = dict(r)
            passes.append(p)
        try:
            integ = json.loads(db.get_meta("integrite") or "null")
        except ValueError:
            integ = None
        last_exclus = next((p["exclus"] for p in passes if p.get("fin") and p.get("mode") != "RELECTURE"), None)
        todo_n, todo_f = self._anom_totals("a_traiter")
        all_n, all_f = self._anom_totals("tout")
        causes = sorted((x for x in counts.values() if x["a_traiter"]), key=lambda x: -x["a_traiter"])
        return {
            "releves": st["releves"], "etudiants": st["etudiants"], "pages": st["pages"], "fichiers": st["fichiers"],
            "sante": {"fichiers": st["fichiers"], "echec": echecs, "avertissement": avert,
                      "ok": max(0, st["fichiers"] - echecs - avert), "exclus": last_exclus},
            "aujourdhui": {"imprimes": jour.get("IMPRESSION", 0) + jour.get("DUPLICATA", 0),
                           "duplicatas": jour.get("DUPLICATA", 0), "verifications": jour.get("VERIFICATION", 0),
                           "extractions": jour.get("EXTRACTION", 0)},
            "formats": sorted(fmts.values(), key=lambda d: -d["n"]),
            "annees": self._year_series(st["annees"]),
            "anomalies": {"a_traiter": todo_n, "a_traiter_fichiers": todo_f, "tout": all_n, "tout_fichiers": all_f,
                          "causes": len(causes)},
            "causes": causes,
            "fix": self._common_fix(counts),
            "passes": passes,
            "derniere_indexation": db.get_meta("derniere_indexation"),
            "integrite": {"date": (integ or {}).get("date"), "orphelins": counts.get("orphelin", {}).get("tout_fichiers", 0),
                          "verifiee": bool(integ)},
            "outils": {"ocr": bool(self.s.ocr_enabled), "tesseract": self._tesseract_found()},
            "source": {"chemin": self.s.source_root, "existe": bool(self.s.source_root) and Path(self.s.source_root).exists()},
        }

    def anomalies_summary(self, vue: str = "a_traiter") -> dict:
        """Colonne « Causes » : effectifs par cause et par niveau pour la vue choisie, et totaux des trois vues."""
        self.db.anomalies_backfill()
        vue = vue if vue in self.ANOM_VUES else "a_traiter"
        counts = self._anom_counts()
        causes = []
        order = {c.code: i for i, c in enumerate(anomaly_causes.CAUSES)}
        for x in counts.values():
            if x[vue]:
                causes.append({"code": x["code"], "niveau": x["niveau"], "label": x["label"], "fix": x["fix"],
                               "n": x[vue], "fichiers": x[f"{vue}_fichiers"]})
        rank = {"ERREUR": 0, "AVERTISSEMENT": 1, "INFO": 2}
        causes.sort(key=lambda x: (rank.get(x["niveau"], 3), -x["n"], order.get(x["code"], 99)))
        totaux = {}
        for v in self.ANOM_VUES:
            n, f = self._anom_totals(v)
            totaux[v] = {"n": n, "fichiers": f}
        return {"vue": vue, "causes": causes, "totaux": totaux, "fix": self._common_fix(counts)}

    def anomalies_files(self, filter: dict | None = None, offset: int = 0, limit: int = 200) -> dict:
        """Colonne « Fichiers » : fichiers de la cause choisie (une ligne par fichier), triés par dossier,
        paginés ; avec les effectifs par dossier et par année (filtres et en-têtes de groupe)."""
        c = self.db.conn
        f = filter or {}
        where, args = self._anom_where(f)
        limit = max(1, min(int(limit or 200), 2000))
        rows = c.execute(
            "SELECT a.chemin, a.dossier, a.annee, COUNT(*) AS n, MIN(a.id) AS premier, a.message, a.niveau, "
            "GROUP_CONCAT(DISTINCT a.code) AS codes, SUM(v.chemin IS NOT NULL) AS vus, "
            "MAX(f.nb_pages) AS nb_pages, MAX(f.type_fichier) AS type_fichier, MAX(f.statut) AS statut "
            f"FROM anomalies a {self._VU_JOIN} LEFT JOIN fichiers f ON f.chemin = a.chemin "
            f"WHERE {where} GROUP BY a.chemin ORDER BY a.dossier COLLATE NOCASE, a.chemin COLLATE NOCASE "
            "LIMIT ? OFFSET ?", args + [limit, max(0, int(offset or 0))]).fetchall()
        out = []
        for r in rows:
            chemin = r["chemin"] or ""
            out.append({"chemin": chemin, "nom": Path(chemin).name if chemin else "(sans fichier)",
                        "dossier": r["dossier"] or "", "annee": r["annee"], "n": r["n"], "message": r["message"],
                        "niveau": r["niveau"], "codes": (r["codes"] or "").split(","), "vu": (r["vus"] or 0) >= r["n"],
                        "nb_pages": r["nb_pages"], "type_fichier": r["type_fichier"], "statut": r["statut"],
                        "page": anomaly_causes.page_of(r["message"])})
        total = c.execute(f"SELECT COUNT(DISTINCT COALESCE(a.chemin, '')), COUNT(*) FROM anomalies a {self._VU_JOIN} "
                          f"WHERE {where}", args).fetchone()
        dw, da = self._anom_where(f, skip=("dossier",))
        dossiers = [{"dossier": d or "", "label": self._rel_folder(d), "n": n} for d, n in c.execute(
            f"SELECT a.dossier, COUNT(DISTINCT COALESCE(a.chemin, '')) FROM anomalies a {self._VU_JOIN} WHERE {dw} "
            "GROUP BY a.dossier ORDER BY a.dossier COLLATE NOCASE", da).fetchall()]
        yw, ya = self._anom_where(f, skip=("annee",))
        annees = [{"annee": y or "-", "n": n} for y, n in c.execute(
            f"SELECT a.annee, COUNT(DISTINCT COALESCE(a.chemin, '')) FROM anomalies a {self._VU_JOIN} WHERE {yw} "
            "GROUP BY a.annee ORDER BY a.annee", ya).fetchall()]
        return {"offset": int(offset or 0), "rows": out, "total": total[0] or 0, "anomalies": total[1] or 0,
                "dossiers": dossiers, "annees": annees}

    def anomalies_detail(self, chemin: str) -> dict:
        """Volet « Détail » : le fichier, toutes ses anomalies et de quoi l'afficher."""
        c = self.db.conn
        rows = c.execute(f"SELECT a.*, v.chemin IS NOT NULL AS vu FROM anomalies a {self._VU_JOIN} "
                         "WHERE COALESCE(a.chemin, '') = ? ORDER BY a.id", (chemin or "",)).fetchall()
        fic = c.execute("SELECT * FROM fichiers WHERE chemin = ?", (chemin,)).fetchone()
        exists = bool(chemin) and Path(chemin).is_file()
        preview = None
        if exists and chemin.lower().endswith(".pdf"):
            preview = chemin
        elif fic is not None and fic["cache_pdf"]:
            from .database import resolve_cache
            cand = resolve_cache(fic["cache_pdf"], Path(self.cache_dir))
            if cand and Path(cand).exists():
                preview = cand
        rel = c.execute("SELECT r.id FROM etudiants_releves r JOIN fichiers f ON f.id = r.fichier_id "
                        "WHERE f.chemin = ? ORDER BY r.numero_page LIMIT 1", (chemin,)).fetchone()
        return {"chemin": chemin, "nom": Path(chemin).name if chemin else "", "dossier": anomaly_causes.folder_of(chemin),
                "dossier_label": self._rel_folder(anomaly_causes.folder_of(chemin)), "existe": exists,
                "type_fichier": fic["type_fichier"] if fic else None, "nb_pages": fic["nb_pages"] if fic else None,
                "statut": fic["statut"] if fic else None, "preview_pdf": preview,
                "is_word": Path(chemin or "").suffix.lower() in (".doc", ".docx"),
                "releve_id": rel[0] if rel else None,
                "anomalies": [{"id": r["id"], "code": r["code"], "label": anomaly_causes.label(r["code"]),
                               "niveau": r["niveau"], "message": r["message"], "action": r["action"],
                               "horodatage": r["horodatage"], "vu": bool(r["vu"]),
                               "page": anomaly_causes.page_of(r["message"])} for r in rows]}

    def _anom_pairs(self, items: list[str] | None, filter: dict | None) -> list[tuple[str, str]]:
        """(fichier, cause) visés par une action de lot : les fichiers cochés (items), ou tout le filtre."""
        where, args = self._anom_where(filter)
        sql = (f"SELECT DISTINCT COALESCE(a.chemin, ''), a.code FROM anomalies a {self._VU_JOIN} WHERE {where}")
        c = self.db.conn
        if items is None:
            return [(p, code) for p, code in c.execute(sql, args).fetchall()]
        out: list[tuple[str, str]] = []
        items = [str(x) for x in items]
        for i in range(0, len(items), 400):
            chunk = items[i:i + 400]
            out += c.execute(sql + f" AND COALESCE(a.chemin, '') IN ({','.join('?' * len(chunk))})",
                             args + chunk).fetchall()
        return [(p, code) for p, code in out]

    def anomalies_mark_seen(self, items: list[str] | None = None, filter: dict | None = None,
                            seen: bool = True) -> dict:
        """« Marquer vus » (ou « à traiter » de nouveau) : la marque reste d'une passe à l'autre."""
        f = dict(filter or {})
        f["vue"] = "a_traiter" if seen else "vues"
        pairs = self._anom_pairs(items, f)
        db = self.db
        with db._lock:
            if seen:
                db.conn.executemany("INSERT OR IGNORE INTO anomalies_vues(chemin, code, operateur) VALUES (?,?,?)",
                                    [(p, code, self.s.operateur) for p, code in pairs])
            else:
                db.conn.executemany("DELETE FROM anomalies_vues WHERE chemin = ? AND code = ?", pairs)
            db.conn.commit()
        return {"n": len(pairs), "fichiers": len({p for p, _ in pairs}), "seen": bool(seen)}

    def anomalies_paths(self, items: list[str] | None = None, filter: dict | None = None) -> list[str]:
        """Fichiers à relire (« Relire », « Relire les 3 104 »)."""
        return sorted({p for p, _ in self._anom_pairs(items, filter) if p})

    def anomalies_export(self, path: str = "", items: list[str] | None = None, filter: dict | None = None) -> dict:
        """Export CSV (séparateur « ; », UTF-8 avec BOM pour Excel) des anomalies de la sélection."""
        import csv
        if not path:
            path = str(exports.dest_for(self.s, exports.JOURNAL, "Anomalies.csv"))
        where, args = self._anom_where(filter)
        sql = (f"SELECT a.*, v.chemin IS NOT NULL AS vu FROM anomalies a {self._VU_JOIN} WHERE {where}")
        c = self.db.conn
        rows: list = []
        if items is None:
            rows = c.execute(sql + " ORDER BY a.dossier, a.chemin, a.id", args).fetchall()
        else:
            items = [str(x) for x in items]
            for i in range(0, len(items), 400):
                chunk = items[i:i + 400]
                rows += c.execute(sql + f" AND COALESCE(a.chemin, '') IN ({','.join('?' * len(chunk))}) "
                                  "ORDER BY a.dossier, a.chemin, a.id", args + chunk).fetchall()
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["Niveau", "Cause", "Fichier", "Dossier", "Année", "Message", "Action", "Horodatage", "Vu"])
            for r in rows:
                w.writerow([r["niveau"], anomaly_causes.label(r["code"]), r["chemin"] or "",
                            self._rel_folder(r["dossier"]), r["annee"] or "", r["message"], r["action"] or "",
                            r["horodatage"], "oui" if r["vu"] else "non"])
        return {"path": str(path), "name": Path(path).name, "count": len(rows)}

    def index_exclude_folder(self, dossier: str, apply: bool = False) -> dict:
        """« Exclure ce dossier » : ajouté aux exclusions (enregistré tout de suite) ; ses fichiers et leurs
        relevés sont retirés de l'index maintenant (les archives ne sont jamais touchées)."""
        d = str(dossier or "").rstrip("\\/")
        if not d:
            raise ServiceError("Dossier à exclure manquant.")
        sep = "\\" if "\\" in d else "/"
        prefix = d + sep
        c = self.db.conn
        cond = "lower(substr(chemin, 1, ?)) = lower(?)"
        n_files = c.execute(f"SELECT COUNT(*) FROM fichiers WHERE {cond}", (len(prefix), prefix)).fetchone()[0]
        n_rel = c.execute(f"SELECT COUNT(*) FROM etudiants_releves r JOIN fichiers f ON f.id = r.fichier_id "
                          f"WHERE lower(substr(f.chemin, 1, ?)) = lower(?)", (len(prefix), prefix)).fetchone()[0]
        n_anom = c.execute(f"SELECT COUNT(*) FROM anomalies WHERE {cond}", (len(prefix), prefix)).fetchone()[0]
        res = {"dossier": d, "label": self._rel_folder(d), "fichiers": n_files, "releves": n_rel, "anomalies": n_anom,
               "deja": any(str(x).rstrip("\\/").casefold() == d.casefold() for x in self.s.exclusions)}
        if not apply:
            return res
        if not res["deja"]:
            self.s.exclusions = list(self.s.exclusions) + [d]
            save_settings(self.s)
        db = self.db
        with db._lock:
            cur = c.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.execute(f"DELETE FROM fichiers WHERE {cond}", (len(prefix), prefix))
                cur.execute(f"DELETE FROM anomalies WHERE {cond}", (len(prefix), prefix))
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        db._token_index = None
        self._log("INDEX EXCLUSION", None, f"Dossier exclu : {d} · {n_files} fichier(s), {n_rel} relevé(s) et "
                                           f"{n_anom} anomalie(s) retirés de l'index (archives non modifiées)")
        return dict(res, applique=True, exclusions=list(self.s.exclusions))

    # ---- revue demandes / cursus ----
    # Revue de la demande (contrôle à l'écran pièce par pièce, étapes horodatées, bordereau provisoire), page
    # Demandes (tableau de bord) et aperçu du cursus fusionné sans numéro de délivrance. Bloc délimité : les
    # autres chantiers (vérification, indexation, réglages) n'y touchent pas.
    CONTROLE = "CONTRÔLE VISUEL"
    CONTROLE_RETIRE = "CONTRÔLE VISUEL RETIRÉ"
    CONTROLE_OMIS = "CONTRÔLE VISUEL OMIS"

    def demande_review(self, did: int) -> dict:
        """La demande telle que la montre la revue : pièces (fichier préparé encore valable ou non), relevés
        contrôlés à l'écran (journal), heure de chaque étape, bordereau et dossier préparés."""
        res = self._demande(did)
        d = res["demande"]
        state: dict[int, bool] = {}
        for row in self.db.conn.execute(
                "SELECT action, releve_id FROM journal WHERE action IN (?, ?) AND detail LIKE ? ORDER BY id",
                (self.CONTROLE, self.CONTROLE_RETIRE, f"Demande {d['numero']} ·%")):
            state[row["releve_id"]] = row["action"] == self.CONTROLE
        present = {it["item"]["releve_id"] for it in res["items"]}
        prepared = d["statut"] in ("PRÊTE", "IMPRIMÉE", "REMISE")
        folder = Path(d["dossier"]) if d["dossier"] else None

        def exists(p) -> bool:
            try:
                return bool(p) and Path(p).exists()
            except OSError:
                return False
        for it in res["items"]:
            item = it["item"]
            it["prepared"] = bool(prepared and item["etat"] == "PRÊT" and exists(item["fichier"]))
        bord = folder / demandes.BORDEREAU if prepared and folder else None
        tout = folder / demandes.A_IMPRIMER if prepared and folder else None
        res["review"] = {
            "controles": sorted(i for i, ok in state.items() if ok and i in present),
            "etapes": self._demande_etapes(d),
            "bordereau": str(bord) if exists(bord) else None,
            "a_imprimer": str(tout) if exists(tout) else None,
            "pages": sum((it["releve"] or {}).get("nb_pages") or 0 for it in res["items"]),
        }
        return res

    def _demande_etapes(self, d) -> dict:
        """Heure de chaque étape (journal « DEMANDE ») ; seulement celles que le statut actuel a franchies
        (une demande modifiée après préparation repart « EN COURS » : l'ancienne heure ne vaut plus)."""
        seen: dict[str, str] = {}
        for row in self.db.conn.execute("SELECT horodatage, detail FROM journal WHERE action='DEMANDE' AND detail LIKE ? "
                                        "ORDER BY id", (f"{d['numero']}%",)):
            detail = row["detail"] or ""
            for key, mark in (("preparer", f"{d['numero']} préparée"), ("imprimer", f"{d['numero']} : imprimée"),
                              ("remettre", f"{d['numero']} : remise"), ("annuler", f"{d['numero']} : annulée")):
                if detail.startswith(mark):
                    seen[key] = row["horodatage"]
        rank = {"EN COURS": 0, "PRÊTE": 1, "IMPRIMÉE": 2, "REMISE": 3, "ANNULÉE": 0}.get(d["statut"], 0)
        out = {"recue": d["creee_le"], "preparer": None, "imprimer": None, "remettre": None, "annuler": None}
        for k, need in (("preparer", 1), ("imprimer", 2), ("remettre", 3)):
            if rank >= need:
                out[k] = seen.get(k)
        if d["statut"] == "ANNULÉE":
            out["annuler"] = seen.get("annuler")
        return out

    def demande_control(self, did: int, releve_id: int, checked: bool = True) -> dict:
        """Coche « contrôlé à l'écran » d'un relevé de la demande : tracée au journal comme le contrôle visuel
        du guichet (retirer la coche est tracé aussi)."""
        d = self.db.demande(int(did))
        if d is None:
            raise ServiceError("Demande introuvable.")
        rel = self.db.get(int(releve_id))
        what = f"{rel.periode} · {rel.annee_academique or '?'}" if rel else "relevé retiré de l'index"
        self.db.log(self.CONTROLE if checked else self.CONTROLE_RETIRE, self.s.operateur, self.s.poste, int(releve_id),
                    rel.matricule if rel else d["matricule"],
                    f"Demande {d['numero']} · {what} · " + ("contrôlé à l'écran par l'agent avant impression" if checked
                                                            else "coche « contrôlé à l'écran » retirée"))
        return self.demande_review(did)

    def demande_control_skipped(self, did: int, releve_ids: list[int]) -> dict:
        """« Imprimer tout » confirmé alors que des relevés n'ont pas été contrôlés à l'écran : c'est tracé."""
        d = self.db.demande(int(did))
        if d is None:
            raise ServiceError("Demande introuvable.")
        for rid in releve_ids or []:
            rel = self.db.get(int(rid))
            what = f"{rel.periode} · {rel.annee_academique or '?'}" if rel else "relevé retiré de l'index"
            self.db.log(self.CONTROLE_OMIS, self.s.operateur, self.s.poste, int(rid), rel.matricule if rel else d["matricule"],
                        f"Demande {d['numero']} · {what} · impression confirmée par l'agent sans contrôle à l'écran")
        return {"logged": len(releve_ids or [])}

    def demande_bordereau_preview(self, did: int) -> dict:
        """Bordereau provisoire (avant préparation) pour l'aperçu de la revue ; refait seulement si la demande a
        changé. Fichier temporaire : aucun numéro de délivrance réservé, rien au journal."""
        d = self.db.demande(int(did))
        if d is None:
            raise ServiceError("Demande introuvable.")
        items = tuple((item["releve_id"], item["ordre"]) for item, _rel in self.db.demande_items(int(did)))
        sig = (d["maj_le"], d["demandeur"], d["motif"], d["duplicata"], d["nom"], d["matricule"], items)
        with self._ops_lock:
            cache = self.__dict__.setdefault("_bordereaux", {})
            hit = cache.get(int(did))
        if hit and hit[0] == sig and Path(hit[1]).exists():
            return {"path": hit[1], "provisoire": True}
        dest = pdf_tools.temp_pdf("bordereau_")
        demandes.bordereau_provisoire(self.db, self.s, int(did), dest)
        with self._ops_lock:
            old = cache.get(int(did))
            cache[int(did)] = (sig, str(dest))
        if old:
            _unlink_temp(old[1])
        return {"path": str(dest), "provisoire": True}

    def demandes_board(self, statuts: list[str] | None = None) -> list[dict]:
        """Page Demandes : chaque demande avec ses pages, ses relevés bloqués et la date de sa dernière étape."""
        rows = [_row(r) for r in self.db.demandes(statuts or None)]
        if not rows:
            return rows
        extra = {r["did"]: r for r in self.db.conn.execute(
            "SELECT i.demande_id AS did, SUM(CASE WHEN i.etat LIKE 'BLOQU%' THEN 1 ELSE 0 END) AS bloques, "
            "SUM(CASE WHEN r.id IS NULL THEN 0 ELSE r.page_fin - r.numero_page + 1 END) AS pages "
            "FROM demande_items i LEFT JOIN etudiants_releves r ON r.id = i.releve_id GROUP BY i.demande_id")}
        for r in rows:
            e = extra.get(r["id"])
            r["bloques"] = int(e["bloques"] or 0) if e else 0
            r["pages"] = int(e["pages"] or 0) if e else 0
        return rows

    def _cursus_rels(self, releve_ids: list[int]) -> list[Releve]:
        """Relevés cochés, un seul exemplaire par relevé, dans l'ordre du cursus."""
        rels = [r for r in (self.db.get(int(i)) for i in releve_ids) if r]
        if not rels:
            raise ServiceError("Cochez au moins un relevé.")
        rels = self._exemplaires_uniques(rels)
        rels.sort(key=lambda r: (r.niveau_num, r.semestre or 0, r.annee_academique or "",
                                 1 if (r.session or "").lower().startswith("rattrap") else 0))
        return rels

    def _exemplaires_uniques(self, rels: list[Releve]) -> list[Releve]:
        """Deux exemplaires du même relevé cochés : fusionné une seule fois. L'exemplaire gardé est celui que
        l'agent a coché (choix « exemplaires multiples » de la revue du cursus), pas d'office celui de l'index."""
        groups, _info = self.db._dup_groups({r.matricule for r in rels})
        out, seen = [], set()
        for r in rels:
            key = (groups.get(r.id) or [r.id])[0]
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out

    def _cursus_preview(self, matricule: str, releve_ids: list[int], cover: bool, duplicata: bool,
                        op: str | None) -> dict:
        """Aperçu du PDF fusionné tel qu'il sortira (page de garde, ordre, timbre, pied de page) : mêmes
        conversions Word que l'impression (une par fichier source, progression, annulation) — elles servent
        ensuite à l'impression. Aucun numéro de délivrance réservé, rien au journal ; mis en cache par sélection."""
        rels = self._cursus_rels(releve_ids)
        with self._operation(op) as cancelled:
            ready, errors = self._prepare_word(rels, op, cancelled, "Aperçu du cursus")
            self._progress(op, "fusion", len(rels), len(rels), "Contrôle et fusion de l'aperçu…")
            base = self.db.cursus(matricule) or rels
            absent = missing_periods(base)

            def sig_of(rel: Releve):
                r = ready.get(rel.id, rel)
                try:
                    mtime = Path(r.preview_pdf).stat().st_mtime if r.preview_pdf else 0
                except OSError:
                    mtime = 0
                return (r.id, r.preview_pdf, mtime, r.numero_page, r.page_fin)
            sig = (tuple(sig_of(r) for r in rels), bool(cover), bool(duplicata), tuple(sorted(errors.items())),
                   tuple(absent), bool(self.s.delivery_ref_stamp))
            with self._ops_lock:
                cache = self.__dict__.setdefault("_cursus_previews", {})
                hit = cache.get(sig)
            if hit and Path(hit["path"]).exists():
                return dict(hit, cached=True)

            def prepare(rel):
                if rel.id in errors:
                    raise ExtractionError(errors[rel.id])
                rel = ready.get(rel.id, rel)
                check_before_print(rel)
                return rel
            dest = pdf_tools.temp_pdf("cursus_apercu_")
            layout: list[dict] = []
            try:
                path, n, missing = pdf_tools.merge_releves(
                    rels, dest, cover=cover, stamp=pdf_tools.stamp_text() if duplicata else None, prepare=prepare,
                    absent=absent, layout=layout,
                    footer=pdf_tools.footer_text("(attribuée à l'impression)") if self.s.delivery_ref_stamp else None)
            except ValueError as exc:
                _unlink_temp(str(dest))
                raise ServiceError(f"Aperçu impossible : {exc}") from exc
        out = {"path": str(path), "name": Path(path).name, "pages": n, "missing": missing, "ref": None,
               "preview": True, "layout": layout, "location": ""}
        with self._ops_lock:
            cache[sig] = out
            while len(cache) > 6:
                old = cache.pop(next(iter(cache)))
                _unlink_temp(old["path"])
        return dict(out, cached=False)


def _unlink_temp(file: str | None) -> None:
    """Fichier temporaire d'impression : supprimé seulement s'il est dans le dossier temporaire de Scolarix."""
    if not file:
        return
    try:
        p = Path(file)
        if str(p).startswith(str(pdf_tools.TEMP_DIR)):
            p.unlink()
    except OSError:
        pass
