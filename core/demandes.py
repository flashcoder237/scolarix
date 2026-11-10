"""Demandes du guichet : réunir, préparer et imprimer ensemble les relevés demandés par une personne.

Une demande = une personne qui demande un ou plusieurs relevés (plusieurs années, plusieurs semestres).
L'agent crée la demande, y ajoute les relevés au fil des recherches, puis « prépare le dossier » :

    Exports Scolarix/Demandes/AAAA/MM/2026-G1-0042_98EM101_NOM/
        00_BORDEREAU.pdf             ce qu'il faut imprimer et remettre, case à cocher par relevé
        01_FMSP_98EM101_NOM_2021_Niv1_S1.pdf
        02_FMSP_98EM101_NOM_2021_Niv1_S2.pdf
        …
        A_IMPRIMER_TOUT.pdf          tous les relevés de la demande, dans l'ordre, en un seul fichier

Chaque relevé passe par le contrôle de dernière ligne (core/guard.py) et reçoit son numéro de
délivrance. Un relevé bloqué est écarté du dossier et signalé sur le bordereau : rien n'est imprimé
à l'aveugle. Le statut de la demande (EN COURS → PRÊTE → IMPRIMÉE → REMISE) dit à tout moment ce
qui reste à faire.
"""
from __future__ import annotations

import io
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import Color
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from . import exports, pdf_tools
from .config import Settings
from .database import Database, Releve
from .guard import check_before_print
from .normalize import norm_text

DEMANDES = "Demandes"
STATUTS = ("EN COURS", "PRÊTE", "IMPRIMÉE", "REMISE", "ANNULÉE")
OUVERTES = ("EN COURS", "PRÊTE", "IMPRIMÉE")
BORDEREAU = "00_BORDEREAU.pdf"
A_IMPRIMER = "A_IMPRIMER_TOUT.pdf"


@dataclass
class Preparation:
    dossier: Path
    a_imprimer: Path | None
    bordereau: Path
    prets: list[tuple[Releve, str, int]] = field(default_factory=list)      # (relevé, référence, nb pages)
    bloques: list[tuple[Releve | None, str]] = field(default_factory=list)  # (relevé, motif)

    @property
    def pages(self) -> int:
        return sum(n for _, _, n in self.prets)


def periode_label(rel: Releve) -> str:
    """« Niveau 3 · S5 · rattrapage » : deux relevés du même semestre se distinguent sur le bordereau."""
    sess = " · rattrapage" if (rel.session or "").lower().startswith("rattrap") else ""
    return rel.periode + sess


def folder_name(d) -> str:
    nom = re.sub(r"[^A-Z0-9]", "", (norm_text(d["nom"] or "").split() or ["DEMANDE"])[0])[:20] or "DEMANDE"
    return f"{d['numero']}_{d['matricule'] or 'SANS-MATRICULE'}_{nom}"


def demande_folder(settings: Settings, d) -> Path:
    if d["dossier"] and Path(d["dossier"]).exists():
        return Path(d["dossier"])
    created = datetime.strptime(d["creee_le"][:10], "%Y-%m-%d") if d["creee_le"] else datetime.now()
    folder = settings.exports_root_dir() / DEMANDES / f"{created:%Y}" / f"{created:%m}" / folder_name(d)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def prepare(db: Database, settings: Settings, did: int, previews=None) -> Preparation:
    """Produit le dossier de la demande. Bloquant (aperçus Word possibles) : appeler depuis une tâche de fond."""
    d = db.demande(did)
    if d is None:
        raise ValueError("Demande introuvable.")
    folder = demande_folder(settings, d)
    for old in folder.glob("*.pdf"):             # une nouvelle préparation remplace l'ancienne, entièrement
        try:
            old.unlink()
        except OSError:
            pass
    stamp = pdf_tools.stamp_text() if d["duplicata"] else None
    prep = Preparation(folder, None, folder / BORDEREAU)
    files: list[Path] = []
    for k, (item, rel) in enumerate(db.demande_items(did), start=1):
        if rel is None:
            prep.bloques.append((None, "relevé retiré de l'index depuis l'ajout"))
            db.set_item_result(item["id"], None, None, "BLOQUÉ : retiré de l'index")
            continue
        try:
            if previews is not None:
                rel = previews.ensure(rel)
            check_before_print(rel)
        except Exception as exc:
            reason = re.sub(r"^Impression bloquée( pour [^:]+)?\s*:\s*", "", str(exc)).strip()[:160] \
                or exc.__class__.__name__
            prep.bloques.append((rel, reason))
            db.set_item_result(item["id"], None, None, f"BLOQUÉ : {reason}")
            continue
        ref = db.new_delivery("DEMANDE " + d["numero"], settings.poste, settings.operateur, rel.matricule, rel.id)
        dest = folder / f"{k:02d}_{pdf_tools.export_filename(rel, '_DUPLICATA' if d['duplicata'] else '')}"
        pdf_tools.extract_pages(rel.preview_pdf, rel.numero_page, rel.page_fin, dest, stamp=stamp,
                                footer=pdf_tools.footer_text(ref) if settings.delivery_ref_stamp else None)
        db.complete_delivery(ref, str(dest), exports.sha256_file(dest))
        db.set_item_result(item["id"], ref, str(dest), "PRÊT")
        prep.prets.append((rel, ref, rel.nb_pages))
        files.append(dest)
    if files:
        writer = PdfWriter()
        for f in files:
            for page in PdfReader(str(f)).pages:
                writer.add_page(page)
        prep.a_imprimer = folder / A_IMPRIMER
        with open(prep.a_imprimer, "wb") as fh:
            writer.write(fh)
    _bordereau(prep, d, settings)
    db.update_demande(did, dossier=str(folder), statut="PRÊTE" if files else "EN COURS")
    db.log("DEMANDE", settings.operateur, settings.poste, None, d["matricule"],
           f"{d['numero']} préparée : {len(prep.prets)} relevé(s), {prep.pages} page(s)"
           + (f", {len(prep.bloques)} bloqué(s)" if prep.bloques else "") + f" · {folder}")
    return prep


