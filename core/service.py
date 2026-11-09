"""Services du guichet, sans aucune interface : ce que le moteur expose à l'interface Electron.

Chaque méthode publique prend et renvoie des types JSON (dict, list, str, nombres). Les opérations longues
(aperçu Word, fusion, préparation de demande) sont bloquantes : le moteur les exécute dans ses fils de travail,
jamais dans l'interface — c'est ce qui empêche l'application de se figer.

Une connexion SQLite par fil (threading.local) : plusieurs demandes simultanées sans se gêner.
"""
from __future__ import annotations

import contextlib
import dataclasses
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
from .normalize import best_display_name, looks_like_matricule, norm_matricule, norm_text
from .text_rules import RuleError, apply_text, compile_rules

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
        return {"releve": releve_dict(rel), "siblings": [releve_dict(s) for s in siblings],
                "alerts": [{"text": t, "tone": tone} for t, tone in alerts.compute(rel, siblings, self.rules_active)],
                "needs_render": needs, "preview_pdf": None if needs else rel.preview_pdf}

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
        return self.releve(rel.id)

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
              "ui_scale", "delivery_ref_stamp", "docx_convert_at_index", "ocr_preprocess", "word_com_enabled",
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
        out, n = apply_text(text or "", self._check_rules(rules))
        return {"text": out, "count": n}


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
