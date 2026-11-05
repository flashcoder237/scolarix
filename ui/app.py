"""Initialisation de QApplication, police et thème."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from core.config import APP_NAME, load_settings
from core.database import Database

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
    qss = resource_path("ui", "theme_fmsp.qss")
    if qss.exists():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))
    settings = load_settings()
    db = Database(settings.resolved_db_path())
    win = MainWindow(db, settings)
    win.show()
    return app.exec()
