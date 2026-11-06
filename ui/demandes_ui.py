"""Demandes : barre « demande en cours » du guichet, fenêtre d'une demande, écran de suivi.

Le principe : on ne cherche plus des fichiers exportés en vrac. Chaque personne qui se présente
ouvre une demande ; l'agent y ajoute les relevés au fil des recherches (Ctrl+D), puis prépare un
dossier à part qui contient exactement ce qu'il faut imprimer, dans l'ordre, avec un bordereau.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from core import demandes, exports, pdf_tools
from core.config import Settings
from core.database import Database
from core.printer_service import print_pdf

from .widgets import TONES, Badge, Card, IconChip, Segmented, Switch, Task, Toast, eyebrow, muted, statut_tone, title

STATUT_TONE = {"EN COURS": "blue", "PRÊTE": "violet", "IMPRIMÉE": "amber", "REMISE": "green", "ANNULÉE": "gray"}
STATUT_AIDE = {"EN COURS": "à préparer", "PRÊTE": "dossier prêt, à imprimer", "IMPRIMÉE": "à remettre",
               "REMISE": "remise au demandeur", "ANNULÉE": "annulée"}
MOTIFS = ["Inscription / poursuite d'études", "Concours", "Emploi / stage", "Équivalence / études à l'étranger",
          "Bourse", "Dossier personnel", "Autre"]


def new_demande(db: Database, s: Settings, parent, rel=None) -> int | None:
    """Ouvre une demande, pré-remplie avec l'étudiant du relevé affiché."""
    demandeur, ok = QInputDialog.getText(parent, "Nouvelle demande",
                                         "Demandeur (l'étudiant lui-même, un parent, un mandataire…) :",
                                         text="L'étudiant lui-même")
    if not ok:
        return None
    return db.create_demande(s.poste, s.operateur, rel.matricule if rel else None,
                             rel.nom_complet if rel else None, demandeur.strip())


