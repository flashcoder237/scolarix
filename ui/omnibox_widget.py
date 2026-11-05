"""Zone A : barre de recherche Omnibox (debounce 120 ms, normalisation silencieuse, navigation clavier)."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit

from core.normalize import looks_like_matricule, norm_matricule

from .icons import pixmap
from .widgets import Badge

DEBOUNCE_MS = 120


class Omnibox(QFrame):
    search = Signal(str)       # émis 120 ms après la dernière frappe
    navigate = Signal(int)     # ↑ / ↓ : parcourir les résultats sans quitter la saisie

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("omnibox")
        self.setMinimumHeight(60)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 6, 10, 6)
        lay.setSpacing(12)
        ico = QLabel()
        ico.setPixmap(pixmap("search", "#1D4ED8", 22, 2.2))
        lay.addWidget(ico)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Matricule, nom, prénom ou année — ex. 18M042, TCHOUA, 2021")
        self.edit.setAccessibleName("Rechercher un relevé par matricule, nom, prénom ou année")
        self.edit.setClearButtonEnabled(True)
        self.edit.installEventFilter(self)
        lay.addWidget(self.edit, 1)
        self.norm = Badge("", "blue")
        self.norm.hide()
        lay.addWidget(self.norm)
        kbd = QLabel("Échap")
        kbd.setStyleSheet("QLabel{border:1px solid #DCE2EA;border-bottom:2px solid #DCE2EA;border-radius:7px;"
                          "padding:2px 7px;font-family:Consolas,monospace;font-size:8pt;color:#0F172A;}")
        kbd.setToolTip("Échap ou Ctrl+F : nouvelle recherche (focus + sélection du texte)")
        lay.addWidget(kbd)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(lambda: self.search.emit(self.edit.text()))
        self.edit.textChanged.connect(self._changed)

    def _changed(self, text: str) -> None:
        mat = norm_matricule(text)
        if looks_like_matricule(text) and mat != text.strip().upper():
            self.norm.set(f"Recherché : {mat}", "blue")
            self.norm.show()
        else:
            self.norm.hide()
        self._timer.start()

    def focus_and_select(self) -> None:
        self.edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.edit.selectAll()

    def text(self) -> str:
        return self.edit.text()

    def eventFilter(self, obj, event):
        if obj is self.edit:
            if event.type() == QEvent.Type.FocusIn:
                self.setProperty("focused", True)
                self.style().unpolish(self)
                self.style().polish(self)
            elif event.type() == QEvent.Type.FocusOut:
                self.setProperty("focused", False)
                self.style().unpolish(self)
                self.style().polish(self)
            elif event.type() == QEvent.Type.KeyPress:
                key = event.key()
                if key == Qt.Key.Key_Down:
                    self.navigate.emit(1)
                    return True
                if key == Qt.Key.Key_Up:
                    self.navigate.emit(-1)
                    return True
                if key == Qt.Key.Key_PageDown:
                    self.navigate.emit(10)
                    return True
                if key == Qt.Key.Key_PageUp:
                    self.navigate.emit(-10)
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._timer.stop()
                    self.search.emit(self.edit.text())
                    return True
        return super().eventFilter(obj, event)
