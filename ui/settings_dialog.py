"""Réglages du poste : identité, impression, indexation (exclusions, parallélisme), corrections Word."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QFrame,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QPushButton,
                               QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from core.config import DEFAULT_TEXT_RULES, Settings, config_path, save_settings
from core.indexer import default_workers
from core.text_rules import RuleError, apply_text, compile_rules

from .widgets import Card, card_header, eyebrow, muted


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
RULE_COLS = ("Actif", "Description", "Chercher", "Remplacer par", "Sauf si déjà présent", "Regex", "Mise en forme")
C_ACT, C_DESC, C_FIND, C_REPL, C_UNLESS, C_RX, C_FMT = range(len(RULE_COLS))
STYLE_KEYS = ("police", "taille", "gras", "italique", "style_sur")
ORIGINE = "(police d'origine)"
COMMON_FONTS = ["Arial", "Arial Narrow", "Book Antiqua", "Bookman Old Style", "Calibri", "Cambria", "Century Gothic",
                "Garamond", "Georgia", "Segoe UI", "Tahoma", "Times New Roman", "Verdana"]
TRI_LABELS = (("Comme l'original", None), ("Oui", True), ("Non", False))


def style_summary(st: dict) -> str:
    bits = []
    if st.get("police"):
        bits.append(st["police"])
    if st.get("taille"):
        bits.append(f"{st['taille']:g} pt")
    if st.get("gras") is not None:
        bits.append("gras" if st["gras"] else "non gras")
    if st.get("italique") is not None:
        bits.append("italique" if st["italique"] else "droit")
    if bits and st.get("style_sur") == "tout":
        bits.append("tout le résultat")
    return " · ".join(bits) or "d'origine"


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
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        hh = self.table.horizontalHeader()
        for c in range(len(RULE_COLS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(C_FIND, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(C_REPL, QHeaderView.ResizeMode.Stretch)
        for col, w in ((C_ACT, 48), (C_DESC, 170), (C_UNLESS, 170), (C_RX, 56), (C_FMT, 150)):
            self.table.setColumnWidth(col, w)
        self._loading = True
        for r in settings.text_rules:
            self._append(r)
        self._loading = False
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

        # ---- mise en forme du texte de remplacement (règle sélectionnée)
        box = QFrame()
        box.setObjectName("soft")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(12, 10, 12, 10)
        bl.setSpacing(8)
        self.fmt_title = QLabel("Mise en forme du texte de remplacement")
        f = self.fmt_title.font()
        f.setWeight(QFont.Weight.DemiBold)
        self.fmt_title.setFont(f)
        bl.addWidget(self.fmt_title)
        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.font = QComboBox()
        self.font.setEditable(True)
        self.font.setMinimumWidth(210)
        self.font.addItem(ORIGINE)
        try:
            installed = set(QFontDatabase.families())
        except Exception:
            installed = set()
        self.font.addItems(sorted(set(COMMON_FONTS) | installed))
        self.font.setToolTip("Police du texte de remplacement. Elle doit être installée sur le poste qui produit "
                             "l'aperçu (sinon Word en substitue une autre).")
        self.size = QDoubleSpinBox()
        self.size.setRange(0, 72)
        self.size.setDecimals(1)
        self.size.setSingleStep(0.5)
        self.size.setSuffix(" pt")
        self.size.setSpecialValueText("taille d'origine")
        self.size.setMinimumWidth(140)
        self.bold = QComboBox()
        self.italic = QComboBox()
        for combo in (self.bold, self.italic):
            for label, value in TRI_LABELS:
                combo.addItem(label, value)
        self.scope = QComboBox()
        self.scope.addItem("au texte ajouté seulement", "ajout")
        self.scope.addItem("à tout le texte de remplacement", "tout")
        self.scope.setToolTip("« Texte ajouté » : seul ce qui change est mis en forme (« Ref N° » ajouté devant le "
                              "titre ne réduit pas le titre). « Tout le texte » : utile pour changer la police ou la "
                              "taille d'un texte existant (remplacer un texte par lui-même).")
        for label, w in (("Police", self.font), ("Taille", self.size), ("Gras", self.bold), ("Italique", self.italic),
                         ("Appliquer", self.scope)):
            col = QVBoxLayout()
            col.setSpacing(2)
            col.addWidget(eyebrow(label))
            col.addWidget(w)
            controls.addLayout(col)
        controls.addStretch(1)
        bl.addLayout(controls)
        self.sample_fmt = QLabel()
        self.sample_fmt.setMinimumHeight(44)
        self.sample_fmt.setStyleSheet("QLabel{background:#FFFFFF;color:#0F172A;border:1px solid #E3E7EF;"
                                      "border-radius:10px;padding:6px 12px;}")
        bl.addWidget(self.sample_fmt)
        lay.addWidget(box)
        self.font.currentTextChanged.connect(self._style_changed)
        self.size.valueChanged.connect(self._style_changed)
        for combo in (self.bold, self.italic, self.scope):
            combo.currentIndexChanged.connect(self._style_changed)
        self.table.currentCellChanged.connect(lambda *_: self._load_style())

        test = QHBoxLayout()
        self.sample = QLineEdit("Tel  : 33 01 41 39  / 33 01 41 29.  RELEVE DE NOTES /TRANSCRIPT   FMSP/VDAAP/VDSSSE/SSSE")
        self.sample.setObjectName("field")
        self.sample.textChanged.connect(self.preview)
        self.table.itemChanged.connect(lambda *_: (self.preview(), self._refresh_sample()))
        test.addWidget(QLabel("Essai :"))
        test.addWidget(self.sample, 1)
        lay.addLayout(test)
        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result.setStyleSheet("QLabel{background:#0F172A;color:#E2E8F0;border-radius:12px;padding:8px 12px;}")
        lay.addWidget(self.result)
        lay.addWidget(muted("« Sauf si déjà présent » : la règle est ignorée pour un en-tête qui contient déjà ce texte "
                            "(un relevé déjà corrigé à la main ne reçoit pas un second « Ref N° ») ; un texte trouvé "
                            "qui est déjà exactement le texte voulu n'est jamais remplacé.", 8.5))
        lay.addWidget(muted("Uniquement les fichiers Word : la correction est faite sur une COPIE au moment de produire "
                            "l'aperçu ; l'original n'est jamais modifié et les PDF/scans ne sont pas concernés. En texte "
                            "simple, les espaces acceptent n'importe quel nombre d'espaces. Une taille plus petite évite "
                            "d'allonger l'en-tête. Toute modification refait les aperçus à la consultation suivante.", 8.5))
        if self.table.rowCount():
            self.table.setCurrentCell(0, C_DESC)
        self._load_style()
        self.preview()

    # ------------------------------------------------------------ lignes
    def _append(self, r: dict) -> None:
        i = self.table.rowCount()
        self.table.insertRow(i)

        def check(col, on):
            it = QTableWidgetItem()
            it.setFlags((it.flags() | Qt.ItemFlag.ItemIsUserCheckable) & ~Qt.ItemFlag.ItemIsEditable)
            it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            self.table.setItem(i, col, it)

        check(C_ACT, r.get("actif", True))
        for col, key in ((C_DESC, "description"), (C_FIND, "chercher"), (C_REPL, "remplacer"),
                         (C_UNLESS, "sauf_si")):
            self.table.setItem(i, col, QTableWidgetItem(str(r.get(key) or "")))
        check(C_RX, r.get("regex"))
        style = {k: r.get(k) for k in STYLE_KEYS if r.get(k) not in (None, "")}
        fmt = QTableWidgetItem(style_summary(style))
        fmt.setFlags(fmt.flags() & ~Qt.ItemFlag.ItemIsEditable)
        fmt.setData(Qt.ItemDataRole.UserRole, style)
        fmt.setToolTip("Sélectionnez la règle pour modifier la police, la taille, le gras et l'italique ci-dessous.")
        self.table.setItem(i, C_FMT, fmt)

    def _style_item(self) -> QTableWidgetItem | None:
        row = self.table.currentRow()
        return self.table.item(row, C_FMT) if row >= 0 else None

    def _load_style(self) -> None:
        it = self._style_item()
        st = dict(it.data(Qt.ItemDataRole.UserRole) or {}) if it else {}
        self._loading = True
        self.font.setCurrentText(st.get("police") or ORIGINE)
        self.size.setValue(float(st.get("taille") or 0))
        self.bold.setCurrentIndex(self.bold.findData(st.get("gras")) if st.get("gras") is not None else 0)
        self.italic.setCurrentIndex(self.italic.findData(st.get("italique")) if st.get("italique") is not None else 0)
        self.scope.setCurrentIndex(1 if st.get("style_sur") == "tout" else 0)
        self._loading = False
        enabled = it is not None
        for w in (self.font, self.size, self.bold, self.italic, self.scope):
            w.setEnabled(enabled)
        desc = self.table.item(self.table.currentRow(), C_DESC) if enabled else None
        self.fmt_title.setText("Mise en forme du texte de remplacement"
                               + (f" — {desc.text()}" if desc and desc.text() else ""))
        self._refresh_sample()

    def _current_style(self) -> dict:
        st = {}
        font = self.font.currentText().strip()
        if font and font != ORIGINE:
            st["police"] = font
        if self.size.value() > 0:
            st["taille"] = round(self.size.value(), 1)
        for key, combo in (("gras", self.bold), ("italique", self.italic)):
            if combo.currentData() is not None:
                st[key] = combo.currentData()
        if self.scope.currentData() == "tout":
            st["style_sur"] = "tout"
        return st

    def _style_changed(self, *_):
        if self._loading:
            return
        it = self._style_item()
        if it is None:
            return
        st = self._current_style()
        it.setData(Qt.ItemDataRole.UserRole, st)
        it.setText(style_summary(st))   # déclenche itemChanged -> essai et aperçu mis à jour

    def _refresh_sample(self) -> None:
        """Le texte de remplacement de la règle sélectionnée, dans sa police et sa taille (aperçu à l'écran)."""
        row = self.table.currentRow()
        repl = self.table.item(row, C_REPL).text() if row >= 0 and self.table.item(row, C_REPL) else ""
        st = self._current_style() if row >= 0 else {}
        f = QFont(self.font.font())
        if st.get("police"):
            f.setFamily(st["police"])
        f.setPointSizeF(min(float(st.get("taille") or 11), 28))
        f.setBold(bool(st.get("gras")))
        f.setItalic(bool(st.get("italique")))
        self.sample_fmt.setFont(f)
        self.sample_fmt.setText(repl if repl else "(suppression : aucun texte de remplacement)")

    def rules(self) -> list[dict]:
        out = []
        for i in range(self.table.rowCount()):
            def txt(c):
                it = self.table.item(i, c)
                return it.text() if it else ""
            rule = {"actif": self.table.item(i, C_ACT).checkState() == Qt.CheckState.Checked,
                    "description": txt(C_DESC), "chercher": txt(C_FIND), "remplacer": txt(C_REPL),
                    "sauf_si": txt(C_UNLESS).strip(),
                    "regex": self.table.item(i, C_RX).checkState() == Qt.CheckState.Checked}
            rule.update(self.table.item(i, C_FMT).data(Qt.ItemDataRole.UserRole) or {})
            out.append(rule)
        return out

    def add_rule(self) -> None:
        self._append({"actif": True, "description": "Nouvelle règle", "chercher": "", "remplacer": "", "regex": False})
        self.table.setCurrentCell(self.table.rowCount() - 1, C_FIND)

    def remove_rule(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        self._load_style()
        self.preview()

    def reset(self) -> None:
        self.table.setRowCount(0)
        for r in DEFAULT_TEXT_RULES:
            self._append(dict(r))
        if self.table.rowCount():
            self.table.setCurrentCell(0, C_DESC)
        self._load_style()
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
        self.resize(1040, 760)
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
        self.sounds = QCheckBox("Sons discrets (succès, attention, erreur, impression)")
        self.sounds.setChecked(settings.ui_sounds)
        self.anims = QCheckBox("Petites animations (messages, changement d'écran, compteurs)")
        self.anims.setChecked(settings.ui_animations)
        form.addRow("Confort", self.sounds)
        form.addRow("", self.anims)
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

        # ---- Exports
        exp = QWidget()
        el = QVBoxLayout(exp)
        f3 = QFormLayout()
        f3.setSpacing(10)
        try:
            default_root = str(settings.exports_root_dir()) if not settings.exports_root else ""
        except OSError:
            default_root = ""
        f3.addRow("Dossier des exports", self._line("exports_root", default_root or "Documents\\Exports Scolarix", "dir"))
        self.retention = QSpinBox()
        self.retention.setRange(0, 3650)
        self.retention.setSuffix(" jours")
        self.retention.setSpecialValueText("jamais")
        self.retention.setValue(int(settings.exports_retention_days or 0))
        f3.addRow("Conserver relevés, cursus et vérifications", self.retention)
        self.ref_stamp = QCheckBox("Imprimer le numéro de délivrance en bas de chaque page délivrée")
        self.ref_stamp.setChecked(settings.delivery_ref_stamp)
        f3.addRow("", self.ref_stamp)
        el.addLayout(f3)
        tree = QLabel("<pre style='font-family:Consolas,monospace;font-size:9pt;color:#334155'>"
                      "Exports Scolarix\\\n"
                      "├─ Releves\\2026\\11\\     FMSP_98EM101_NOM_2023_Niv1_S1_20261106-0812.pdf\n"
                      "├─ Duplicatas\\2026\\11\\  …_DUPLICATA_20261106-0815.pdf      (conservés)\n"
                      "├─ Cursus\\2026\\11\\      FMSP_98EM101_NOM_Cursus_20261106-0820.pdf\n"
                      "├─ Extractions\\2026\\11\\ Decanat_M_admis_2022-2024_20261106-0900.xlsx (conservées)\n"
                      "├─ Verifications\\2026\\11\\\n"
                      "└─ Journal\\                                                  (conservés)</pre>")
        tree.setTextFormat(Qt.TextFormat.RichText)
        el.addWidget(tree)
        el.addWidget(muted("Classement par nature de demande puis par mois ; un fichier n'est jamais écrasé (date et "
                           "heure dans le nom). Chaque document délivré (impression, export, duplicata, cursus) reçoit "
                           "un numéro de délivrance « 00042/POSTE/2026 » et son empreinte est enregistrée : l'écran "
                           "Vérification reconnaît ensuite un fichier sorti du guichet. Un dossier partagé du service "
                           "(\\\\serveur\\Scolarité\\Exports) réunit les exports de tous les guichets ; réservez-en "
                           "l'accès au personnel habilité (les documents contiennent des notes).", 8.5))
        open_btn = QPushButton("Ouvrir le dossier des exports")
        open_btn.setObjectName("secondary")
        open_btn.clicked.connect(self._open_exports)
        row_open = QHBoxLayout()
        row_open.addWidget(open_btn)
        row_open.addStretch(1)
        el.addLayout(row_open)
        el.addStretch(1)
        self.tabs.addTab(exp, "Exports")

        # ---- Corrections Word
        self.rules = RulesEditor(settings)
        self.tabs.addTab(self.rules, "Corrections des relevés Word")

        self.error = QLabel()
        self.error.setStyleSheet("color:#B91C1C;")
        card.lay.addWidget(self.error)
        card.lay.addLayout(_buttons(self))

    def _open_exports(self) -> None:
        from core.exports import reveal
        root = self.fields["exports_root"].text().strip()
        try:
            reveal(root if root and Path(root).exists() else self.s.exports_root_dir())
        except Exception as exc:
            self.error.setText(f"Dossier inaccessible : {exc}")

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
        from core.config import is_network_path
        db_path = self.fields["db_path"].text().strip()
        if db_path and is_network_path(db_path):
            self.tabs.setCurrentIndex(0)
            self.error.setText("La base d'index doit rester sur le disque local du poste (pas sur un partage réseau : "
                               "risque de corruption avec plusieurs guichets).")
            return
        err = self.rules.validate()
        if err:
            self.tabs.setCurrentWidget(self.rules)
            self.error.setText(err)
            return
        for key, e in self.fields.items():
            setattr(self.s, key, e.text().strip())
        self.s.printer_name = self.printer.currentData() or ""
        self.s.duplex = self.duplex.isChecked()
        self.s.ui_sounds = self.sounds.isChecked()
        self.s.ui_animations = self.anims.isChecked()
        self.s.index_workers = self.workers.value()
        self.s.exports_retention_days = self.retention.value()
        self.s.delivery_ref_stamp = self.ref_stamp.isChecked()
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