# --------------------------------------------------------------- barre du guichet
class DemandeBar(QFrame):
    """« Demande en cours » au-dessus des résultats : on sait toujours pour qui on travaille."""
    changed = Signal()

    def __init__(self, db: Database, settings: Settings, current_releve, parent=None):
        super().__init__(parent)
        self.db, self.s = db, settings
        self.current_releve = current_releve          # fonction -> relevé affiché (ou None)
        self.active: int | None = None
        self.setObjectName("demandebar")
        self.setStyleSheet("QFrame#demandebar{background:#FFFFFF;border:1px solid #E3E7EF;border-radius:18px;}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(10)
        lay.addWidget(IconChip("folder", "violet", 36))
        col = QVBoxLayout()
        col.setSpacing(1)
        col.addWidget(eyebrow("Demande en cours"))
        self.label = QLabel()
        f = self.label.font()
        f.setWeight(QFont.Weight.DemiBold)
        self.label.setFont(f)
        col.addWidget(self.label)
        lay.addLayout(col, 1)
        self.picker = QComboBox()
        self.picker.setMinimumWidth(220)
        self.picker.setToolTip("Changer de demande (plusieurs personnes au guichet)")
        self.picker.activated.connect(self._picked)
        lay.addWidget(self.picker)
        self.btn_add = QPushButton("Ajouter ce relevé   Ctrl+D")
        self.btn_add.setObjectName("primary")
        self.btn_add.clicked.connect(self.add_current)
        self.btn_open = QPushButton("Voir / préparer")
        self.btn_open.setObjectName("secondary")
        self.btn_open.clicked.connect(self.open_dialog)
        self.btn_new = QPushButton("Nouvelle demande")
        self.btn_new.setObjectName("ghost")
        self.btn_new.clicked.connect(self.create)
        for b in (self.btn_add, self.btn_open, self.btn_new):
            lay.addWidget(b)
        self.refresh()

    def refresh(self) -> None:
        open_ = self.db.demandes(demandes.OUVERTES, limit=50)
        if self.active is not None and not any(r["id"] == self.active for r in open_):
            self.active = None
        self.picker.blockSignals(True)
        self.picker.clear()
        self.picker.addItem("— aucune —", None)
        for r in open_:
            self.picker.addItem(f"{r['numero']} · {r['nom'] or r['matricule'] or '?'} ({r['n']})", r["id"])
        self.picker.setCurrentIndex(max(0, self.picker.findData(self.active)))
        self.picker.blockSignals(False)
        d = self.db.demande(self.active) if self.active else None
        if d:
            self.label.setText(f"{d['numero']} · {d['nom'] or '—'} ({d['matricule'] or '—'}) · {d['n']} relevé(s) · "
                               f"{STATUT_AIDE.get(d['statut'], d['statut'])}")
        else:
            self.label.setText("Aucune — créez une demande pour réunir les relevés d'une même personne")
        self.btn_add.setEnabled(True)
        self.btn_open.setEnabled(d is not None)
        self.changed.emit()

    def releve_ids(self) -> set[int]:
        if not self.active:
            return set()
        return {item["releve_id"] for item, _ in self.db.demande_items(self.active)}

    def _picked(self, idx: int) -> None:
        self.active = self.picker.itemData(idx)
        self.refresh()

    def set_active(self, did: int | None) -> None:
        self.active = did
        self.refresh()

    def create(self) -> None:
        did = new_demande(self.db, self.s, self, self.current_releve())
        if did:
            self.set_active(did)

    def add_current(self) -> str:
        rel = self.current_releve()
        if rel is None:
            return "Sélectionnez d'abord un relevé."
        if self.active is None:
            did = new_demande(self.db, self.s, self, rel)
            if not did:
                return ""
            self.active = did
        d = self.db.demande(self.active)
        if d["matricule"] and d["matricule"] != rel.matricule:
            msg = (f"Attention : ce relevé ({rel.matricule}) n'est pas celui de l'étudiant de la demande "
                   f"({d['matricule']}). Ajouté quand même (changement de filière ?) : vérifiez.")
        else:
            msg = ""
        if not d["matricule"]:
            self.db.update_demande(self.active, matricule=rel.matricule, nom=rel.nom_complet)
        added = self.db.add_demande_item(self.active, rel.id)
        self.refresh()
        if not added:
            return f"Ce relevé est déjà dans la demande {d['numero']}."
        return msg or f"Ajouté à la demande {d['numero']} : {rel.periode} · {rel.annee_academique or '?'}"

    def open_dialog(self) -> None:
        if self.active:
            DemandeDialog(self.db, self.s, self.active, self, previews=getattr(self, "previews", None)).exec()
            self.refresh()


# ------------------------------------------------------------- fenêtre d'une demande
class DemandeDialog(QDialog):
    COLS = ("N°", "Période", "Année", "Décision", "Pages", "État", "Réf. délivrance")

    def __init__(self, db: Database, settings: Settings, did: int, parent=None, previews=None):
        super().__init__(parent)
        self.db, self.s, self.did, self.previews = db, settings, did, previews
        self._busy = False
        self.setWindowTitle("Demande")
        self.resize(940, 720)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        card = Card(padding=18, spacing=12, soft_shadow=False)
        lay.addWidget(card)
        head = QHBoxLayout()
        head.addWidget(IconChip("folder", "violet", 44))
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(eyebrow("Demande de relevés"))
        self.title = title("", 15)
        col.addWidget(self.title)
        self.sub = muted("", 9)
        col.addWidget(self.sub)
        head.addLayout(col, 1)
        self.badge = Badge("", "gray", big=True)
        head.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        card.lay.addLayout(head)

        form = QFormLayout()
        form.setSpacing(8)
        self.demandeur = QLineEdit()
        self.demandeur.setObjectName("field")
        self.motif = QComboBox()
        self.motif.setEditable(True)
        self.motif.addItems(MOTIFS)
        self.duplicata = Switch(False)
        self.duplicata.setAccessibleName("Timbre « Duplicata conforme »")
        dup = QHBoxLayout()
        dup.addWidget(self.duplicata)
        dup.addWidget(QLabel("Timbre « Duplicata conforme » sur tous les relevés"))
        dup.addStretch(1)
        form.addRow("Demandeur", self.demandeur)
        form.addRow("Motif", self.motif)
        form.addRow("", dup)
        card.lay.addLayout(form)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setObjectName("demande")
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.table.horizontalHeader()
        for c, w in ((0, 40), (1, 170), (2, 96), (3, 160), (4, 54), (5, 200)):
            hh.resizeSection(c, w)
        hh.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(220)
        card.lay.addWidget(self.table, 1)

        tools = QHBoxLayout()
        for label, slot, name in (("Monter", lambda: self.move(-1), "ghost"), ("Descendre", lambda: self.move(1), "ghost"),
                                  ("Retirer", self.remove, "ghost"),
                                  ("Ajouter tout le cursus validé", self.add_cursus, "secondary")):
            b = QPushButton(label)
            b.setObjectName(name)
            b.clicked.connect(slot)
            tools.addWidget(b)
        tools.addStretch(1)
        self.with_bordereau = QCheckBox("Imprimer aussi le bordereau")
        self.with_bordereau.setChecked(True)
        tools.addWidget(self.with_bordereau)
        card.lay.addLayout(tools)

        self.summary = QLabel()
        self.summary.setStyleSheet("QLabel{background:#0F172A;color:#E2E8F0;border-radius:14px;padding:10px 14px;}")
        self.summary.setWordWrap(True)
        card.lay.addWidget(self.summary)

        btns = QHBoxLayout()
        self.btn_prepare = QPushButton("Préparer le dossier")
        self.btn_prepare.setObjectName("primary")
        self.btn_prepare.clicked.connect(lambda: self.prepare())
        self.btn_print = QPushButton("Imprimer tout")
        self.btn_print.setObjectName("secondary")
        self.btn_print.clicked.connect(self.print_all)
        self.btn_done = QPushButton("Marquer remise")
        self.btn_done.setObjectName("secondary")
        self.btn_done.clicked.connect(self.mark_done)
        self.btn_folder = QPushButton("Ouvrir le dossier")
        self.btn_folder.setObjectName("ghost")
        self.btn_folder.clicked.connect(self.open_folder)
        self.btn_cancel = QPushButton("Annuler la demande")
        self.btn_cancel.setObjectName("ghost")
        self.btn_cancel.clicked.connect(self.cancel)
        close = QPushButton("Fermer")
        close.setObjectName("ghost")
        close.clicked.connect(self.accept)
        for b in (self.btn_prepare, self.btn_print, self.btn_done, self.btn_folder, self.btn_cancel):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(close)
        card.lay.addLayout(btns)
        self.toast = Toast()
        card.lay.addWidget(self.toast)

        d = self.db.demande(did)
        self.demandeur.setText(d["demandeur"] or "")
        self.motif.setCurrentText(d["motif"] or "")
        self.duplicata.setChecked(bool(d["duplicata"]))
        self.demandeur.editingFinished.connect(self._save_fields)
        self.motif.currentTextChanged.connect(lambda *_: self._save_fields())
        self.duplicata.toggled.connect(lambda *_: self._save_fields(reset=True))
        self.refresh()

    # ---------------------------------------------------------------- données
    def _save_fields(self, reset: bool = False) -> None:
        fields = {"demandeur": self.demandeur.text().strip(), "motif": self.motif.currentText().strip(),
                  "duplicata": 1 if self.duplicata.isChecked() else 0}
        if reset and self.db.demande(self.did)["statut"] == "PRÊTE":
            fields["statut"] = "EN COURS"      # le timbre change : le dossier est à refaire
        self.db.update_demande(self.did, **fields)
        self.refresh()

    def refresh(self) -> None:
        d = self.db.demande(self.did)
        self.items = self.db.demande_items(self.did)
        self.title.setText(f"Demande {d['numero']} · {d['nom'] or '—'} ({d['matricule'] or '—'})")
        self.sub.setText(f"Reçue le {(d['creee_le'] or '')[:16].replace('-', '/')} par {d['operateur'] or '—'}"
                         + (f" · dossier : {exports.short_location(self.s, Path(d['dossier']) / 'x')}"
                            if d["dossier"] else ""))
        self.badge.set(f"{d['statut']} — {STATUT_AIDE.get(d['statut'], '')}", STATUT_TONE.get(d["statut"], "gray"))
        self.table.setRowCount(len(self.items))
        pages = 0
        for i, (item, rel) in enumerate(self.items):
            vals = [f"{i + 1:02d}", demandes.periode_label(rel) if rel else "(retiré de l'index)",
                    (rel.annee_academique or "?") if rel else "", (rel.decision or rel.statut or "?") if rel else "",
                    str(rel.nb_pages) if rel else "", item["etat"] or ("à préparer" if d["statut"] == "EN COURS" else ""),
                    item["reference"] or ""]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j == 3 and rel:
                    it.setForeground(QColor(TONES[statut_tone(rel.statut)][0]))
                if j == 5 and (item["etat"] or "").startswith("BLOQUÉ"):
                    it.setForeground(QColor(TONES["red"][0]))
                    it.setToolTip(item["etat"])
                self.table.setItem(i, j, it)
            if rel and not (item["etat"] or "").startswith("BLOQUÉ"):
                pages += rel.nb_pages
        status = d["statut"]
        nxt = {"EN COURS": "Étape suivante : « Préparer le dossier ».",
               "PRÊTE": "Étape suivante : « Imprimer tout », puis remettre les documents.",
               "IMPRIMÉE": "Étape suivante : remettre les documents et « Marquer remise ».",
               "REMISE": "Demande terminée.", "ANNULÉE": "Demande annulée."}[status]
        self.summary.setText(f"{len(self.items)} relevé(s) · {pages} page(s) à imprimer · {nxt}")
        editable = status not in ("REMISE", "ANNULÉE") and not self._busy
        self.btn_prepare.setEnabled(editable and bool(self.items))
        self.btn_print.setEnabled(editable and bool(self.items))
        self.btn_done.setEnabled(status in ("PRÊTE", "IMPRIMÉE") and not self._busy)
        self.btn_folder.setEnabled(bool(d["dossier"]))
        self.btn_cancel.setEnabled(status not in ("REMISE", "ANNULÉE"))

    def _selected_row(self) -> int:
        rows = {i.row() for i in self.table.selectedIndexes()}
        return min(rows) if rows else -1

    def move(self, delta: int) -> None:
        r = self._selected_row()
        if r < 0 or not (0 <= r + delta < len(self.items)):
            return
        ids = [item["releve_id"] for item, _ in self.items]
        ids[r], ids[r + delta] = ids[r + delta], ids[r]
        self.db.reorder_demande(self.did, ids)
        self.refresh()
        self.table.selectRow(r + delta)

    def remove(self) -> None:
        r = self._selected_row()
        if r >= 0:
            self.db.remove_demande_item(self.did, self.items[r][0]["releve_id"])
            self.refresh()

    def add_cursus(self) -> None:
        d = self.db.demande(self.did)
        if not d["matricule"]:
            self.toast.show_message("Ajoutez d'abord un relevé de l'étudiant.", "amber")
            return
        n = sum(self.db.add_demande_item(self.did, r.id)
                for r in pdf_tools.select_for_cursus(self.db.cursus(d["matricule"])))
        self.refresh()
        self.toast.show_message(f"{n} relevé(s) ajouté(s) (cursus : dernier relevé validé de chaque semestre).")

    # ---------------------------------------------------------------- actions
    def prepare(self, then=None) -> None:
        self._save_fields()
        self._busy = True
        self.refresh()
        self.toast.show_message("Préparation du dossier… (contrôle de chaque relevé avant copie)", "blue", 60000)
        task = Task(demandes.prepare, self.db, self.s, self.did, self.previews)

        def done(prep):
            self._busy = False
            self.refresh()
            msg = f"Dossier prêt : {len(prep.prets)} relevé(s), {prep.pages} page(s)"
            if prep.bloques:
                msg += f" — {len(prep.bloques)} écarté(s), voir l'état (bordereau)"
            self.toast.show_message(msg, "amber" if prep.bloques else "green", 15000,
                                    action=("Ouvrir le dossier", lambda: exports.reveal(prep.dossier)))
            if then:
                then(prep)

        def failed(err):
            self._busy = False
            self.refresh()
            self.toast.show_message(f"Préparation impossible : {err}", "red")

        task.signals.done.connect(done)
        task.signals.failed.connect(failed)
        QThreadPool.globalInstance().start(task)

    def print_all(self) -> None:
        def go(prep):
            if not prep.a_imprimer:
                self.toast.show_message("Aucun relevé imprimable dans cette demande.", "red")
                return
            files = ([prep.bordereau] if self.with_bordereau.isChecked() else []) + [prep.a_imprimer]
            for f in files:
                task = Task(print_pdf, f, self.s)
                task.signals.failed.connect(lambda e: self.toast.show_message(f"Impression impossible : {e}", "red"))
                QThreadPool.globalInstance().start(task)
            demandes.mark(self.db, self.s, self.did, "IMPRIMÉE",
                          f"{len(prep.prets)} relevé(s), {prep.pages} page(s) envoyés à l'imprimante")
            self.refresh()
            self.toast.show_message(f"Envoyé à l'imprimante : {prep.pages} page(s)"
                                    + (" + bordereau" if self.with_bordereau.isChecked() else "")
                                    + ". Remettez les documents puis « Marquer remise ».")
        d = self.db.demande(self.did)
        folder = demandes.demande_folder(self.s, d) if d["dossier"] else None
        if d["statut"] in ("PRÊTE", "IMPRIMÉE") and folder and (folder / demandes.A_IMPRIMER).exists():
            go(demandes.Preparation(folder, folder / demandes.A_IMPRIMER, folder / demandes.BORDEREAU,
                                    prets=[(rel, item["reference"], rel.nb_pages) for item, rel in self.items
                                           if rel and item["etat"] == "PRÊT"]))
        else:
            self.prepare(then=go)          # dossier absent ou périmé : on le refait d'abord

    def mark_done(self) -> None:
        who, ok = QInputDialog.getText(self, "Remise", "Remis à (nom de la personne) :",
                                       text=self.demandeur.text())
        if ok:
            demandes.mark(self.db, self.s, self.did, "REMISE", f"remis à {who.strip() or '?'}")
            self.refresh()

    def cancel(self) -> None:
        demandes.mark(self.db, self.s, self.did, "ANNULÉE")
        self.refresh()

    def open_folder(self) -> None:
        d = self.db.demande(self.did)
        if d["dossier"]:
            exports.reveal(d["dossier"])


# ---------------------------------------------------------------- écran de suivi
class DemandesPage(QWidget):
    activate = Signal(int)          # rendre une demande active au guichet

    FILTRES = (("a_traiter", "À traiter"), ("a_remettre", "À remettre"), ("remises", "Remises"), ("toutes", "Toutes"))
    STATUTS = {"a_traiter": ("EN COURS", "PRÊTE"), "a_remettre": ("IMPRIMÉE",), "remises": ("REMISE",), "toutes": None}

    def __init__(self, db: Database, settings: Settings, parent=None):
        super().__init__(parent)
        self.db, self.s = db, settings
        self.previews = None
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)
        head = QHBoxLayout()
        col = QVBoxLayout()
        col.addWidget(eyebrow("Suivi des demandes"))
        col.addWidget(title("Demandes de relevés", 22, "h1"))
        head.addLayout(col, 1)
        self.counts = Badge("", "violet", big=True)
        head.addWidget(self.counts, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(head)
        card = Card(padding=16, spacing=12)
        root.addWidget(card, 1)
        bar = QHBoxLayout()
        self.seg = Segmented(list(self.FILTRES), "a_traiter")
        self.seg.changed.connect(lambda *_: self.refresh())
        bar.addWidget(self.seg)
        self.q = QLineEdit()
        self.q.setObjectName("field")
        self.q.setPlaceholderText("Filtrer : numéro, matricule, nom, demandeur…")
        self.q.textChanged.connect(lambda *_: self.refresh())
        bar.addWidget(self.q, 1)
        card.lay.addLayout(bar)
        self.table = QTableWidget(0, 7)
        self.table.setObjectName("demandes")
        self.table.setHorizontalHeaderLabels(("N° de demande", "Reçue le", "Étudiant", "Demandeur · motif",
                                              "Relevés", "Statut", "Prochaine étape"))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.table.horizontalHeader()
        for c, w in ((0, 130), (1, 110), (2, 230), (3, 230), (4, 64), (5, 110)):
            hh.resizeSection(c, w)
        hh.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(lambda *_: self.open_selected())
        card.lay.addWidget(self.table, 1)
        btns = QHBoxLayout()
        for label, slot, name in (("Ouvrir", self.open_selected, "primary"),
                                  ("Reprendre au guichet", self.activate_selected, "secondary"),
                                  ("Ouvrir le dossier", self.folder_selected, "ghost"),
                                  ("Nouvelle demande", self.create, "ghost")):
            b = QPushButton(label)
            b.setObjectName(name)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch(1)
        card.lay.addLayout(btns)
        card.lay.addWidget(muted("Une demande par personne : on y réunit tous les relevés demandés (plusieurs années, "
                                 "plusieurs semestres). « Préparer » crée un dossier Demandes\\AAAA\\MM\\<n°>_<matricule>_<NOM> "
                                 "avec un bordereau, les relevés numérotés et un fichier unique « A_IMPRIMER_TOUT ».", 8.5))
        self.refresh()

    def refresh(self) -> None:
        rows = self.db.demandes(self.STATUTS[self.seg.value()])
        q = self.q.text().strip().lower()
        if q:
            rows = [r for r in rows if q in " ".join(str(r[k] or "") for k in
                                                     ("numero", "matricule", "nom", "demandeur", "motif")).lower()]
        self._rows = rows
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            vals = (r["numero"], (r["creee_le"] or "")[:16].replace("-", "/"), f"{r['nom'] or '—'} ({r['matricule'] or '—'})",
                    " · ".join(x for x in (r["demandeur"], r["motif"]) if x) or "—", str(r["n"]), r["statut"],
                    STATUT_AIDE.get(r["statut"], ""))
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j == 5:
                    it.setForeground(QColor(TONES[STATUT_TONE.get(v, "gray")][0]))
                self.table.setItem(i, j, it)
        allr = self.db.demandes(demandes.OUVERTES)
        n = {st: sum(1 for r in allr if r["statut"] == st) for st in demandes.OUVERTES}
        self.counts.set(f"{n['EN COURS']} à préparer · {n['PRÊTE']} à imprimer · {n['IMPRIMÉE']} à remettre", "violet")

    def _selected(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        return self._rows[min(rows)] if rows else None

    def open_selected(self) -> None:
        r = self._selected()
        if r:
            DemandeDialog(self.db, self.s, r["id"], self, previews=self.previews).exec()
            self.refresh()

    def activate_selected(self) -> None:
        r = self._selected()
        if r:
            self.activate.emit(r["id"])

    def folder_selected(self) -> None:
        r = self._selected()
        if r and r["dossier"]:
            exports.reveal(r["dossier"])

    def create(self) -> None:
        did = new_demande(self.db, self.s, self)
        if did:
            self.refresh()
            self.activate.emit(did)
