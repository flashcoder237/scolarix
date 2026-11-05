"""Export / impression du cursus complet d'un étudiant (cas S1.4, S2.1, S2.2)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from core import pdf_tools
from core.config import Settings
from core.database import Database, Releve
from core.normalize import norm_text
from core.printer_service import print_pdf

from .widgets import TONES, Alert, Card, IconChip, Switch, Task, Toast, eyebrow, muted, statut_tone, title


class CursusDialog(QDialog):
    def __init__(self, db: Database, settings: Settings, matricule: str, parent=None):
        super().__init__(parent)
        self.db, self.s = db, settings
        self.setWindowTitle("Cursus complet")
        self.resize(820, 760)
        self.rels = db.cursus(matricule)
        self.correlated = db.correlated_matricules(matricule)
        self.extra: list[Releve] = []
        default = {r.id for r in pdf_tools.select_for_cursus(self.rels)}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(14)
        card = Card(padding=18, spacing=12, soft_shadow=False)
        lay.addWidget(card)
        head = QHBoxLayout()
        head.addWidget(IconChip("layers", "blue", 44))
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(eyebrow("Export du cursus complet"))
        first = self.rels[0] if self.rels else None
        col.addWidget(title(f"{first.matricule} · {first.nom_complet}" if first else matricule, 14))
        head.addLayout(col, 1)
        card.lay.addLayout(head)

        missing = self._missing_levels()
        if missing:
            card.lay.addWidget(Alert(f"Niveau(x) introuvable(s) dans l'index : {', '.join(missing)}. "
                                     "Le dossier fusionné sera incomplet.", "red"))
        if any(not r.is_success for r in self.rels):
            card.lay.addWidget(Alert("Les relevés d'échec sont décochés lorsqu'un relevé ADMIS existe pour le même "
                                     "niveau (rattrapage ou redoublement).", "amber"))

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Inclure", "Niveau", "Année", "Session", "Décision", "Pages"])
        self.tree.setRootIsDecorated(False)
        self.tree.setMinimumHeight(240)
        for r in self.rels:
            self._add_item(r, r.id in default)
        self.tree.itemChanged.connect(lambda *_: self._update_summary())
        for i, w in enumerate((70, 110, 110, 110, 120, 90)):
            self.tree.setColumnWidth(i, w)
        card.lay.addWidget(self.tree, 1)

        self.switch_link = None
        if self.correlated:
            c = self.correlated[0]
            box = QFrame()
            box.setObjectName("corr")
            box.setStyleSheet("QFrame#corr{background:#FFFBF0;border:1px solid #F6E3B4;border-radius:16px;}")
            bl = QHBoxLayout(box)
            bl.setContentsMargins(12, 10, 12, 10)
            bl.addWidget(IconChip("link", "amber", 38))
            txt = QVBoxLayout()
            t = QLabel(f"Corrélation probable : {c['matricule']} ({c['filiere']}, {c['n']} relevé(s))")
            f = t.font()
            f.setWeight(QFont.Weight.DemiBold)
            t.setFont(f)
            txt.addWidget(t)
            txt.addWidget(muted("Même nom et prénom" + (" et même date de naissance" if c["meme_naissance"] else "")
                                + " : changement de filière probable. Inclure après vérification de l'identité.", 8.5))
            bl.addLayout(txt, 1)
            self.switch_link = Switch(False)
            self.switch_link.setAccessibleName("Inclure le parcours rattaché")
            self.switch_link.toggled.connect(self._toggle_link)
            bl.addWidget(self.switch_link)
            card.lay.addWidget(box)

        opts = QHBoxLayout()
        self.sw_cover = Switch(True)
        self.sw_dup = Switch(False)
        for sw, label in ((self.sw_cover, "Page de garde récapitulative"), (self.sw_dup, "Timbre « Duplicata conforme »")):
            sw.setAccessibleName(label)
            sw.toggled.connect(lambda *_: self._update_summary())
            opts.addWidget(sw)
            opts.addWidget(QLabel(label))
            opts.addSpacing(16)
        opts.addStretch(1)
        card.lay.addLayout(opts)

        self.summary = QLabel()
        self.summary.setStyleSheet("QLabel{background:#0F172A;color:#E2E8F0;border-radius:14px;padding:10px 14px;}")
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card.lay.addWidget(self.summary)

        btns = QHBoxLayout()
        self.btn_merge = QPushButton("Fusionner et exporter   Ctrl+Maj+E")
        self.btn_merge.setObjectName("primary")
        self.btn_merge.clicked.connect(self.export)
        self.btn_print = QPushButton("Imprimer le cursus (recto-verso)")
        self.btn_print.setObjectName("secondary")
        self.btn_print.clicked.connect(self.print_)
        close = QPushButton("Fermer")
        close.setObjectName("ghost")
        close.clicked.connect(self.reject)
        btns.addWidget(self.btn_merge, 2)
        btns.addWidget(self.btn_print, 1)
        btns.addWidget(close)
        card.lay.addLayout(btns)
        self.toast = Toast()
        card.lay.addWidget(self.toast)
        self._update_summary()

    def _add_item(self, r: Releve, checked: bool) -> None:
        it = QTreeWidgetItem(["", r.niveau or "?", r.annee_academique or "?", r.session or "?", r.statut or "—",
                              r.pages_label])
        it.setData(0, Qt.ItemDataRole.UserRole, r.id)
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        it.setForeground(4, QColor(TONES[statut_tone(r.statut)][0]))
        self.tree.addTopLevelItem(it)

    def _missing_levels(self) -> list[str]:
        nums = {r.niveau_num for r in self.rels if 0 < r.niveau_num < 100}
        if not nums:
            return []
        return [f"Niveau {n}" for n in range(1, max(nums) + 1) if n not in nums]

    def _toggle_link(self, on: bool) -> None:
        if on and not self.extra:
            for c in self.correlated:
                self.extra.extend(self.db.cursus(c["matricule"]))
            self.tree.blockSignals(True)
            for r in self.extra:
                self._add_item(r, True)
            self.tree.blockSignals(False)
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.data(0, Qt.ItemDataRole.UserRole) in {r.id for r in self.extra}:
                it.setHidden(not on)
        self._update_summary()

    def selected(self) -> list[Releve]:
        by_id = {r.id: r for r in self.rels + self.extra}
        out = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if not it.isHidden() and it.checkState(0) == Qt.CheckState.Checked:
                out.append(by_id[it.data(0, Qt.ItemDataRole.UserRole)])
        out.sort(key=lambda r: (r.annee_academique or "", r.niveau_num,
                                0 if (r.session or "").lower().startswith("normal") else 1))
        return out

    def _filename(self) -> str:
        sel = self.selected()
        r = sel[0] if sel else (self.rels[0] if self.rels else None)
        if not r:
            return "FMSP_Cursus.pdf"
        nom = (norm_text(r.nom or r.nom_complet).split() or ["ETUDIANT"])[0]
        mat = self.rels[0].matricule if self.rels else r.matricule
        return f"FMSP_{mat}_{nom}_Cursus.pdf"

    def _update_summary(self) -> None:
        sel = self.selected()
        pages = sum(r.nb_pages for r in sel) + (1 if self.sw_cover.isChecked() and sel else 0)
        self.summary.setText(f"Fichier : {self._filename()}     ·     {len(sel)} relevé(s)     ·     {pages} pages")
        self.btn_merge.setEnabled(bool(sel))
        self.btn_print.setEnabled(bool(sel))

    def _build(self, dest: Path) -> tuple[Path, int, list[str]]:
        stamp = pdf_tools.stamp_text() if self.sw_dup.isChecked() else None
        return pdf_tools.merge_releves(self.selected(), dest, cover=self.sw_cover.isChecked(), stamp=stamp)

    def _log(self, action: str, detail: str) -> None:
        mat = self.rels[0].matricule if self.rels else None
        self.db.log(action, self.s.operateur, self.s.poste, None, mat, detail)

    def export(self) -> None:
        dest = self.s.export_dir() / self._filename()
        try:
            path, n, missing = self._build(dest)
        except Exception as exc:
            self.toast.show_message(f"Échec de la fusion : {exc}", "red")
            return
        self._log("CURSUS", f"{path.name} · {n} pages" + (" · DUPLICATA" if self.sw_dup.isChecked() else ""))
        extra = f" — introuvables : {', '.join(missing)}" if missing else ""
        self.toast.show_message(f"Exporté : {path} ({n} pages){extra}")

    def print_(self) -> None:
        tmp = Path(tempfile.gettempdir()) / self._filename()
        try:
            path, n, _ = self._build(tmp)
        except Exception as exc:
            self.toast.show_message(f"Échec de la fusion : {exc}", "red")
            return
        task = Task(print_pdf, path, self.s)
        task.signals.done.connect(lambda how: (self._log("IMPRESSION", f"Cursus {n} pages ({how})"),
                                               self.toast.show_message(f"Cursus envoyé à l'imprimante : {n} pages.")))
        task.signals.failed.connect(lambda e: self.toast.show_message(f"Impression impossible : {e}", "red"))
        QThreadPool.globalInstance().start(task)
