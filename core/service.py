"""Services du guichet, sans aucune interface : ce que le moteur expose à l'interface Electron.

Chaque méthode publique prend et renvoie des types JSON (dict, list, str, nombres). Les opérations longues
(aperçu Word, fusion, préparation de demande) sont bloquantes : le moteur les exécute dans ses fils de travail,
jamais dans l'interface — c'est ce qui empêche l'application de se figer.

Une connexion SQLite par fil (threading.local) : plusieurs demandes simultanées sans se gêner.
"""
from __future__ import annotations

import dataclasses
import threading
import time
from datetime import datetime
from pathlib import Path

from . import alerts, demandes, exports, pdf_tools, verification
from .config import APP_NAME, APP_VERSION, UI_SCALES, Settings, save_settings
from .database import Database, Releve, missing_periods
from .guard import PrintBlocked, VisualCheckRequired, check_before_print
from .normalize import norm_text

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
    return d


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


class Service:
    def __init__(self, settings: Settings, previews=None, db_factory=None):
        self.s = settings
        self.previews = previews
        self._local = threading.local()
        self._db_factory = db_factory or (lambda: Database(self.s.resolved_db_path(),
                                                            cache_dir=self.s.resolved_cache_dir()))

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

    # ------------------------------------------------------------------ accueil
    def info(self) -> dict:
        return {"app": APP_NAME, "version": APP_VERSION, "operateur": self.s.operateur, "poste": self.s.poste,
                "rules_active": self.rules_active, "ui_scales": list(UI_SCALES), "stats": self.stats(),
                "settings": self.settings_get()}

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
                "pages": st["pages"], "echecs": st["echecs"], "formats": st["formats"], "annees": st["annees"],
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
        return {"q": q, "results": out, "counts": counts, "ms": round((time.perf_counter() - t0) * 1000),
                "students": len({r.matricule for r in rels})}

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
            out.append({"matricule": mat, "nom": latest.nom_complet, "found": [releve_dict(r) for r in found],
                        "missing": missing, "valides": n_ok, "total": len(dossier),
                        "tone": "red" if missing else ("green" if n_ok == len(dossier) else "amber")})
        return out

    # ------------------------------------------------------------------ relevé
    def releve(self, releve_id: int) -> dict:
        rel = self._get(releve_id)
        siblings = self.db.level_siblings(rel)
        needs = bool(self.previews is not None and self.previews.needs_render(rel))
        return {"releve": releve_dict(rel), "siblings": [releve_dict(s) for s in siblings],
                "alerts": [{"text": t, "tone": tone} for t, tone in alerts.compute(rel, siblings, self.rules_active)],
                "needs_render": needs, "preview_pdf": None if needs else rel.preview_pdf}

    def preview(self, releve_id: int) -> dict:
        """Aperçu Word produit au besoin (long : Word ou LibreOffice). Renvoie le relevé à jour."""
        rel = self._get(releve_id)
        if self.previews is not None and self.previews.needs_render(rel):
            rel = self.previews.ensure(rel)
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
        default = {r.id for r in pdf_tools.select_for_cursus(rels)}
        corr = self.db.correlated_matricules(matricule)
        return {"matricule": rels[0].matricule, "nom": rels[0].nom_complet,
                "releves": [dict(releve_dict(r), selected=r.id in default) for r in rels],
                "missing": missing_periods(rels), "correlated": corr,
                "correlated_releves": [releve_dict(r) for c in corr for r in self.db.cursus(c["matricule"])]}

    def cursus_build(self, matricule: str, releve_ids: list[int], cover: bool = True, duplicata: bool = False,
                     mode: str = "export") -> dict:
        rels = [r for r in (self.db.get(int(i)) for i in releve_ids) if r]
        if not rels:
            raise ServiceError("Cochez au moins un relevé.")
        rels.sort(key=lambda r: (r.niveau_num, r.semestre or 0, r.annee_academique or "",
                                 1 if (r.session or "").lower().startswith("rattrap") else 0))
        base = self.db.cursus(matricule) or rels
        nature = "CURSUS" if mode == "export" else "CURSUS IMPRIMÉ"
        ref = self.db.new_delivery(nature, self.s.poste, self.s.operateur, base[0].matricule)
        if mode == "export":
            nom = (norm_text(base[0].nom or base[0].nom_complet).split() or ["ETUDIANT"])[0]
            dest = exports.dest_for(self.s, exports.CURSUS, f"FMSP_{base[0].matricule}_{nom}_Cursus.pdf")
        else:
            dest = pdf_tools.temp_pdf("cursus_")

        def prepare(rel):
            if self.previews is not None:
                rel = self.previews.ensure(rel)
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
        return res

    def demande_remove(self, did: int, releve_id: int) -> dict:
        self.db.remove_demande_item(int(did), int(releve_id))
        return self._demande(did)

    def demande_reorder(self, did: int, releve_ids: list[int]) -> dict:
        self.db.reorder_demande(int(did), [int(i) for i in releve_ids])
        return self._demande(did)

    def demande_prepare(self, did: int) -> dict:
        prep = demandes.prepare(self.db, self.s, int(did), self.previews)
        return {"dossier": str(prep.dossier), "a_imprimer": str(prep.a_imprimer) if prep.a_imprimer else None,
                "bordereau": str(prep.bordereau), "pages": prep.pages,
                "prets": [{"releve_id": r.id, "ref": ref, "pages": n} for r, ref, n in prep.prets],
                "bloques": [{"releve_id": r.id if r else None, "periode": r.periode if r else "?", "motif": m}
                            for r, m in prep.bloques], **self._demande(did)}

    def demande_files_to_print(self, did: int, with_bordereau: bool = True) -> dict:
        """Fichiers prêts de la demande ; refait le dossier s'il est absent ou périmé."""
        d = self.db.demande(int(did))
        folder = demandes.demande_folder(self.s, d) if d and d["dossier"] else None
        if not (d and d["statut"] in ("PRÊTE", "IMPRIMÉE") and folder and (folder / demandes.A_IMPRIMER).exists()):
            prep = self.demande_prepare(did)
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

    def verify_compare(self, path: str, releve_id: int) -> dict:
        from .extractors import pdf_pages_text
        orig = self._get(releve_id)
        if self.previews is not None and self.previews.needs_render(orig):
            orig = self.previews.ensure(orig)
        sub_text = verification.read_submitted(path, self.s)
        pages = pdf_pages_text(Path(orig.preview_pdf)) if orig.preview_pdf else []
        orig_text = "\n".join(p.text for p in pages[orig.numero_page - 1:orig.page_fin])
        rows = verification.compare(sub_text, orig_text, orig)
        self._last_verif = (rows, orig)
        return {"original": releve_dict(orig), "rows": [dict(dataclasses.asdict(r), conforme=r.conforme) for r in rows],
                "conformes": sum(1 for r in rows if r.conforme), "total": len(rows)}

    def verify_report(self, path: str, releve_id: int, demandeur: str = "", reference: str = "") -> dict:
        """Rapport de vérification (PDF) pour l'organisme qui a soumis le document."""
        res = self.verify_compare(path, releve_id)
        rows, orig = self._last_verif
        dest = exports.dest_for(self.s, exports.VERIFICATIONS, f"Verification_{orig.matricule}.pdf")
        verification.report_pdf(rows, orig, dest, demandeur, reference, self.s.operateur)
        bad = res["total"] - res["conformes"]
        self._log("VERIFICATION", orig, f"{demandeur or 'demandeur ?'} · réf. {reference or '?'} · {bad} divergence(s)"
                                        f" · rapport {dest}")
        return {"path": str(dest), "name": dest.name}

    # ------------------------------------------------------------------ réglages
    PUBLIC = ("operateur", "poste", "source_root", "exports_root", "exports_retention_days", "printer_name",
              "print_command", "duplex", "fuzzy_threshold", "max_results", "ocr_enabled", "differential",
              "index_workers", "exclusions", "text_rules_enabled", "text_rules", "ui_sounds", "ui_animations",
              "ui_scale", "delivery_ref_stamp", "docx_convert_at_index")

    def settings_get(self) -> dict:
        return {k: getattr(self.s, k) for k in self.PUBLIC if hasattr(self.s, k)}

    def settings_save(self, values: dict) -> dict:
        for k, v in (values or {}).items():
            if k in self.PUBLIC and hasattr(self.s, k):
                setattr(self.s, k, v)
        save_settings(self.s)
        return self.settings_get()


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
