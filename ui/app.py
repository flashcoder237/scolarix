"""Initialisation de QApplication, police et thème."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from core.config import APP_NAME, load_settings
from core.database import Database
from core.pdf_tools import purge_temp
from core.preview import PreviewService

from .main_window import MainWindow


def resource_path(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base.joinpath(*parts)


def _font() -> QFont:
    families = set(QFontDatabase.families())
    for fam in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Noto Sans", "DejaVu Sans"):
        if fam in families:
            f = QFont(fam)
            break
    else:
        f = QFont()
    f.setPointSizeF(10)
    f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    return f


def run_gui(argv: list[str]) -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("FMSP - Université de Douala")
    app.setFont(_font())
    ico = resource_path("assets", "scolarix.ico")
    if not ico.exists():
        ico = resource_path("assets", "scolarix_256.png")
    if ico.exists():
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(ico)))
    purge_temp(max_age=3600)          # restes d'une session précédente (plus d'une heure)
    app.aboutToQuit.connect(purge_temp)   # fichiers de cette session
    qss = resource_path("ui", "theme_fmsp.qss")
    if qss.exists():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))
    settings = load_settings()
    db = Database(settings.resolved_db_path(), cache_dir=settings.resolved_cache_dir())
    previews = PreviewService(db, settings)
    app.aboutToQuit.connect(previews.close)
    try:   # instances de Word laissées par une session précédente interrompue (plantage, coupure)
        from core.extractors import kill_orphan_words
        kill_orphan_words()
    except Exception:
        pass
    win = MainWindow(db, settings, previews)
    win.show()
    return app.exec()
