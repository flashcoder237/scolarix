"""Fenêtre principale : rail de navigation + écrans, raccourcis clavier globaux."""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QStackedWidget,
                               QVBoxLayout, QWidget)

from core.config import APP_NAME, APP_VERSION, UI_SCALES, Settings, save_settings
from core.database import Database

from .extraction_page import ExtractionPage
from .guichet_page import GuichetPage
from .demandes_ui import DemandesPage
from .icons import pixmap
from .indexing_page import IndexingPage
from .settings_dialog import SettingsDialog
from .verification_page import VerificationPage
from .widgets import NavButton, Toast, shadow


class MainWindow(QMainWindow):
    def __init__(self, db: Database, settings: Settings, previews=None):
        super().__init__()
        self.db, self.s = db, settings
        if previews is None:
            from core.preview import PreviewService
            previews = PreviewService(db, settings)
        self.previews = previews
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
        from .app import resource_path
        logo_png = resource_path("assets", "scolarix_256.png")
        if logo_png.exists():
            from PySide6.QtGui import QPixmap
            logo.setPixmap(QPixmap(str(logo_png)).scaled(52, 52, Qt.AspectRatioMode.KeepAspectRatio,
                                                         Qt.TransformationMode.SmoothTransformation))
        else:
            logo.setPixmap(pixmap("doc", "#FFFFFF", 24))
            logo.setStyleSheet("QLabel{background:#0F172A;border-radius:16px;}")
        logo.setToolTip("Scolarix — Archives des relevés de notes")
        rl.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)
        brand = QLabel("SCOLARIX")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.setStyleSheet("font-size:7pt;font-weight:800;letter-spacing:1px;")
        rl.addWidget(brand)
        rl.addSpacing(18)

        self.stack = QStackedWidget()
        self.guichet = GuichetPage(db, settings, previews)
        self.indexing = IndexingPage(db, settings)
        self.extraction = ExtractionPage(db, settings)
        self.verification = VerificationPage(db, settings)
        self.verification.previews = previews
        self.demandes = DemandesPage(db, settings)
        self.demandes.previews = previews
        for page in (self.guichet, self.demandes, self.indexing, self.extraction, self.verification):
            self.stack.addWidget(page)

        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        for i, (ic, label) in enumerate((("search", "Guichet"), ("folder", "Demandes"), ("database", "Indexation"),
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
        self.demandes.activate.connect(self._activate_demande)
        self.stack.currentChanged.connect(self._page_changed)

        # ----------------------------------------------- raccourcis (loi n° 5 : 100 % clavier)
        for seq in ("Esc", "Ctrl+F"):
            QShortcut(QKeySequence(seq), self, activated=self.focus_search)
        QShortcut(QKeySequence("Ctrl+P"), self, activated=lambda: self._on_guichet(self.guichet.print_current))
        QShortcut(QKeySequence("Ctrl+E"), self, activated=lambda: self._on_guichet(self.guichet.export_current))
        QShortcut(QKeySequence("Ctrl+Shift+E"), self, activated=lambda: self._on_guichet(self.guichet.open_cursus))
        QShortcut(QKeySequence("Ctrl+D"), self, activated=lambda: self._on_guichet(self.guichet.add_to_demande))
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self._new_demande)
        for i in range(5):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda idx=i: self.go(idx))
        # Taille de toute l'interface, comme dans un navigateur (Ctrl + molette zoome le document seul)
        # (une même touche déclarée deux fois rendrait le raccourci ambigu : aucun ne marcherait)
        zoom_keys: dict[str, int] = {}
        for seqs, step in (((QKeySequence.StandardKey.ZoomIn, "Ctrl++", "Ctrl+="), +1),
                           ((QKeySequence.StandardKey.ZoomOut, "Ctrl+-"), -1), (("Ctrl+0",), 0)):
            for seq in seqs:
                for ks in QKeySequence.keyBindings(seq) if not isinstance(seq, str) else [QKeySequence(seq)]:
                    zoom_keys.setdefault(ks.toString(), step)
        for key, step in zoom_keys.items():
            QShortcut(QKeySequence(key), self, activated=lambda st=step: self.change_scale(st))
        self.toast = Toast()
        self.toast.float_over(self.stack)

        self.focus_search()

    def _activate_demande(self, did: int) -> None:
        self.guichet.demande_bar.set_active(did)
        self.go(0)
        self.focus_search()

    def _page_changed(self, idx: int) -> None:
        from . import effects
        effects.fade_in(self.stack.widget(idx), 160)
        if self.stack.widget(idx) is self.demandes:
            self.demandes.refresh()
        elif self.stack.widget(idx) is self.guichet:
            self.guichet.demande_bar.refresh()

    def go(self, idx: int) -> None:
        self.stack.setCurrentIndex(idx)
        self.nav.button(idx).setChecked(True)
        page = self.stack.widget(idx)          # par la page, pas par son rang : l'ordre du menu peut changer
        if page is self.indexing:
            self.indexing.refresh()
        elif page is self.extraction:
            self.extraction.reload_years()     # années, chiffres et journal du jour à jour
        elif page is self.guichet:
            self.guichet.refresh_stats()

    def focus_search(self) -> None:
        self.go(0)
        self.guichet.focus_search()

    # ------------------------------------------------------------ taille de l'interface
    def change_scale(self, step: int) -> int:
        """+1 / -1 : taille suivante / précédente ; 0 : taille normale (100 %). Appliquée au redémarrage."""
        cur = self.s.ui_scale if self.s.ui_scale in UI_SCALES else 100
        k = UI_SCALES.index(cur)
        new = 100 if step == 0 else UI_SCALES[max(0, min(len(UI_SCALES) - 1, k + step))]
        if new == cur:
            limit = "maximale" if step > 0 else ("minimale" if step < 0 else "normale")
            self.toast.show_message(f"Taille de l'interface déjà {limit} ({cur} %).", "blue", 4000)
            return cur
        self.s.ui_scale = new
        save_settings(self.s)
        self._scale_notice()
        return new

    def _scale_notice(self) -> None:
        applied = round(float(os.environ.get("QT_SCALE_FACTOR", "1") or 1) * 100)
        if self.s.ui_scale == applied:
            self.toast.show_message(f"Taille de l'interface : {self.s.ui_scale} % (taille actuelle).", "green", 5000)
            return
        self.toast.show_message(f"Taille de l'interface : {self.s.ui_scale} % — s'applique au redémarrage de "
                                "Scolarix (Ctrl+0 : taille normale).", "blue", 20000,
                                action=("Redémarrer maintenant", self.restart))

    def restart(self) -> None:
        """Relance Scolarix (même exécutable, mêmes arguments) après une fermeture normale."""
        from PySide6.QtCore import QProcess
        program = sys.executable
        args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
        if self.close():
            QProcess.startDetached(program, list(args))

    def _new_demande(self) -> None:
        """Ctrl+N : au guichet (pré-remplie avec le relevé affiché) comme sur l'écran Demandes."""
        page = self.stack.currentWidget()
        if page is self.guichet:
            self.guichet.demande_bar.create()
        elif page is self.demandes:
            self.demandes.create()

    def _on_guichet(self, fn) -> None:
        if self.stack.currentIndex() == 0:
            fn()

    def _after_indexing(self) -> None:
        self.guichet.refresh_stats()
        self.guichet.run_search(self.guichet.omni.text())

    def open_settings(self) -> None:
        scale = self.s.ui_scale
        if SettingsDialog(self.s, self).exec():
            if self.s.ui_scale != scale:
                self._scale_notice()
            from . import effects
            effects.configure(self.s)
            self.guichet.refresh_header()
            self.guichet.preview.rules_active = self.s.text_rules_enabled and any(
                r.get("actif", True) for r in self.s.text_rules)
            self.indexing._update_excl_btn()
            self.avatar.setText(_initials(self.s.operateur))

    def closeEvent(self, event):
        if self.indexing.is_running():
            r = QMessageBox.question(self, "Indexation en cours",
                                     "Le robot d'indexation travaille encore. L'arrêter et quitter ?\n"
                                     "(Une indexation différentielle reprendra là où il s'est arrêté.)")
            if r != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            from PySide6.QtWidgets import QApplication
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self.indexing.stop_and_wait(30000)
            finally:
                QApplication.restoreOverrideCursor()
        self.previews.close()    # ferme Word (aperçus) avant l'arrêt de l'interpréteur
        super().closeEvent(event)


def _initials(name: str) -> str:
    parts = [p for p in (name or "?").replace(".", " ").replace("_", " ").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"
