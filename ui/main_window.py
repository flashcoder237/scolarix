"""Fenêtre principale : rail de navigation + écrans, raccourcis clavier globaux."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QStackedWidget,
                               QVBoxLayout, QWidget)

from core.config import APP_NAME, APP_VERSION, Settings
from core.database import Database

from .extraction_page import ExtractionPage
from .guichet_page import GuichetPage
from .icons import pixmap
from .indexing_page import IndexingPage
from .settings_dialog import SettingsDialog
from .verification_page import VerificationPage
from .widgets import NavButton, shadow


class MainWindow(QMainWindow):
    def __init__(self, db: Database, settings: Settings):
        super().__init__()
        self.db, self.s = db, settings
        self.setWindowTitle(f"{APP_NAME} — v{APP_VERSION}")
        self.resize(1480, 940)
        self.setMinimumSize(1100, 700)

        central = QWidget()
        central.setObjectName("page")
        self.setCentralWidget(central)
        lay = QHBoxLayout(central)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(18)

        # ----------------------------------------------- rail
        rail = QFrame()
        rail.setObjectName("rail")
        rail.setFixedWidth(100)
        shadow(rail)
        rl = QVBoxLayout(rail)
        rl.setContentsMargins(8, 20, 8, 18)
        rl.setSpacing(10)
        logo = QLabel()
        logo.setFixedSize(52, 52)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setPixmap(pixmap("doc", "#FFFFFF", 24))
        logo.setStyleSheet("QLabel{background:#0F172A;border-radius:16px;}")
        rl.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)
        brand = QLabel("FMSP")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.setStyleSheet("font-size:8pt;font-weight:800;letter-spacing:3px;")
        rl.addWidget(brand)
        rl.addSpacing(18)

        self.stack = QStackedWidget()
        self.guichet = GuichetPage(db, settings)
        self.indexing = IndexingPage(db, settings)
        self.extraction = ExtractionPage(db, settings)
        self.verification = VerificationPage(db, settings)
        for page in (self.guichet, self.indexing, self.extraction, self.verification):
            self.stack.addWidget(page)

        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        for i, (ic, label) in enumerate((("search", "Guichet"), ("database", "Indexation"),
                                         ("chart", "Extraction"), ("shield", "Vérification"))):
            b = NavButton(ic, label)
            b.clicked.connect(lambda _=False, idx=i: self.go(idx))
            self.nav.addButton(b, i)
            rl.addWidget(b, 0, Qt.AlignmentFlag.AlignHCenter)
        self.nav.button(0).setChecked(True)
        rl.addStretch(1)
        settings_btn = NavButton("sliders", "Réglages")
        settings_btn.setCheckable(False)
        settings_btn.clicked.connect(self.open_settings)
        rl.addWidget(settings_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        self.avatar = QLabel(_initials(settings.operateur))
        self.avatar.setFixedSize(46, 46)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar.setToolTip(f"Opérateur : {settings.operateur}")
        self.avatar.setStyleSheet("QLabel{background:#E0EAFF;color:#1D4ED8;border-radius:15px;font-weight:800;}")
        rl.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(rail)
        lay.addWidget(self.stack, 1)

        # ----------------------------------------------- liens entre écrans
        self.guichet.open_settings.connect(self.open_settings)
        self.indexing.progress.connect(self.guichet.on_indexing_progress)
        self.indexing.finished.connect(self._after_indexing)

        # ----------------------------------------------- raccourcis (loi n° 5 : 100 % clavier)
        for seq in ("Esc", "Ctrl+F"):
            QShortcut(QKeySequence(seq), self, activated=self.focus_search)
        QShortcut(QKeySequence("Ctrl+P"), self, activated=lambda: self._on_guichet(self.guichet.print_current))
        QShortcut(QKeySequence("Ctrl+E"), self, activated=lambda: self._on_guichet(self.guichet.export_current))
        QShortcut(QKeySequence("Ctrl+Shift+E"), self, activated=lambda: self._on_guichet(self.guichet.open_cursus))
        for i in range(4):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda idx=i: self.go(idx))

        self.focus_search()

    def go(self, idx: int) -> None:
        self.stack.setCurrentIndex(idx)
        self.nav.button(idx).setChecked(True)
        if idx == 1:
            self.indexing.refresh()
        elif idx == 2:
            self.extraction.reload_years()
        elif idx == 0:
            self.guichet.refresh_stats()

    def focus_search(self) -> None:
        self.go(0)
        self.guichet.focus_search()

    def _on_guichet(self, fn) -> None:
        if self.stack.currentIndex() == 0:
            fn()

    def _after_indexing(self) -> None:
        self.guichet.refresh_stats()
        self.guichet.run_search(self.guichet.omni.text())

    def open_settings(self) -> None:
        if SettingsDialog(self.s, self).exec():
            self.guichet.refresh_header()
            self.avatar.setText(_initials(self.s.operateur))

    def closeEvent(self, event):
        if self.indexing.is_running():
            r = QMessageBox.question(self, "Indexation en cours",
                                     "Le robot d'indexation travaille encore. L'arrêter et quitter ?\n"
                                     "(Une indexation différentielle reprendra là où il s'est arrêté.)")
            if r != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.indexing.proc.kill()
            self.indexing.proc.waitForFinished(3000)
        super().closeEvent(event)


def _initials(name: str) -> str:
    parts = [p for p in (name or "?").replace(".", " ").replace("_", " ").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"
