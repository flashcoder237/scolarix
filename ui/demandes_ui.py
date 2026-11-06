"""Demandes : barre « demande en cours » du guichet, fenêtre d'une demande, écran de suivi.

Le principe : on ne cherche plus des fichiers exportés en vrac. Chaque personne qui se présente
ouvre une demande ; l'agent y ajoute les relevés au fil des recherches (Ctrl+D), puis prépare un
dossier à part qui contient exactement ce qu'il faut imprimer, dans l'ordre, avec un bordereau.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from core import demandes, exports, pdf_tools
from core.config import Settings
from core.database import Database
from core.printer_service import print_pdf

from .widgets import (Badge, Card, IconChip, Segmented, StatCard, Switch, Task, Toast, card_header, eyebrow,
                      mono_font, muted, statut_tone, title)

STATUT_TONE = {"EN COURS": "blue", "PRÊTE": "violet", "IMPRIMÉE": "amber", "REMISE": "green", "ANNULÉE": "gray"}
STATUT_AIDE = {"EN COURS": "à préparer", "PRÊTE": "dossier prêt, à imprimer", "IMPRIMÉE": "à remettre",
               "REMISE": "remise au demandeur", "ANNULÉE": "annulée"}
MOTIFS = ["Inscription / poursuite d'études", "Concours", "Emploi / stage", "Équivalence / études à l'étranger",
          "Bourse", "Dossier personnel", "Autre"]


def style_table(table: QTableWidget, headers: tuple[str, ...]) -> None:
    """Même apparence que les autres tableaux de l'application (en-têtes en capitales, lignes alternées)."""
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels([h.upper() for h in headers])
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(44)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setShowGrid(False)
    table.setAlternatingRowColors(True)
    table.setStyleSheet("QTableWidget{alternate-background-color:#F8FAFC;}")
    table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


def badge_cell(table: QTableWidget, row: int, col: int, text: str, tone: str) -> None:
    holder = QWidget()
    hl = QHBoxLayout(holder)
    hl.setContentsMargins(4, 4, 4, 4)
    hl.addWidget(Badge(text, tone))
    hl.addStretch(1)
    table.setCellWidget(row, col, holder)