def mark(db: Database, settings: Settings, did: int, statut: str, detail: str = "") -> None:
    d = db.demande(did)
    db.update_demande(did, statut=statut)
    db.log("DEMANDE", settings.operateur, settings.poste, None, d["matricule"] if d else None,
           f"{d['numero'] if d else did} : {statut.lower()}" + (f" · {detail}" if detail else ""))


def bordereau_provisoire(db: Database, settings: Settings, did: int, dest: Path) -> Path:
    """Aperçu du bordereau AVANT la préparation (revue de la demande) : mêmes lignes, dans l'ordre actuel,
    numéros de délivrance « à attribuer » et filigrane PROVISOIRE. Rien n'est réservé ni journalisé."""
    d = db.demande(did)
    if d is None:
        raise ValueError("Demande introuvable.")
    prep = Preparation(Path(dest).parent, None, Path(dest))
    for _item, rel in db.demande_items(did):
        if rel is None:
            prep.bloques.append((None, "relevé retiré de l'index depuis l'ajout"))
        else:
            prep.prets.append((rel, "à attribuer", rel.nb_pages))
    _bordereau(prep, d, settings, provisoire=True)
    reader = PdfReader(str(dest))
    writer = PdfWriter()
    for page in reader.pages:
        page = writer.add_page(page)
        page.merge_page(pdf_tools._stamp_page(float(page.mediabox.width), float(page.mediabox.height), "PROVISOIRE",
                                              "numéros de délivrance attribués à la préparation"))
    with open(dest, "wb") as fh:
        writer.write(fh)
    return Path(dest)


def _bordereau(prep: Preparation, d, settings: Settings, provisoire: bool = False) -> None:
    """Feuille de route de l'agent : qui, quoi, combien de pages, numéros de délivrance, cases à cocher."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    ink, muted, red = Color(0.06, 0.09, 0.16), Color(0.36, 0.40, 0.47), Color(0.72, 0.11, 0.11)

    def header():
        c.setFillColor(ink)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(40, h - 42, "UNIVERSITÉ DE DOUALA — FMSP · Scolarité")
        c.setFont("Helvetica", 9)
        c.setFillColor(muted)
        c.drawRightString(w - 40, h - 42, ("Bordereau PROVISOIRE · aperçu du" if provisoire else "Bordereau de demande · édité le")
                          + f" {datetime.now():%d/%m/%Y %H:%M}")

    header()
    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(40, h - 82, f"Demande {d['numero']}")
    c.setFont("Helvetica", 11)
    lines = [f"Étudiant : {d['nom'] or '—'}   ·   matricule {d['matricule'] or '—'}",
             f"Demandeur : {d['demandeur'] or '—'}   ·   motif : {d['motif'] or '—'}",
             f"Reçue le {(d['creee_le'] or '')[:16].replace('-', '/')} par {d['operateur'] or '—'} (poste {d['poste'] or '—'})"
             + ("   ·   DUPLICATA CONFORME" if d["duplicata"] else "")]
    y = h - 106
    for line in lines:
        c.drawString(40, y, line)
        y -= 17
    y -= 8
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, y, f"À imprimer : {len(prep.prets)} relevé(s), {prep.pages} page(s)"
                 + (f" — fichier {A_IMPRIMER}" if prep.a_imprimer else ""))
    y -= 24
    cols = ((40, "Imprimé"), (90, "Remis"), (132, "N°"), (158, "Période"), (300, "Année"), (362, "Décision"),
            (470, "Pages"), (505, "Réf. délivrance"))
    c.setFont("Helvetica-Bold", 8.5)
    c.setFillColor(muted)
    for x, label in cols:
        c.drawString(x, y, label)
    y -= 6
    c.line(40, y, w - 40, y)
    c.setFillColor(ink)
    for k, (rel, ref, n) in enumerate(prep.prets, start=1):
        y -= 20
        if y < 90:
            c.showPage()
            header()
            y = h - 80
        c.rect(52, y - 2, 10, 10)
        c.rect(98, y - 2, 10, 10)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(132, y, f"{k:02d}")
        c.setFont("Helvetica", 9)
        c.drawString(158, y, periode_label(rel)[:30])
        c.drawString(300, y, rel.annee_academique or "?")
        c.drawString(362, y, (rel.decision or rel.statut or "?")[:20])
        c.drawString(470, y, str(n))
        c.setFont("Helvetica", 7.5)
        c.drawString(505, y, ref)
    if prep.bloques:
        y -= 34
        c.setFillColor(red)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(40, y, f"Non joints au dossier ({len(prep.bloques)}) — à traiter avant la remise :")
        c.setFont("Helvetica", 9)
        for rel, reason in prep.bloques:
            y -= 16
            if y < 90:
                c.showPage()
                header()
                y = h - 80
                c.setFillColor(red)
            label = f"{periode_label(rel)} {rel.annee_academique or ''}" if rel else "Relevé supprimé"
            c.drawString(52, y, f"• {label} : {reason}"[:118])
    c.setFillColor(muted)
    c.setFont("Helvetica", 9)
    c.drawString(40, 60, "Remis le ____ / ____ / ________    à : ______________________________    "
                         "Signature : ____________________")
    c.save()
    prep.bordereau.write_bytes(buf.getvalue())


def delete_folder(settings: Settings, d) -> None:
    if d["dossier"] and Path(d["dossier"]).exists():
        shutil.rmtree(d["dossier"], ignore_errors=True)
