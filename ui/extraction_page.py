"""Extraction en masse pour le Décanat (S3.2) et journal de traçabilité (consultations, impressions)."""
from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from core import exporter
from core.config import Settings
from core.database import Database, Releve

from .icons import icon
from .widgets import Badge, Card, IconChip, PillBars, Segmented, Toast, card_header, eyebrow, muted, title

ACTIONS = {"IMPRESSION": "blue", "CONSULTATION": "gray", "DUPLICATA": "amber", "EXPORT PDF": "green",
           "EXTRACTION": "violet", "CURSUS": "green", "VERIFICATION": "red"}
MENTION_COLORS = [("Très bien", "#0F172A"), ("Bien", "#1D4ED8"), ("Assez bien", "#93B4F5"), ("Passable", "#D6E2FB")]


class MentionBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(14)
        self.counts: dict[str, int] = {}

    def set_counts(self, counts):
        self.counts = counts
        self.update()

    def paintEvent(self, _):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QPainter
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        total = sum(self.counts.values())
        if not total:
            p.setBrush(QColor("#EEF1F5"))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(0, 0, self.width(), 14), 7, 7)
            return
        x = 0.0
        p.setPen(Qt.PenStyle.NoPen)
        for label, color in MENTION_COLORS:
            n = self.counts.get(label, 0)
            if not n:
                continue
            w = self.width() * n / total
            p.setBrush(QColor(color))
            p.drawRoundedRect(QRectF(x, 0, max(w - 3, 4), 14), 7, 7)
            x += w