class StepTracker(QWidget):
    """Les étapes d'une demande, l'étape courante en couleur : Préparer → Imprimer → Remettre."""
    STEPS = (("EN COURS", "1 · Préparer"), ("PRÊTE", "2 · Imprimer"), ("IMPRIMÉE", "3 · Remettre"), ("REMISE", "Terminée"))

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.badges = []
        for k, (_, label) in enumerate(self.STEPS):
            if k:
                arrow = QLabel("→")
                arrow.setStyleSheet("color:#94A3B8;")
                lay.addWidget(arrow)
            b = Badge(label, "gray")
            self.badges.append(b)
            lay.addWidget(b)
        lay.addStretch(1)

    def set(self, statut: str) -> None:
        order = [st for st, _ in self.STEPS]
        cur = order.index(statut) if statut in order else -1
        for k, (b, (st, label)) in enumerate(zip(self.badges, self.STEPS)):
            tone = "green" if k < cur or statut == "REMISE" else (STATUT_TONE[st] if k == cur else "gray")
            b.set(("✓ " if (k < cur or statut == "REMISE") and k < 3 else "") + label, tone)


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
class DemandeBar(Card):
    """« Demande en cours » au-dessus des résultats : on sait toujours pour qui on travaille."""
    changed = Signal()

    def __init__(self, db: Database, settings: Settings, current_releve, parent=None):
        super().__init__(padding=14, spacing=0, parent=parent)
        self.db, self.s = db, settings
        self.current_releve = current_releve          # fonction -> relevé affiché (ou None)
        self.active: int | None = None
        lay = QHBoxLayout()
        lay.setSpacing(12)
        self.lay.addLayout(lay)
        lay.addWidget(IconChip("folder", "violet", 36))
        col = QVBoxLayout()
        col.setSpacing(1)
        col.addWidget(eyebrow("Demande en cours"))
        self.label = title("", 11)
        self.label.setMinimumWidth(120)
        from PySide6.QtWidgets import QSizePolicy
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(self.label)
        lay.addLayout(col, 1)
        self.status = Badge("", "gray")
        lay.addWidget(self.status)
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
            self.label.setText(f"{d['numero']} · {d['nom'] or '—'} · {d['n']} relevé(s)")
            self.label.setToolTip(f"{d['nom'] or '—'} — matricule {d['matricule'] or '—'} — demandeur : "
                                  f"{d['demandeur'] or '—'}")
            self.status.set(STATUT_AIDE.get(d["statut"], d["statut"]), STATUT_TONE.get(d["statut"], "gray"))
            self.status.show()
        else:
            self.label.setText("Aucune — une demande réunit les relevés demandés par une même personne")
            self.status.hide()
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
        self.steps = StepTracker()
        card.lay.addWidget(self.steps)

        self.demandeur = QLineEdit()
        self.demandeur.setObjectName("field")
        self.motif = QComboBox()
        self.motif.setEditable(True)
        self.motif.addItems(MOTIFS)
        self.duplicata = Switch(False)
        self.duplicata.setAccessibleName("Timbre « Duplicata conforme »")
        grid = QHBoxLayout()
        grid.setSpacing(12)
        for label, w, stretch in (("Demandeur", self.demandeur, 1), ("Motif", self.motif, 1)):
            box = QVBoxLayout()
            box.setSpacing(4)
            box.addWidget(eyebrow(label))
            box.addWidget(w)
            grid.addLayout(box, stretch)
        box = QVBoxLayout()
        box.setSpacing(4)
        box.addWidget(eyebrow("Duplicata conforme"))
        dup = QHBoxLayout()
        dup.addWidget(self.duplicata)
        dup.addWidget(muted("timbre sur tous les relevés", 9))
        box.addLayout(dup)
        grid.addLayout(box)
        card.lay.addLayout(grid)

        card.lay.addWidget(eyebrow("Relevés de la demande · dans l'ordre d'impression"))
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setObjectName("demande")
        style_table(self.table, self.COLS)
        hh = self.table.horizontalHeader()
        for c, w in ((0, 44), (1, 200), (2, 96), (3, 150), (4, 64), (5, 190)):
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
        self.steps.set(d["statut"])
        self.table.setRowCount(0)              # supprime aussi les pastilles de l'affichage précédent
        self.table.setRowCount(len(self.items))
        pages = 0
        for i, (item, rel) in enumerate(self.items):
            vals = [f"{i + 1:02d}", demandes.periode_label(rel) if rel else "(retiré de l'index)",
                    (rel.annee_academique or "?") if rel else "", (rel.decision or rel.statut or "?") if rel else "",
                    str(rel.nb_pages) if rel else "", item["etat"] or ("à préparer" if d["statut"] == "EN COURS" else ""),
                    item["reference"] or ""]
            for j, v in enumerate(vals):
                if j == 3 and rel:
                    badge_cell(self.table, i, j, v, statut_tone(rel.statut))
                    continue
                if j == 5:
                    etat = item["etat"] or ""
                    self.table.setItem(i, j, QTableWidgetItem(""))
                    badge_cell(self.table, i, j, "Bloqué" if etat.startswith("BLOQUÉ") else (v or "—"),
                               "red" if etat.startswith("BLOQUÉ") else ("green" if etat == "PRÊT" else "gray"))
                    if etat.startswith("BLOQUÉ"):
                        self.table.cellWidget(i, j).setToolTip(etat)
                    continue
                it = QTableWidgetItem(v)
                if j in (0, 6):
                    it.setFont(mono_font(9.5 if j == 6 else 10.5))
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
    COLS = ("N° de demande", "Reçue le", "Étudiant", "Demandeur · motif", "Relevés", "Statut", "Prochaine étape")

    def __init__(self, db: Database, settings: Settings, parent=None):
        super().__init__(parent)
        self.db, self.s = db, settings
        self.previews = None
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)
        head = QVBoxLayout()
        head.setSpacing(4)
        head.addWidget(eyebrow("Guichet · une personne = une demande"))
        head.addWidget(title("Demandes de relevés", 22, "h1"))
        root.addLayout(head)

        stats = QHBoxLayout()
        stats.setSpacing(12)
        self.card_prep = StatCard("folder", "blue", "À préparer", "—")
        self.card_print = StatCard("printer", "violet", "Prêtes à imprimer", "—")
        self.card_give = StatCard("alert", "amber", "Imprimées, à remettre", "—")
        self.card_done = StatCard("check", "green", "Remises aujourd'hui", "—")
        for c in (self.card_prep, self.card_print, self.card_give, self.card_done):
            stats.addWidget(c)
        root.addLayout(stats)

        card = Card(padding=18, spacing=12)
        root.addWidget(card, 1)
        h = QHBoxLayout()
        h.addWidget(IconChip("folder", "violet", 42))
        h.addWidget(card_header("File d'attente", "Suivi des demandes"), 1)
        new_btn = QPushButton("Nouvelle demande   Ctrl+N")
        new_btn.setObjectName("primary")
        new_btn.clicked.connect(self.create)
        h.addWidget(new_btn, 0, Qt.AlignmentFlag.AlignTop)
        card.lay.addLayout(h)
        bar = QHBoxLayout()
        bar.setSpacing(12)
        bar.addWidget(eyebrow("Afficher"))
        self.seg = Segmented(list(self.FILTRES), "a_traiter")
        self.seg.changed.connect(lambda *_: self.refresh())
        bar.addWidget(self.seg)
        self.q = QLineEdit()
        self.q.setObjectName("field")
        self.q.setPlaceholderText("Filtrer : numéro, matricule, nom, demandeur…")
        self.q.textChanged.connect(lambda *_: self.refresh())
        bar.addWidget(self.q, 1)
        card.lay.addLayout(bar)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setObjectName("demandes")
        style_table(self.table, self.COLS)
        hh = self.table.horizontalHeader()
        for c, w in ((0, 140), (1, 120), (2, 250), (3, 250), (4, 80), (5, 130)):
            hh.resizeSection(c, w)
        hh.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(lambda *_: self.open_selected())
        card.lay.addWidget(self.table, 1)
        self.empty = muted("Aucune demande dans cette liste. Au guichet, « Ajouter ce relevé » (Ctrl+D) ouvre une "
                           "demande pour l'étudiant affiché.", 9.5)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card.lay.addWidget(self.empty)
        btns = QHBoxLayout()
        for label, slot, name in (("Ouvrir la demande", self.open_selected, "primary"),
                                  ("Reprendre au guichet", self.activate_selected, "secondary"),
                                  ("Ouvrir le dossier", self.folder_selected, "ghost")):
            b = QPushButton(label)
            b.setObjectName(name)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(muted("Double-clic : ouvrir · le dossier préparé contient le bordereau, les relevés "
                             "numérotés et « A_IMPRIMER_TOUT »", 8.5))
        card.lay.addLayout(btns)
        self.refresh()

    def refresh(self) -> None:
        rows = self.db.demandes(self.STATUTS[self.seg.value()])
        q = self.q.text().strip().lower()
        if q:
            rows = [r for r in rows if q in " ".join(str(r[k] or "") for k in
                                                     ("numero", "matricule", "nom", "demandeur", "motif")).lower()]
        self._rows = rows
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            vals = (r["numero"], (r["creee_le"] or "")[:16].replace("-", "/"),
                    f"{r['nom'] or '—'} ({r['matricule'] or '—'})",
                    " · ".join(x for x in (r["demandeur"], r["motif"]) if x) or "—", str(r["n"]), r["statut"],
                    STATUT_AIDE.get(r["statut"], ""))
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j == 0:
                    it.setFont(mono_font(10.5))
                if j == 6:
                    it.setForeground(QColor("#64748B"))
                if j == 5:
                    it.setForeground(QColor(0, 0, 0, 0))     # texte gardé pour le tri/les tests, la pastille l'affiche
                self.table.setItem(i, j, it)
                if j == 5:
                    badge_cell(self.table, i, j, v, STATUT_TONE.get(v, "gray"))
        self.table.setVisible(bool(rows))
        self.empty.setVisible(not rows)
        allr = self.db.demandes(demandes.OUVERTES)
        n = {st: sum(1 for r in allr if r["statut"] == st) for st in demandes.OUVERTES}
        today = __import__("datetime").date.today().isoformat()
        done = sum(1 for r in self.db.demandes(("REMISE",), limit=200) if (r["maj_le"] or "").startswith(today))
        self.card_prep.set(str(n["EN COURS"]))
        self.card_print.set(str(n["PRÊTE"]))
        self.card_give.set(str(n["IMPRIMÉE"]))
        self.card_done.set(str(done))

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
