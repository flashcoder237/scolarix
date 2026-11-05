"""Vérification d'authenticité (S3.1) : document soumis et original indexé, côte à côte."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from core import pdf_tools, verification
from core.config import Settings
from core.database import Database, Releve
from core.extractors import pdf_pages_text
from core.printer_service import print_pdf

from .icons import icon
from .pdf_preview_panel import PdfViewer
from .widgets import TONES, Badge, Card, IconChip, Ring, Task, Toast, eyebrow, muted, title


class VerificationPage(QWidget):
    def __init__(self, db: Database, settings: Settings, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self.db, self.s = db, settings
        self.original: Releve | None = None
        self.submitted_path: str | None = None
        self.rows: list[verification.Comparison] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        body.setObjectName("page")
        scroll.setWidget(body)
        root = QVBoxLayout(body)
        root.setContentsMargins(0, 0, 8, 8)
        root.setSpacing(16)

        head = QVBoxLayout()
        head.setSpacing(4)
        head.addWidget(eyebrow("Contrôle externe · ambassade, Ordre, université étrangère"))
        head.addWidget(title("Vérification d'authenticité", 22, "h1"))
        root.addLayout(head)

        top = QHBoxLayout()
        top.setSpacing(16)
        form = Card(padding=20, spacing=12)
        grid = QGridLayout()
        grid.setSpacing(12)
        self.demandeur = QLineEdit()
        self.reference = QLineEdit()
        self.matricule = QLineEdit()
        for w, ph in ((self.demandeur, "Organisme demandeur"), (self.reference, "N° de courrier / référence"),
                      (self.matricule, "Matricule déclaré, ex. 18M042")):
            w.setObjectName("field")
            w.setPlaceholderText(ph)
        self.matricule.returnPressed.connect(self.find_original)
        self.choice = QComboBox()
        self.choice.currentIndexChanged.connect(self._choose)
        for i, (lab, w) in enumerate((("Organisme demandeur", self.demandeur), ("Référence", self.reference),
                                      ("Matricule déclaré", self.matricule), ("Relevé d'origine", self.choice))):
            box = QVBoxLayout()
            box.setSpacing(4)
            box.addWidget(eyebrow(lab))
            box.addWidget(w)
            grid.addLayout(box, i // 2, i % 2)
        form.lay.addLayout(grid)
        brow = QHBoxLayout()
        find = QPushButton(icon("search", "#1D4ED8", 16), "  Rechercher l'original")
        find.setObjectName("secondary")
        find.clicked.connect(self.find_original)
        load = QPushButton(icon("upload", "#FFFFFF", 16), "  Charger le document soumis (PDF, JPG, PNG)")
        load.setObjectName("ink")
        load.clicked.connect(self.load_submitted)
        brow.addWidget(find)
        brow.addWidget(load)
        brow.addStretch(1)
        form.lay.addLayout(brow)
        top.addWidget(form, 3)

        self.verdict = QFrame()
        self.verdict.setObjectName("card")
        vl = QHBoxLayout(self.verdict)
        vl.setContentsMargins(20, 18, 20, 18)
        vl.setSpacing(16)
        self.ring = Ring()
        vl.addWidget(self.ring)
        vc = QVBoxLayout()
        vc.addWidget(eyebrow("Verdict"))
        self.v_title = title("En attente des deux documents", 13)
        self.v_title.setWordWrap(True)
        vc.addWidget(self.v_title)
        self.v_text = muted("Chargez le document soumis et sélectionnez l'original pour lancer la comparaison.", 9)
        vc.addWidget(self.v_text)
        vl.addLayout(vc, 1)
        top.addWidget(self.verdict, 2)
        root.addLayout(top)

        docs = QHBoxLayout()
        docs.setSpacing(16)
        sub_card = Card(padding=12, spacing=10)
        sh = QHBoxLayout()
        sh.addWidget(IconChip("upload", "amber", 36))
        sh.addWidget(title("Document soumis", 11.5), 1)
        self.sub_badge = Badge("NON CHARGÉ", "gray")
        sh.addWidget(self.sub_badge)
        sub_card.lay.addLayout(sh)
        self.sub_viewer = PdfViewer()
        self.sub_viewer.show_message("Aucun document soumis.")
        self.sub_image = QLabel()
        self.sub_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sub_image.setStyleSheet("background:#EEF1F5;border-radius:16px;")
        self.sub_image.setMinimumHeight(420)
        self.sub_image.hide()
        sub_card.lay.addWidget(self.sub_viewer, 1)
        sub_card.lay.addWidget(self.sub_image, 1)
        docs.addWidget(sub_card)
        orig_card = Card(padding=12, spacing=10)
        oh = QHBoxLayout()
        oh.addWidget(IconChip("shield", "green", 36))
        oh.addWidget(title("Original indexé", 11.5), 1)
        oh.addWidget(Badge("ARCHIVE OFFICIELLE", "green"))
        orig_card.lay.addLayout(oh)
        self.orig_viewer = PdfViewer()
        self.orig_viewer.show_message("Recherchez l'original par matricule.")
        orig_card.lay.addWidget(self.orig_viewer, 1)
        docs.addWidget(orig_card)
        root.addLayout(docs)

        cmp_card = Card(padding=14, spacing=10)
        cmp_card.lay.addWidget(eyebrow("Comparaison champ par champ"))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["CHAMP", "DOCUMENT SOUMIS", "ORIGINAL INDEXÉ", "RÉSULTAT"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setMinimumHeight(360)
        hh = self.table.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in (1, 2):
            self.table.setColumnWidth(i, 200)
        self.table.setColumnWidth(3, 150)
        cmp_card.lay.addWidget(self.table)
        arow = QHBoxLayout()
        rep = QPushButton("Générer le rapport de vérification (PDF)")
        rep.setObjectName("primary")
        rep.clicked.connect(self.report)
        pr = QPushButton("Imprimer l'original (timbre « Vérifié »)")
        pr.setObjectName("secondary")
        pr.clicked.connect(self.print_original)
        arow.addWidget(rep)
        arow.addWidget(pr)
        arow.addStretch(1)
        cmp_card.lay.addLayout(arow)
        self.toast = Toast()
        cmp_card.lay.addWidget(self.toast)
        root.addWidget(cmp_card)

    # ----------------------------------------------------------------
    def find_original(self) -> None:
        rels = self.db.cursus(self.matricule.text())
        self.choice.blockSignals(True)
        self.choice.clear()
        for r in rels:
            self.choice.addItem(f"{r.niveau} · {r.annee_academique} · {r.session} · {r.statut or '?'}", r.id)
        self.choice.blockSignals(False)
        if not rels:
            self.toast.show_message("Aucun relevé indexé pour ce matricule : document probablement non authentique "
                                    "ou matricule erroné.", "red")
            self.original = None
            return
        self.choice.setCurrentIndex(len(rels) - 1)
        self._choose()

    def _choose(self) -> None:
        rid = self.choice.currentData()
        self.original = self.db.get(rid) if rid else None
        if self.original:
            self.orig_viewer.show_page(self.original.preview_pdf, self.original.numero_page - 1)
            self.compare()

    def load_submitted(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Document soumis", str(Path.home()),
                                              "Documents (*.pdf *.png *.jpg *.jpeg *.tif *.tiff)")
        if not path:
            return
        self.submitted_path = path
        if path.lower().endswith(".pdf"):
            self.sub_image.hide()
            self.sub_viewer.show()
            self.sub_viewer.show_page(path, 0)
        else:
            self.sub_viewer.hide()
            pm = QPixmap(path)
            self.sub_image.setPixmap(pm.scaled(560, 760, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
            self.sub_image.show()
        self.sub_badge.set("LECTURE…", "blue")
        self.compare()

    def compare(self) -> None:
        if not (self.original and self.submitted_path):
            return
        orig = self.original
        sub_path = self.submitted_path

        def work():
            sub_text = verification.read_submitted(sub_path, self.s)
            pages = pdf_pages_text(Path(orig.preview_pdf)) if orig.preview_pdf else []
            orig_text = "\n".join(p.text for p in pages[orig.numero_page - 1:orig.page_fin])
            return verification.compare(sub_text, orig_text, orig)

        task = Task(work)
        task.signals.done.connect(self._show_rows)
        task.signals.failed.connect(lambda e: self.toast.show_message(f"Lecture impossible : {e}", "red"))
        QThreadPool.globalInstance().start(task)

    def _show_rows(self, rows) -> None:
        self.rows = rows
        ok = sum(1 for r in rows if r.conforme)
        bad = len(rows) - ok
        self.ring.set(ok, len(rows))
        self.sub_badge.set("LU", "green")
        tone = "red" if bad else "green"
        fg, bg = TONES[tone]
        self.verdict.setStyleSheet(f"QFrame#card{{background:{bg};border:1px solid {bg};border-radius:20px;}}")
        self.v_title.setStyleSheet(f"color:{fg};")
        self.v_title.setText(f"{bad} divergence(s) : document soumis non conforme" if bad
                             else "Document conforme à l'original indexé")
        self.v_text.setText("Comparez visuellement les lignes en rouge sur les deux documents, puis générez le rapport."
                            if bad else "Toutes les informations lues concordent avec l'archive officielle.")
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j, v in enumerate((r.champ, r.soumis, r.original)):
                it = QTableWidgetItem(v)
                if not r.conforme:
                    it.setBackground(QColor("#FFF6F6"))
                self.table.setItem(i, j, it)
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(4, 4, 4, 4)
            hl.addWidget(Badge("CONFORME" if r.conforme else "DIVERGENT", "green" if r.conforme else "red"))
            hl.addStretch(1)
            self.table.setCellWidget(i, 3, holder)
        self.db.log("VERIFICATION", self.s.operateur, self.s.poste, self.original.id if self.original else None,
                    self.original.matricule if self.original else None,
                    f"{self.demandeur.text() or 'demandeur ?'} · réf. {self.reference.text() or '?'} · {bad} divergence(s)")

    def report(self) -> None:
        if not (self.rows and self.original):
            self.toast.show_message("Comparez d'abord un document soumis avec l'original.", "amber")
            return
        dest = self.s.export_dir() / f"FMSP_Verification_{self.original.matricule}.pdf"
        verification.report_pdf(self.rows, self.original, dest, self.demandeur.text(), self.reference.text(),
                                self.s.operateur)
        self.toast.show_message(f"Rapport généré : {dest}")

    def print_original(self) -> None:
        o = self.original
        if not o or not o.preview_pdf:
            self.toast.show_message("Aucun original sélectionné.", "amber")
            return
        tmp = pdf_tools.extract_pages(o.preview_pdf, o.numero_page, o.page_fin, stamp=pdf_tools.stamp_text("VÉRIFIÉ"))
        task = Task(print_pdf, tmp, self.s)
        task.signals.done.connect(lambda _: (self.db.log("IMPRESSION", self.s.operateur, self.s.poste, o.id, o.matricule,
                                                         f"Original vérifié · {o.pages_label}"),
                                             self.toast.show_message("Original envoyé à l'imprimante avec le timbre « Vérifié ».")))
        task.signals.failed.connect(lambda e: self.toast.show_message(f"Impression impossible : {e}", "red"))
        QThreadPool.globalInstance().start(task)