class ExtractionPage(QWidget):
    def __init__(self, db: Database, settings: Settings, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self.db, self.s = db, settings
        self.rows: list[Releve] = []
        self.statut = "tous"

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
        head.addWidget(eyebrow("Rapports · Décanat, DAAS & traçabilité"))
        head.addWidget(title("Extraction & journal", 22, "h1"))
        root.addLayout(head)

        main = QHBoxLayout()
        main.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(16)

        filt = Card(padding=20, spacing=14)
        h = QHBoxLayout()
        h.addWidget(IconChip("filter", "blue", 42))
        h.addWidget(card_header("Extraction en masse", "Liste pour rapport ministériel"), 1)
        filt.lay.addLayout(h)
        grid = QGridLayout()
        grid.setSpacing(12)
        self.fil = QComboBox()
        for code, label in ((None, "Toutes les filières"), ("M", "Médecine générale (M)"), ("P", "Pharmacie (P)"),
                            ("B", "Sciences biomédicales (B)"), ("MS", "Masters")):
            self.fil.addItem(label, code)
        self.y_from = QComboBox()
        self.y_to = QComboBox()
        self.niv = QComboBox()
        self.niv.addItem("Dernier niveau atteint (diplômés)", True)
        self.niv.addItem("Tous les niveaux", False)
        for i, (lab, w) in enumerate((("Filière", self.fil), ("Année de début", self.y_from),
                                      ("Année de fin", self.y_to), ("Niveau", self.niv))):
            box = QVBoxLayout()
            box.setSpacing(4)
            box.addWidget(eyebrow(lab))
            box.addWidget(w)
            grid.addLayout(box, 0, i)
            w.currentIndexChanged.connect(self.run)
        filt.lay.addLayout(grid)
        srow = QHBoxLayout()
        srow.addWidget(eyebrow("Statut"))
        self.seg = Segmented([("tous", "Tous"), ("admis", "Admis"), ("non_valides", "Ajournés / exclus")], "tous")
        self.seg.changed.connect(self._set_statut)
        srow.addWidget(self.seg)
        srow.addStretch(1)
        filt.lay.addLayout(srow)
        left.addWidget(filt)

        kpi = QHBoxLayout()
        kpi.setSpacing(16)
        count_card = Card(padding=20, spacing=10)
        count_card.lay.addWidget(eyebrow("Lignes trouvées"))
        self.count = QLabel("0")
        f = self.count.font()
        f.setPointSizeF(32)
        f.setWeight(QFont.Weight.Bold)
        self.count.setFont(f)
        count_card.lay.addWidget(self.count)
        self.scope = muted("", 9)
        count_card.lay.addWidget(self.scope)
        self.mbar = MentionBar()
        count_card.lay.addWidget(self.mbar)
        self.mlegend = muted("", 8.5)
        count_card.lay.addWidget(self.mlegend)
        kpi.addWidget(count_card, 1)
        promo_card = Card(padding=20, spacing=8)
        promo_card.lay.addWidget(eyebrow("Par année académique"))
        self.promos = PillBars(height=170)
        promo_card.lay.addWidget(self.promos)
        kpi.addWidget(promo_card, 2)
        left.addLayout(kpi)

        prev = Card(padding=14, spacing=10)
        ph = QHBoxLayout()
        ph.addWidget(card_header("Aperçu de l'export", "Premières lignes"), 1)
        prev.lay.addLayout(ph)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["MATRICULE", "NOM & PRÉNOM", "ANNÉE", "NIVEAU · SEM.", "MOY.", "DÉCISION"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet("QTableWidget{alternate-background-color:#F8FAFC;}")
        self.table.setMinimumHeight(320)
        hh = self.table.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        prev.lay.addWidget(self.table)
        brow = QHBoxLayout()
        xl = QPushButton(icon("download", "#FFFFFF", 18), "  Exporter en Excel (.xlsx)")
        xl.setObjectName("primary")
        xl.clicked.connect(lambda: self.export("xlsx"))
        csv = QPushButton("Exporter en CSV")
        csv.setObjectName("secondary")
        csv.clicked.connect(lambda: self.export("csv"))
        brow.addWidget(xl)
        brow.addWidget(csv)
        brow.addStretch(1)
        prev.lay.addLayout(brow)
        self.toast = Toast()
        prev.lay.addWidget(self.toast)
        left.addWidget(prev)
        main.addLayout(left, 58)

        # ---------------- journal
        log = Card(padding=20, spacing=12)
        lh = QHBoxLayout()
        lh.addWidget(IconChip("clock", "violet", 42))
        lh.addWidget(card_header("Traçabilité · non modifiable", "Journal des actions"), 1)
        log.lay.addLayout(lh)
        log.lay.addWidget(muted("Qui a consulté, imprimé ou réédité quel relevé, et quand : la preuve en cas de contestation.", 9))
        fl = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setObjectName("field")
        self.q.setPlaceholderText("Filtrer par matricule…")
        self.q.textChanged.connect(self.refresh_journal)
        self.period = QComboBox()
        for label, days in (("Aujourd'hui", 0), ("7 derniers jours", 7), ("30 derniers jours", 30), ("Tout", None)):
            self.period.addItem(label, days)
        self.period.currentIndexChanged.connect(self.refresh_journal)
        fl.addWidget(self.q, 1)
        fl.addWidget(self.period)
        log.lay.addLayout(fl)
        self.jtable = QTableWidget(0, 4)
        self.jtable.setHorizontalHeaderLabels(["HEURE", "ACTION", "MATRICULE · OPÉRATEUR", "DÉTAIL"])
        self.jtable.verticalHeader().hide()
        self.jtable.verticalHeader().setDefaultSectionSize(52)
        self.jtable.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jtable.setShowGrid(False)
        self.jtable.setWordWrap(True)
        self.jtable.setMinimumHeight(520)
        jh = self.jtable.horizontalHeader()
        jh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        jh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.jtable.setColumnWidth(0, 90)
        self.jtable.setColumnWidth(1, 140)
        self.jtable.setColumnWidth(2, 170)
        log.lay.addWidget(self.jtable, 1)
        jexp = QPushButton("Exporter le journal (CSV)")
        jexp.setObjectName("ghost")
        jexp.clicked.connect(self.export_journal)
        log.lay.addWidget(jexp)
        main.addWidget(log, 42)
        root.addLayout(main)
        self.reload_years()

    # ----------------------------------------------------------------
    def reload_years(self) -> None:
        years = self.db.distinct_years()
        for combo, default_last in ((self.y_from, False), (self.y_to, True)):
            combo.blockSignals(True)
            cur = combo.currentData()
            combo.clear()
            combo.addItem("Toutes", None)
            for y in years:
                combo.addItem(f"{y}-{y + 1}", y)
            if cur is not None and combo.findData(cur) >= 0:
                combo.setCurrentIndex(combo.findData(cur))
            combo.blockSignals(False)
        self.run()
        self.refresh_journal()

    def _set_statut(self, v) -> None:
        self.statut = v
        self.run()

    def run(self) -> None:
        self.rows = self.db.extraction(self.fil.currentData(), self.y_from.currentData(), self.y_to.currentData(),
                                       self.statut, bool(self.niv.currentData()))
        self.count.setText(f"{len(self.rows):,}".replace(",", " "))
        y1 = self.y_from.currentText() if self.y_from.currentData() else "début"
        y2 = self.y_to.currentText() if self.y_to.currentData() else "aujourd'hui"
        self.scope.setText(f"{self.fil.currentText()} · {y1} → {y2}")
        mentions: dict[str, int] = {}
        per_year: dict[str, int] = {}
        for r in self.rows:
            m = exporter.mention(r.moyenne)
            if m:
                mentions[m] = mentions.get(m, 0) + 1
            per_year[r.annee_academique or "?"] = per_year.get(r.annee_academique or "?", 0) + 1
        self.mbar.set_counts(mentions)
        self.mlegend.setText("   ".join(f"{k} · {mentions.get(k, 0)}" for k, _ in MENTION_COLORS))
        ys = sorted(per_year.items())
        self.promos.set_data([((a[2:4] + "-" + a[7:9]) if len(a) >= 9 else a, n,
                               "#1D4ED8" if i == len(ys) - 1 else "#93B4F5") for i, (a, n) in enumerate(ys)])
        shown = self.rows[:200]
        self.table.setRowCount(len(shown))
        mono = QFont("Consolas", 10)
        mono.setBold(True)
        for i, r in enumerate(shown):
            vals = [r.matricule, r.nom_complet, r.annee_academique or "", r.periode, r.moyenne or "", r.statut or ""]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j == 0:
                    it.setFont(mono)
                self.table.setItem(i, j, it)

    def _filename(self, ext: str) -> str:
        fil = self.fil.currentData() or "Toutes"
        y1 = self.y_from.currentData() or "debut"
        y2 = (self.y_to.currentData() + 1) if self.y_to.currentData() else "fin"
        return f"FMSP_Extraction_{fil}_{self.statut}_{y1}-{y2}.{ext}"

    def export(self, kind: str) -> None:
        if not self.rows:
            self.toast.show_message("Aucune ligne à exporter avec ces filtres.", "amber")
            return
        default = str(self.s.export_dir() / self._filename(kind))
        path, _ = QFileDialog.getSaveFileName(self, "Exporter", default,
                                              "Classeur Excel (*.xlsx)" if kind == "xlsx" else "CSV (*.csv)")
        if not path:
            return
        try:
            if kind == "xlsx":
                exporter.export_xlsx(self.rows, path, f"Extraction FMSP — {self.scope.text()}")
            else:
                exporter.export_csv(self.rows, path)
        except Exception as exc:
            self.toast.show_message(f"Export impossible : {exc}", "red")
            return
        self.db.log("EXTRACTION", self.s.operateur, self.s.poste, None, None,
                    f"{self.scope.text()} · {self.seg.value()} · {len(self.rows)} lignes · {kind}")
        self.toast.show_message(f"Exporté : {path} ({len(self.rows)} lignes) · extraction journalisée")
        self.refresh_journal()

    def refresh_journal(self) -> None:
        days = self.period.currentData()
        since = None
        if days is not None:
            since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d") if days else datetime.now().strftime("%Y-%m-%d")
        rows = self.db.journal(self.q.text().strip() or None, since, 1000)
        self._journal_rows = rows
        self.jtable.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.jtable.setItem(i, 0, QTableWidgetItem((r["horodatage"] or "")[5:16].replace("-", "/")))
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(4, 4, 4, 4)
            hl.addWidget(Badge(r["action"], ACTIONS.get(r["action"], "gray")))
            hl.addStretch(1)
            self.jtable.setCellWidget(i, 1, holder)
            self.jtable.setItem(i, 2, QTableWidgetItem(f"{r['matricule'] or '—'} · {r['operateur'] or ''}"))
            self.jtable.setItem(i, 3, QTableWidgetItem(r["detail"] or ""))

    def export_journal(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Exporter le journal",
                                              str(self.s.export_dir() / "FMSP_Journal.csv"), "CSV (*.csv)")
        if path:
            exporter.export_journal_csv(getattr(self, "_journal_rows", []), path)
            self.toast.show_message(f"Journal exporté : {path}")
