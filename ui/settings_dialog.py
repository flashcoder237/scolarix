"""Réglages du poste : identité, impression, indexation (exclusions, parallélisme), corrections Word."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QPushButton,
                               QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from core.config import DEFAULT_TEXT_RULES, Settings, config_path, save_settings
from core.indexer import default_workers
from core.text_rules import RuleError, apply_text, compile_rules

from .widgets import Card, card_header, muted


# ------------------------------------------------------------------ exclusions
class ExclusionsEditor(QWidget):
    """Liste des dossiers et motifs ignorés par l'indexation."""

    def __init__(self, items: list[str], root: str = "", parent=None):
        super().__init__(parent)
        self.root = root
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.list = QListWidget()
        self.list.setObjectName("exclusions")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.addItems([i for i in items if i])
        self.list.setMinimumHeight(120)
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        b_dir = QPushButton("Ajouter un sous-dossier…")
        b_dir.setObjectName("secondary")
        b_dir.clicked.connect(self.add_folder)
        b_pat = QPushButton("Ajouter un motif…")
        b_pat.setObjectName("secondary")
        b_pat.clicked.connect(self.add_pattern)
        b_del = QPushButton("Retirer")
        b_del.setObjectName("ghost")
        b_del.clicked.connect(self.remove_selected)
        for b in (b_dir, b_pat, b_del):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(muted("Un dossier exclu est ignoré avec tout son contenu. Un motif (* = n'importe quel texte) "
                            "s'applique au nom des dossiers ET des fichiers, sans tenir compte des majuscules ni des "
                            "accents : « *DIPLOME* », « Brouillons », « *attestation* ». Les fichiers déjà indexés qui "
                            "deviennent exclus sont retirés de l'index à la prochaine indexation (jamais du disque).", 8.5))

    def items(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def _add(self, text: str) -> None:
        text = text.strip()
        if text and text not in self.items():
            self.list.addItem(text)

    def add_folder(self) -> None:
        p = QFileDialog.getExistingDirectory(self, "Sous-dossier à exclure", self.root or "")
        if not p:
            return
        p = os.path.normpath(p)
        if self.root:
            try:
                rel = os.path.relpath(p, os.path.normpath(self.root))
                if not rel.startswith(".."):
                    p = os.path.join(os.path.normpath(self.root), rel)
            except ValueError:  # autre lecteur
                pass
        self._add(p)

    def add_pattern(self) -> None:
        text, ok = QInputDialog.getText(self, "Motif à exclure",
                                        "Nom ou motif (ex. *DIPLOME*, Brouillons, *.tmp) :")
        if ok:
            if any(sep in text for sep in ("/", "\\")):
                self._add(text)
            else:
                self._add(text.strip())

    def remove_selected(self) -> None:
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))


class ExclusionsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.setWindowTitle("Dossiers et fichiers exclus de l'indexation")
        self.resize(640, 420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.addWidget(card_header("Indexation", "Exclusions"))
        self.editor = ExclusionsEditor(list(settings.exclusions), settings.source_root)
        lay.addWidget(self.editor, 1)
        lay.addLayout(_buttons(self))

    def save(self) -> None:
        self.s.exclusions = self.editor.items()
        save_settings(self.s)
        self.accept()


# ------------------------------------------------------------ corrections Word
RULE_COLS = ("Actif", "Description", "Chercher", "Remplacer par", "Expr. régulière", "Taille (pt)")


class RulesEditor(QWidget):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.enabled = QCheckBox("Appliquer ces corrections aux relevés Word (aperçu, impression, export, cursus)")
        self.enabled.setChecked(settings.text_rules_enabled)
        lay.addWidget(self.enabled)
        self.table = QTableWidget(0, len(RULE_COLS))
        self.table.setObjectName("rules")
        self.table.setHorizontalHeaderLabels(RULE_COLS)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        for i, mode in enumerate((QHeaderView.ResizeMode.ResizeToContents, QHeaderView.ResizeMode.Interactive,
                                  QHeaderView.ResizeMode.Stretch, QHeaderView.ResizeMode.Stretch,
                                  QHeaderView.ResizeMode.ResizeToContents, QHeaderView.ResizeMode.ResizeToContents)):
            hh.setSectionResizeMode(i, mode)
        self.table.setColumnWidth(1, 170)
        for r in settings.text_rules:
            self._append(r)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        for label, slot, name in (("Ajouter", self.add_rule, "secondary"), ("Supprimer", self.remove_rule, "ghost"),
                                  ("Rétablir les règles d'origine", self.reset, "ghost")):
            b = QPushButton(label)
            b.setObjectName(name)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        test = QHBoxLayout()
        self.sample = QLineEdit("Tel  : 33 01 41 39  / 33 01 41 29.  RELEVE DE NOTES /TRANSCRIPT   FMSP/VDAAP/VDSSSE/SSSE")
        self.sample.setObjectName("field")
        self.sample.textChanged.connect(self.preview)
        self.table.itemChanged.connect(lambda *_: self.preview())
        test.addWidget(QLabel("Essai :"))
        test.addWidget(self.sample, 1)
        lay.addLayout(test)
        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result.setStyleSheet("QLabel{background:#0F172A;color:#E2E8F0;border-radius:12px;padding:8px 12px;}")
        lay.addWidget(self.result)
        lay.addWidget(muted("Uniquement les fichiers Word : la correction est faite sur une COPIE au moment de produire "
                            "l'aperçu, l'original n'est jamais modifié et les PDF/scans ne sont pas concernés. En texte "
                            "simple, les espaces acceptent n'importe quel nombre d'espaces. « Taille » met le texte ajouté "
                            "dans cette taille (utile pour ne pas allonger l'en-tête). Toute modification d'une règle "
                            "refait les aperçus à la consultation suivante.", 8.5))
        self.preview()

    def _append(self, r: dict) -> None:
        i = self.table.rowCount()
        self.table.insertRow(i)
        act = QTableWidgetItem()
        act.setFlags(act.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        act.setCheckState(Qt.CheckState.Checked if r.get("actif", True) else Qt.CheckState.Unchecked)
        self.table.setItem(i, 0, act)
        for col, key in ((1, "description"), (2, "chercher"), (3, "remplacer")):
            self.table.setItem(i, col, QTableWidgetItem(str(r.get(key) or "")))
        rx = QTableWidgetItem()
        rx.setFlags(rx.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        rx.setCheckState(Qt.CheckState.Checked if r.get("regex") else Qt.CheckState.Unchecked)
        self.table.setItem(i, 4, rx)
        self.table.setItem(i, 5, QTableWidgetItem(str(r.get("taille") or "")))

    def rules(self) -> list[dict]:
        out = []
        for i in range(self.table.rowCount()):
            def txt(c):
                it = self.table.item(i, c)
                return it.text() if it else ""
            size = txt(5).replace(",", ".").strip()
            rule = {"actif": self.table.item(i, 0).checkState() == Qt.CheckState.Checked,
                    "description": txt(1), "chercher": txt(2), "remplacer": txt(3),
                    "regex": self.table.item(i, 4).checkState() == Qt.CheckState.Checked}
            try:
                if size:
                    rule["taille"] = float(size)
            except ValueError:
                pass
            out.append(rule)
        return out

    def add_rule(self) -> None:
        self._append({"actif": True, "description": "Nouvelle règle", "chercher": "", "remplacer": "", "regex": False})

    def remove_rule(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        self.preview()

    def reset(self) -> None:
        self.table.setRowCount(0)
        for r in DEFAULT_TEXT_RULES:
            self._append(dict(r))
        self.preview()

    def validate(self) -> str | None:
        try:
            compile_rules(self.rules())
        except RuleError as exc:
            return str(exc)
        return None

    def preview(self) -> None:
        try:
            text, n = apply_text(self.sample.text(), compile_rules(self.rules()))
            self.result.setText(f"Résultat ({n} remplacement(s)) : {text}")
        except RuleError as exc:
            self.result.setText(f"⚠ {exc}")


# ------------------------------------------------------------------- dialogue
class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.setWindowTitle("Réglages du poste")
        self.resize(860, 680)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        card = Card(padding=20, spacing=14, soft_shadow=False)
        lay.addWidget(card)
        card.lay.addWidget(card_header("Configuration", "Réglages du poste"))
        self.tabs = QTabWidget()
        card.lay.addWidget(self.tabs, 1)
        self.fields: dict[str, QLineEdit] = {}

        # ---- Poste
        poste = QWidget()
        form = QFormLayout(poste)
        form.setSpacing(10)
        form.addRow("Opérateur (journal)", self._line("operateur"))
        form.addRow("Poste", self._line("poste"))
        self.printer = QComboBox()
        self.printer.addItem("Imprimante par défaut de Windows", "")
        try:
            from PySide6.QtPrintSupport import QPrinterInfo
            for name in QPrinterInfo.availablePrinterNames():
                self.printer.addItem(name, name)
        except Exception:
            pass
        self.printer.setCurrentIndex(max(0, self.printer.findData(settings.printer_name)))
        form.addRow("Imprimante", self.printer)
        self.duplex = QCheckBox("Recto-verso automatique pour les relevés sur 2 pages")
        self.duplex.setChecked(settings.duplex)
        form.addRow("", self.duplex)
        form.addRow("Commande d'impression",
                    self._line("print_command", r'"C:\Outils\SumatraPDF.exe" -print-to-default -silent "{file}"'))
        form.addRow("Tesseract (OCR)", self._line("tesseract_cmd", r"C:\Program Files\Tesseract-OCR\tesseract.exe", "file"))
        form.addRow("LibreOffice (soffice)",
                    self._line("soffice_cmd", r"C:\Program Files\LibreOffice\program\soffice.exe", "file"))
        form.addRow("Base d'index", self._line("db_path", str(settings.resolved_db_path()), "save"))
        form.addRow("Cache d'aperçu", self._line("cache_dir", str(settings.resolved_cache_dir()), "dir"))
        form.addRow("", muted("Laisser vide = détection automatique. La commande d'impression accepte {file} et "
                              "{printer}. Sans commande, l'impression passe par Qt sans boîte de dialogue. "
                              f"Fichier de configuration : {config_path()}. Changer la base d'index nécessite de "
                              "redémarrer l'application.", 8.5))
        self.tabs.addTab(poste, "Poste et impression")

        # ---- Indexation
        idx = QWidget()
        il = QVBoxLayout(idx)
        f2 = QFormLayout()
        self.workers = QSpinBox()
        self.workers.setRange(0, 16)
        self.workers.setValue(int(settings.index_workers or 0))
        self.workers.setSpecialValueText(f"Automatique ({default_workers()})")
        f2.addRow("Processus parallèles", self.workers)
        self.preconvert = QCheckBox("Préconvertir les .docx en PDF pendant l'indexation (aperçu immédiat, "
                                    "mais indexation beaucoup plus lente)")
        self.preconvert.setChecked(settings.docx_convert_at_index)
        f2.addRow("", self.preconvert)
        il.addLayout(f2)
        il.addWidget(muted(f"{os.cpu_count() or '?'} cœurs détectés. Chaque processus lit ses fichiers et, pour les .doc, "
                           "pilote sa propre instance de Word : au-delà de 4, le gain est faible et la mémoire augmente. "
                           "Sur un partage réseau lent, 2 à 3 processus suffisent souvent.", 8.5))
        il.addWidget(QLabel("Dossiers et motifs exclus"))
        self.exclusions = ExclusionsEditor(list(settings.exclusions), settings.source_root)
        il.addWidget(self.exclusions, 1)
        self.tabs.addTab(idx, "Indexation")

        # ---- Corrections Word
        self.rules = RulesEditor(settings)
        self.tabs.addTab(self.rules, "Corrections des relevés Word")

        self.error = QLabel()
        self.error.setStyleSheet("color:#B91C1C;")
        card.lay.addWidget(self.error)
        card.lay.addLayout(_buttons(self))

    def _line(self, key, placeholder="", browse=None):
        e = QLineEdit(str(getattr(self.s, key) or ""))
        e.setObjectName("field")
        e.setPlaceholderText(placeholder)
        self.fields[key] = e
        if not browse:
            return e
        row = QHBoxLayout()
        row.addWidget(e, 1)
        b = QPushButton("…")
        b.setObjectName("ghost")
        b.clicked.connect(lambda: self._browse(e, browse))
        row.addWidget(b)
        return row

    def _browse(self, edit: QLineEdit, mode: str) -> None:
        if mode == "dir":
            p = QFileDialog.getExistingDirectory(self, "Choisir un dossier", edit.text())
        elif mode == "save":
            p, _ = QFileDialog.getSaveFileName(self, "Base d'index", edit.text(), "SQLite (*.db)")
        else:
            p, _ = QFileDialog.getOpenFileName(self, "Choisir le programme", edit.text())
        if p:
            edit.setText(str(Path(p)))

    def save(self) -> None:
        err = self.rules.validate()
        if err:
            self.tabs.setCurrentWidget(self.rules)
            self.error.setText(err)
            return
        for key, e in self.fields.items():
            setattr(self.s, key, e.text().strip())
        self.s.printer_name = self.printer.currentData() or ""
        self.s.duplex = self.duplex.isChecked()
        self.s.index_workers = self.workers.value()
        self.s.docx_convert_at_index = self.preconvert.isChecked()
        self.s.exclusions = self.exclusions.items()
        self.s.text_rules = self.rules.rules()
        self.s.text_rules_enabled = self.rules.enabled.isChecked()
        save_settings(self.s)
        self.accept()


def _buttons(dlg) -> QHBoxLayout:
    btns = QHBoxLayout()
    btns.addStretch(1)
    cancel = QPushButton("Annuler")
    cancel.setObjectName("ghost")
    cancel.clicked.connect(dlg.reject)
    ok = QPushButton("Enregistrer")
    ok.setObjectName("primary")
    ok.clicked.connect(dlg.save)
    btns.addWidget(cancel)
    btns.addWidget(ok)
    return btns
