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
        anomalies = db.conn.execute(
            "SELECT COUNT(*) FROM anomalies WHERE niveau IN ('ERREUR','AVERTISSEMENT')").fetchone()[0]
        return {"releves": st["releves"], "etudiants": st["etudiants"], "fichiers": st["fichiers"],
                "pages": st["pages"], "echecs": st["echecs"], "multi_pages": st.get("multi_pages", 0), "formats": st["formats"], "annees": st["annees"],
                "imprimes": counts.get("IMPRESSION", 0) + counts.get("DUPLICATA", 0),
                "duplicatas": counts.get("DUPLICATA", 0), "anomalies": anomalies,
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
        écarté avec son motif, sans bloquer les autres. Le numéro de délivrance n'est réservé qu'ensuite."""
        rels = [r for r in (self.db.get(int(i)) for i in releve_ids) if r]
        if not rels:
            raise ServiceError("Cochez au moins un relevé.")
        rels = self.db.collapse(rels)      # deux exemplaires du même relevé cochés : fusionné une seule fois
        rels.sort(key=lambda r: (r.niveau_num, r.semestre or 0, r.annee_academique or "",
                                 1 if (r.session or "").lower().startswith("rattrap") else 0))
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
        if "duplicata" in allowed and bool(allowed["duplicata"]) != bool(d["duplicata"]) and d["statut"] == "PRÊTE":
            allowed["statut"] = "EN COURS"      # le timbre change : le dossier préparé est à refaire
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
        missing = [r["chemin"] for r in self.db.orphan_files()]
        for chemin in missing:
            self.db.add_anomaly("AVERTISSEMENT", chemin, "Lien orphelin : fichier déplacé, renommé ou supprimé.",
                                "Relocaliser")
        return {"orphelins": len(missing), "fichiers": missing[:50]}

    def anomalies(self, niveau: str | None = None) -> list[dict]:
        return [_row(r) for r in self.db.anomalies(niveau or None, 500)]

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
        ident = verification.identify(verification.read_submitted(path, self.s))
        mat = (delivered or {}).get("matricule") or ident.get("matricule")
        rels = self.db.cursus(mat) if mat else []
        target = None
        if delivered and delivered.get("releve_id"):
            target = next((r for r in rels if r.id == delivered["releve_id"]), None)
        target = target or verification.best_match(rels, ident)
        return {"delivered": delivered, "ident": ident, "matricule": mat,
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
        sub_text = verification.read_submitted(path, self.s)
        pages = pdf_pages_text(Path(orig.preview_pdf)) if orig.preview_pdf else []
        orig_text = "\n".join(p.text for p in pages[orig.numero_page - 1:orig.page_fin])
        rows = verification.compare(sub_text, orig_text, orig)
        res = {"original": releve_dict(orig), "rows": [dict(dataclasses.asdict(r), conforme=r.conforme) for r in rows],
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

    # ---- réglages / extraction ----
    # Feuille des Réglages (outils du poste, diagnostic, aperçu AVANT / APRÈS des remplacements) et écrans
    # Extraction / Journal des actions (toutes les lignes, filtres avec effectifs). Bloc délimité, en fin de classe.

    # .................................................................. outils du poste, diagnostic
    def tools_status(self, tesseract_cmd: str | None = None, soffice_cmd: str | None = None,
                     probe_word: bool = False) -> dict:
        """Tesseract / LibreOffice / Word : trouvés ou non (chemin, version) et ce que leur absence coûte, chiffré
        d'après l'index. ``tesseract_cmd`` / ``soffice_cmd`` : chemins saisis mais pas encore enregistrés."""
        from . import poste
        c = self.db.conn
        one = lambda sql: c.execute(sql).fetchone()[0] or 0  # noqa: E731
        fmt = {k: n for k, n in c.execute("SELECT type_fichier, COUNT(*) FROM fichiers GROUP BY type_fichier")}
        return {
            "tesseract": poste.tesseract_status(self.s.tesseract_cmd if tesseract_cmd is None else tesseract_cmd),
            "libreoffice": poste.soffice_status(self.s.soffice_cmd if soffice_cmd is None else soffice_cmd),
            "word": poste.word_status(self.s.word_com_enabled, bool(probe_word)),
            "ocr_enabled": bool(self.s.ocr_enabled),
            "counts": {"scans_en_attente": one("SELECT COUNT(DISTINCT chemin) FROM anomalies "
                                               "WHERE message LIKE 'PDF scanné non lu%'"),
                       "scans": fmt.get("PDF_SCAN", 0), "doc": fmt.get("DOC", 0), "docx": fmt.get("DOCX", 0),
                       "anomalies": one("SELECT COUNT(*) FROM anomalies")},
        }

    def diagnostic(self) -> dict:
        """Versions, dossiers et fichiers du poste (à transmettre à la Cellule informatique)."""
        from . import poste
        from .config import data_dir
        db_size = poste.file_size(self.db_path)
        cache_bytes, cache_n = poste.dir_size(Path(self.cache_dir))
        _b, previews = poste.dir_size(Path(self.cache_dir), ".pdf")
        try:
            exports_dir = str(self.s.exports_root_dir())
        except OSError:
            exports_dir = ""
        st = self.db.stats()
        return {"app": APP_NAME, "version": APP_VERSION, **poste.runtime(),
                "data_dir": str(data_dir()), "logs_dir": str(data_dir() / "logs"), "config_path": str(config_path()),
                "db_path": str(self.db_path), "db_bytes": db_size, "db_reseau": is_network_path(str(self.db_path)),
                "cache_dir": str(self.cache_dir), "cache_bytes": cache_bytes, "cache_fichiers": cache_n,
                "cache_apercus": previews, "exports_dir": exports_dir, "releves": st["releves"],
                "fichiers": st["fichiers"], "derniere_indexation": self.db.get_meta("derniere_indexation"),
                "convertisseur": self._rp_converter_name(), "regles_actives": self.rules_active,
                "config_db_path": self.s.db_path, "config_cache_dir": self.s.cache_dir}

    def cache_clear(self, confirm: str = "") -> dict:
        """« Vider le cache d'aperçu » : les aperçus Word seront refaits à la consultation (l'index reste)."""
        if confirm != "VIDER":
            raise ServiceError("Confirmation manquante.")
        import shutil
        removed = kept = 0
        cache = Path(self.cache_dir)
        if cache.exists():
            for f in cache.iterdir():
                try:
                    shutil.rmtree(f) if f.is_dir() else f.unlink()
                    removed += 1
                except OSError:
                    kept += 1          # aperçu ouvert en ce moment : il sera remplacé plus tard
        self._log("CACHE VIDÉ", None, f"{removed} élément(s) du cache d'aperçu supprimés"
                                      + (f", {kept} en cours d'utilisation conservés" if kept else ""))
        return {"supprimes": removed, "conserves": kept}

    # .................................................................. remplacements : aperçu AVANT / APRÈS
    def _rp_converter_name(self) -> str:
        from .extractors import WordConverter
        try:
            return WordConverter(self.s).backend()
        except Exception:
            return "aucun"

    def rules_preview_releves(self, q: str = "", limit: int = 40) -> list[dict]:
        """Relevés Word proposés pour l'essai des règles : un par fichier (ou ceux du matricule / nom cherché)."""
        sql = ("SELECT r.id, r.matricule, r.nom_complet, r.niveau, r.annee_academique, r.semestre, f.type_fichier, "
               "f.chemin FROM etudiants_releves r JOIN fichiers f ON f.id = r.fichier_id "
               "WHERE f.type_fichier IN ('DOC', 'DOCX')")
        args: list = []
        q = (q or "").strip()
        if q:
            sql += " AND (r.matricule LIKE ? OR r.nom_norm LIKE ?)"
            args += [f"%{norm_matricule(q)}%", f"%{norm_text(q)}%"]
        else:
            sql += " AND r.id IN (SELECT MIN(r2.id) FROM etudiants_releves r2 GROUP BY r2.fichier_id)"
        sql += " ORDER BY r.annee_academique DESC, r.matricule LIMIT ?"
        args.append(max(1, min(int(limit), 200)))
        out = []
        for row in self.db.conn.execute(sql, args).fetchall():
            ext = ".doc" if row["type_fichier"] == "DOC" else ".docx"
            label = " · ".join(x for x in (row["matricule"], row["niveau"] or "", row["annee_academique"] or "") if x)
            out.append({"id": row["id"], "label": f"{label} ({ext})", "nom": row["nom_complet"],
                        "fichier": Path(row["chemin"]).name})
        return out

    def _rp_work(self, rel: Releve, converter=None) -> Path | None:
        """.docx de travail du relevé : l'original (.docx) ou la conversion en cache d'un .doc ; None si elle
        n'existe pas encore et qu'aucun convertisseur n'est fourni."""
        from .preview import ensure_intermediate_docx, intermediate_docx
        src = Path(rel.chemin)
        if not src.exists():
            raise ServiceError(f"Fichier source introuvable : {src.name}")
        if rel.type_fichier == "DOCX":
            return src
        inter = intermediate_docx(rel.chemin, Path(self.cache_dir))
        if converter is not None:
            return ensure_intermediate_docx(rel.chemin, Path(self.cache_dir), converter)
        return inter if inter.exists() else None

    def _rp_word_thread(self, fn):
        """Exécute ``fn(convertisseur)`` dans le fil des conversions de l'aperçu (Word exige toujours le même fil)."""
        from concurrent.futures import TimeoutError as FuturesTimeout

        from .extractors import WordConverter
        pv = self.previews

        def job():
            if pv._converter is None:
                pv._converter = WordConverter(self.s)
            return fn(pv._converter)
        try:
            return pv._pool.submit(job).result(timeout=pv.job_timeout)
        except FuturesTimeout as exc:
            raise ServiceError("Le convertisseur ne répond plus : aperçu des règles abandonné.") from exc

    def rules_preview(self, rules: list, releve_id: int | None = None, text: str | None = None,
                      rendu: bool = False, req: int = 0) -> dict:
        """Aperçu AVANT / APRÈS des règles (rien n'est enregistré). Sans ``releve_id`` : sur le texte libre.
        Avec : sur l'en-tête du relevé Word — rendu typographique immédiat, puis (``rendu``) rendu du moteur :
        copie corrigée convertie par Word / LibreOffice, haut de la page en image, textes retirés / ajoutés situés,
        et verdict du contrôle de fidélité de l'aperçu réel. ``req`` croissant : un rendu dépassé est abandonné."""
        from . import rules_preview as rp
        compiled = self._check_rules(rules)
        warnings = rule_warnings(compiled)
        active = len(compiled)
        if releve_id in (None, "", 0):
            avant, apres, out, n = rp.typographic_text(text or "", compiled)
            return {"mode": "texte", "avant": avant, "apres": apres, "text": out, "count": n, "regles": active,
                    "avertissements": warnings, "ecartees": []}
        rel = self._get(int(releve_id))
        if not rel.is_word:
            raise ServiceError("L'essai des règles se fait sur un relevé Word (.doc / .docx).")
        label = f"{rel.matricule} · {rel.niveau or '?'} · {rel.annee_academique or '?'}"
        base = {"mode": "releve", "releve_id": rel.id, "releve": label, "fichier": Path(rel.chemin).name,
                "regles": active, "avertissements": warnings}
        if not rendu:
            work = self._rp_work(rel)
            if work is None:
                return dict(base, count=None, avant=[], apres=[], ecartees=[],
                            attente="Fichier .doc pas encore converti : l'en-tête apparaît avec le rendu du moteur.")
            copy, report, n = rp.rewrite_copy(work, compiled)
            try:
                avant, apres = rp.typographic_releve(work, copy, rel.matricule, rel.numero_page)
            finally:
                copy.unlink(missing_ok=True)
            return dict(base, count=n, avant=avant, apres=apres, ecartees=report.skipped)
        # ---- rendu du moteur
        self._rp_latest = max(int(req or 0), getattr(self, "_rp_latest", 0))
        if self.previews is None or not self._rp_converter_name() or self._rp_converter_name() == "aucun":
            return dict(base, rendu=None, sans_convertisseur=True)
        from . import layout_check
        from .preview import _signature, remap_pages, render_original

        def job(conv):
            if int(req or 0) < getattr(self, "_rp_latest", 0):
                return None                                  # réglage déjà modifié : rendu inutile
            work = self._rp_work(rel, conv)
            copy, report, n = rp.rewrite_copy(work, compiled)
            try:
                before = render_original(rel.chemin, rel.type_fichier, self.s, conv, work)
                key = rp.render_key(rel.chemin, _signature(Path(work)), rules)
                after = rp.cached_render(conv, copy, key, Path(rel.chemin).name) if n else before
            finally:
                copy.unlink(missing_ok=True)
            return before, after, report, n
        res = self._rp_word_thread(job)
        if res is None:
            return dict(base, stale=True)
        before, after, report, n = res
        siblings = self.db.releves_of_file(rel.fichier_id)
        pb = remap_pages(before, siblings, rel.chemin)[0].get(rel.id, (rel.numero_page, rel.page_fin))[0]
        pa = remap_pages(after, siblings, rel.chemin)[0].get(rel.id, (pb, pb))[0] if n else pb
        removed, added = rp.changed_texts(report.replacements)
        img_b = rp.crop_page(before, pb, rel.matricule, removed, [])
        img_a = rp.crop_page(after, pa, rel.matricule, [], added, frac=img_b["frac"], top=img_b["top"])
        motif = None
        if n:
            try:
                motif = layout_check.compare(before, after, report.replacements)
            except Exception as exc:
                motif = f"contrôle du rendu impossible ({exc.__class__.__name__})"
        return dict(base, count=n, ecartees=report.skipped, motif=motif, convertisseur=self._rp_converter_name(),
                    rendu={"avant": img_b, "apres": img_a})

    # .................................................................. extraction : toutes les lignes, effectifs
    EXT_MENTIONS = ("Excellent", "Très bien", "Bien", "Assez bien", "Passable", "Médiocre", "Éliminé")
    EXT_COLS = ("id", "matricule", "nom", "fil", "annee", "periode", "moyenne", "mention", "statut", "niveau")
    EXT_EXTRA = {"session": lambda r: r.session or "", "decision": lambda r: r.decision or "",
                 "credits": lambda r: r.credits if r.credits is not None else "", "grade": lambda r: r.grade or "",
                 "mgp": lambda r: r.mgp or "", "naissance": lambda r: r.date_connue or ""}

    def _ext_base(self) -> list[tuple]:
        """Tous les relevés (un exemplaire par relevé, ordre de Database.extraction), avec ce qu'il faut pour
        filtrer vite : (relevé, année, mention, filière, admis). Gardé tant que l'index ne change pas."""
        from . import exporter
        sig = tuple(self.db.conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM etudiants_releves").fetchone()) \
            + (self.db.get_meta("derniere_indexation"),)
        lock = self.__dict__.setdefault("_ext_lock", threading.Lock())
        with lock:
            cache = getattr(self, "_ext_cache", None)
            if cache and cache[0] == sig:
                return cache[1]
            out = []
            for r in self.db.extraction(None, None, None, "tous", False):
                a = r.annee_academique or ""
                y = int(a[:4]) if a[:4].isdigit() else None
                out.append((r, y, exporter.mention(r.moyenne, r.grade), r.filiere_code, r.is_success))
            self._ext_cache = (sig, out)
            return out

    @staticmethod
    def _ext_last(items: list[tuple]) -> list[tuple]:
        """« Dernier niveau atteint » : même choix que Database.extraction (dernier relevé de chaque étudiant)."""
        best: dict[str, tuple] = {}
        for it in items:
            r = it[0]
            cur = best.get(r.matricule)
            if cur is None or (r.niveau_num, r.semestre or 0, r.annee_academique or "", it[4]) > \
                    (cur[0].niveau_num, cur[0].semestre or 0, cur[0].annee_academique or "", cur[4]):
                best[r.matricule] = it
        return sorted(best.values(), key=lambda it: (it[0].annee_academique or "", it[0].nom_norm))

    def _ext_rows(self, filiere=None, year_from=None, year_to=None, statut="tous", dernier_niveau=True,
                  niveaux=None, mentions=None, base=None, skip: str = "") -> list[tuple]:
        base = self._ext_base() if base is None else base
        y1, y2 = (year_from or None), (year_to or None)
        items = [it for it in base if (not filiere or it[3] == filiere)
                 and (y1 is None or (it[1] is not None and it[1] >= y1))
                 and (y2 is None or (it[1] is not None and it[1] <= y2))]
        if dernier_niveau:
            items = self._ext_last(items)
        return self._ext_post(items, statut, niveaux, mentions, skip)

    @staticmethod
    def _ext_post(items, statut, niveaux, mentions, skip: str = "") -> list[tuple]:
        nv = set(int(x) for x in niveaux) if niveaux and skip != "niveaux" else None
        mt = set(mentions) if mentions is not None and skip != "mentions" else None
        st = statut if skip != "statut" else "tous"
        return [it for it in items
                if (st == "tous" or (st == "admis") == it[4])
                and (nv is None or it[0].niveau_num in nv)
                and (mt is None or (it[2] or "Sans mention") in mt)]

    def extraction_view(self, filiere: str | None = None, year_from: int | None = None, year_to: int | None = None,
                        statut: str = "tous", dernier_niveau: bool = True, niveaux: list | None = None,
                        mentions: list | None = None, extra_cols: list | None = None) -> dict:
        """Extraction Décanat sans plafond : toutes les lignes (colonnes compactes), effectifs de chaque filtre
        (filière, année, niveau, statut, mention : calculés sans ce filtre-là, avec les autres)."""
        t0 = time.perf_counter()
        base = self._ext_base()
        f = dict(filiere=filiere or None, year_from=year_from, year_to=year_to, statut=statut or "tous",
                 dernier_niveau=bool(dernier_niveau), niveaux=niveaux or None, mentions=mentions)
        y1, y2 = (year_from or None), (year_to or None)
        in_years = [it for it in base if (y1 is None or (it[1] is not None and it[1] >= y1))
                    and (y2 is None or (it[1] is not None and it[1] <= y2))]
        last = self._ext_last if f["dernier_niveau"] else (lambda items: items)
        # effectifs par filière : le « dernier niveau atteint » est choisi DANS chaque filière (Database.extraction)
        by_fil: dict = {}
        for it in in_years:
            by_fil.setdefault(it[3], []).append(it)
        post = lambda items, skip="": self._ext_post(items, f["statut"], f["niveaux"], mentions, skip)  # noqa: E731
        fil_counts = {code or "?": len(post(last(items))) for code, items in by_fil.items()}
        everyone = last(in_years)
        scoped = last(by_fil.get(f["filiere"], [])) if f["filiere"] else everyone
        rows = post(scoped)
        # effectifs par année : sans le filtre des années
        per_year: dict[str, int] = {}
        for it in self._ext_rows(**dict(f, year_from=None, year_to=None), base=base):
            a = it[0].annee_academique or "?"
            per_year[a] = per_year.get(a, 0) + 1
        niv: dict[int, int] = {}
        for it in post(scoped, "niveaux"):
            niv[it[0].niveau_num] = niv.get(it[0].niveau_num, 0) + 1
        men: dict[str, int] = {}
        for it in post(scoped, "mentions"):
            men[it[2] or "Sans mention"] = men.get(it[2] or "Sans mention", 0) + 1
        sts = post(scoped, "statut")
        n_ok = sum(1 for it in sts if it[4])
        extra = [c for c in (extra_cols or []) if c in self.EXT_EXTRA]
        mi = {m: i for i, m in enumerate(self.EXT_MENTIONS)}
        data = [[r.id, r.matricule, r.nom_complet, r.filiere_code or "", r.annee_academique or "", r.periode,
                 r.moyenne or "", mi.get(m, -1), r.statut or "", r.niveau_num]
                + [self.EXT_EXTRA[c](r) for c in extra] for r, _y, m, _f, _ok in rows]
        today = datetime.now()
        dest = str(Path(exports.EXTRACTIONS) / f"{today:%Y}" / f"{today:%m}")   # sans créer le dossier
        return {"count": len(rows), "cols": list(self.EXT_COLS) + extra, "rows": data,
                "mentions": men, "mention_order": list(self.EXT_MENTIONS), "per_year": sorted(per_year.items()),
                "filieres": fil_counts, "filieres_total": len(post(everyone)), "niveaux": niv,
                "statuts": {"tous": len(sts), "admis": n_ok, "non_valides": len(sts) - n_ok},
                "destination": dest, "ms": round((time.perf_counter() - t0) * 1000)}

    def extraction_view_export(self, kind: str, path: str | None, scope: str, filiere: str | None = None,
                               year_from: int | None = None, year_to: int | None = None, statut: str = "tous",
                               dernier_niveau: bool = True, niveaux: list | None = None,
                               mentions: list | None = None) -> dict:
        """Export Excel / CSV des lignes affichées (mêmes filtres, mentions et niveaux compris), journalisé."""
        from . import exporter
        rows = [it[0] for it in self._ext_rows(filiere or None, year_from, year_to, statut or "tous",
                                               bool(dernier_niveau), niveaux or None, mentions)]
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
        return {"path": path, "name": Path(path).name, "count": len(rows),
                "location": exports.short_location(self.s, Path(path))}

    def extraction_recent(self, limit: int = 4) -> list[dict]:
        """« Dernières extractions » (journal EXTRACTION) : périmètre, date, lignes, format, fichier."""
        import re
        rx = re.compile(r"^(.*) · ([^·]+) · (\d+) lignes · (.+?) · SHA-256")
        out = []
        for row in self.db.conn.execute("SELECT horodatage, operateur, detail FROM journal WHERE action = 'EXTRACTION' "
                                        "ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 20)),)):
            m = rx.match(row["detail"] or "")
            if not m:
                continue
            path = m.group(4)
            out.append({"horodatage": row["horodatage"], "operateur": row["operateur"], "scope": m.group(1),
                        "statut": m.group(2), "lignes": int(m.group(3)), "path": path,
                        "format": "Excel" if path.lower().endswith(".xlsx") else "CSV",
                        "existe": Path(path).exists()})
        return out

    # .................................................................. journal des actions : filtres, pages
    def _journal_where(self, period: str | None, date_from: str | None, date_to: str | None,
                       actions: list | None, operateur: str | None, q: str | None, skip: str = "",
                       sans_actions: list | None = None) -> tuple[str, list]:
        where, args = ["1=1"], []
        if skip != "period":
            if date_from or date_to:
                if date_from:
                    where.append("horodatage >= ?")
                    args.append(str(date_from)[:10])
                if date_to:
                    where.append("horodatage <= ?")
                    args.append(str(date_to)[:10] + " 23:59:59")
            elif period not in (None, ""):
                from datetime import timedelta
                where.append("horodatage >= ?")
                args.append((datetime.now() - timedelta(days=int(period))).strftime("%Y-%m-%d"))
        if actions is not None and skip != "actions":
            where.append(f"action IN ({','.join('?' * len(actions))})" if actions else "0")
            args.extend(actions)
        if sans_actions and skip != "actions":      # types décochés : un type nouveau (INDEX VIDÉ…) reste affiché
            where.append(f"action NOT IN ({','.join('?' * len(sans_actions))})")
            args.extend(sans_actions)
        if operateur and skip != "operateur":
            where.append("(COALESCE(operateur, '') || ' · ' || COALESCE(poste, '')) = ?")
            args.append(operateur)
        if (q or "").strip():
            like = f"%{q.strip()}%"
            where.append("(matricule LIKE ? OR operateur LIKE ? OR poste LIKE ? OR detail LIKE ? OR action LIKE ?)")
            args += [f"%{norm_matricule(q)}%", like, like, like, like]
        return " AND ".join(where), args

    def journal_view(self, period: str | None = "7", date_from: str | None = None, date_to: str | None = None,
                     actions: list | None = None, operateur: str | None = None, q: str | None = None,
                     offset: int = 0, limit: int = 500, facets: bool = True, sans_actions: list | None = None) -> dict:
        """Journal des actions par pages (aucun plafond) ; effectifs par période, action et opérateur."""
        c = self.db.conn
        where, args = self._journal_where(period, date_from, date_to, actions, operateur, q, sans_actions=sans_actions)
        total = c.execute(f"SELECT COUNT(*) FROM journal WHERE {where}", args).fetchone()[0]
        rows = [_row(r) for r in c.execute(f"SELECT * FROM journal WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                                           args + [max(1, min(int(limit), 5000)), max(0, int(offset))])]
        res = {"total": total, "offset": int(offset), "rows": rows}
        if facets:
            w, a = self._journal_where(period, date_from, date_to, actions, operateur, q, "actions", sans_actions)
            res["actions"] = [list(x) for x in c.execute(
                f"SELECT action, COUNT(*) FROM journal WHERE {w} GROUP BY action ORDER BY 2 DESC", a)]
            w, a = self._journal_where(period, date_from, date_to, actions, operateur, q, "operateur", sans_actions)
            res["operateurs"] = [list(x) for x in c.execute(
                f"SELECT COALESCE(operateur, '') || ' · ' || COALESCE(poste, ''), COUNT(*) FROM journal WHERE {w} "
                "GROUP BY 1 ORDER BY 2 DESC", a)]
            periods = {}
            for p in ("0", "7", "30", ""):
                w, a = self._journal_where(p, None, None, actions, operateur, q, sans_actions=sans_actions)
                periods[p] = c.execute(f"SELECT COUNT(*) FROM journal WHERE {w}", a).fetchone()[0]
            res["periodes"] = periods
        return res

    def journal_view_export(self, path: str, period: str | None = "7", date_from: str | None = None,
                            date_to: str | None = None, actions: list | None = None, operateur: str | None = None,
                            q: str | None = None, sans_actions: list | None = None) -> dict:
        """« Exporter (CSV) » du journal : toutes les lignes de la période et des filtres affichés."""
        from . import exporter
        if not path:
            path = str(exports.dest_for(self.s, exports.JOURNAL, "Journal.csv"))
        where, args = self._journal_where(period, date_from, date_to, actions, operateur, q, sans_actions=sans_actions)
        rows = self.db.conn.execute(f"SELECT * FROM journal WHERE {where} ORDER BY id DESC", args).fetchall()
        exporter.export_journal_csv(rows, path)
        return {"path": str(path), "name": Path(path).name, "count": len(rows)}


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
