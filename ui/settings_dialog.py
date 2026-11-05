"""Réglages du poste : identité pour le journal, impression, outils externes, emplacements."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QVBoxLayout)

from core.config import Settings, config_path, save_settings

from .widgets import Card, card_header, muted


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.setWindowTitle("Réglages du poste")
        self.resize(720, 640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        card = Card(padding=20, spacing=14, soft_shadow=False)
        lay.addWidget(card)
        card.lay.addWidget(card_header("Configuration", "Réglages du poste"))
        form = QFormLayout()
        form.setSpacing(10)
        self.fields: dict[str, QLineEdit] = {}

        def line(key, placeholder="", browse=None):
            e = QLineEdit(str(getattr(settings, key) or ""))
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

        form.addRow("Opérateur (journal)", line("operateur"))
        form.addRow("Poste", line("poste"))
        self.printer = QComboBox()
        self.printer.addItem("Imprimante par défaut de Windows", "")
        try:
            from PySide6.QtPrintSupport import QPrinterInfo
            for name in QPrinterInfo.availablePrinterNames():
                self.printer.addItem(name, name)
        except Exception:
            pass
        idx = self.printer.findData(settings.printer_name)
        self.printer.setCurrentIndex(max(0, idx))
        form.addRow("Imprimante", self.printer)
        self.duplex = QCheckBox("Recto-verso automatique pour les relevés sur 2 pages")
        self.duplex.setChecked(settings.duplex)
        form.addRow("", self.duplex)
        form.addRow("Commande d'impression", line("print_command", r'"C:\Outils\SumatraPDF.exe" -print-to-default -silent "{file}"'))
        form.addRow("Tesseract (OCR)", line("tesseract_cmd", r"C:\Program Files\Tesseract-OCR\tesseract.exe", "file"))
        form.addRow("LibreOffice (soffice)", line("soffice_cmd", r"C:\Program Files\LibreOffice\program\soffice.exe", "file"))
        form.addRow("Base d'index", line("db_path", str(settings.resolved_db_path()), "save"))
        form.addRow("Cache d'aperçu", line("cache_dir", str(settings.resolved_cache_dir()), "dir"))
        card.lay.addLayout(form)
        card.lay.addWidget(muted("Laisser vide = détection automatique. La commande d'impression accepte {file} et "
                                 "{printer}. Sans commande, l'impression passe par Qt sans boîte de dialogue. "
                                 f"Fichier de configuration : {config_path()}", 8.5))
        card.lay.addWidget(muted("Changer la base d'index nécessite de redémarrer l'application.", 8.5))
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Annuler")
        cancel.setObjectName("ghost")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Enregistrer")
        ok.setObjectName("primary")
        ok.clicked.connect(self.save)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        card.lay.addLayout(btns)

    def _browse(self, edit: QLineEdit, mode: str) -> None:
        if mode == "dir":
            p = QFileDialog.getExistingDirectory(self, "Choisir un dossier", edit.text())
        elif mode == "save":
            p, _ = QFileDialog.getSaveFileName(self, "Base d'index", edit.text(), "SQLite (*.db)")
        else:
            p, _ = QFileDialog.getOpenFileName(self, "Choisir le programme", edit.text())
        if p:
            edit.setText(p)

    def save(self) -> None:
        for key, e in self.fields.items():
            setattr(self.s, key, e.text().strip())
        self.s.printer_name = self.printer.currentData() or ""
        self.s.duplex = self.duplex.isChecked()
        save_settings(self.s)
        self.accept()
