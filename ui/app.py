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
    for fam in ("Inter", "Segoe UI Variable Text", "Segoe UI", "Noto Sans", "DejaVu Sans"):
        if fam in families:
            f = QFont(fam)
            break
    else:
        f = QFont()
    f.setPointSizeF(10)
    f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    return f


def apply_ui_scale(settings) -> None:
    """Taille de l'interface choisie par l'agent, en plus de la mise à l'échelle de Windows.
    Qt ne la lit qu'au démarrage : un changement s'applique en relançant Scolarix."""
    import os
    scale = getattr(settings, "ui_scale", 100) or 100
    if scale != 100 and "QT_SCALE_FACTOR" not in os.environ:
        os.environ["QT_SCALE_FACTOR"] = f"{max(50, min(200, scale)) / 100:.2f}"


def run_gui(argv: list[str]) -> int:
    apply_ui_scale(load_settings())
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("FMSP - Université de Douala")
    from . import effects
    effects.load_fonts(resource_path("assets"))    # Inter, Inter Display, JetBrains Mono embarquées
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
    effects.configure(settings)
    effects.init_sounds(resource_path("assets"))
    effects.install_cursors(app)
    db = Database(settings.resolved_db_path(), cache_dir=settings.resolved_cache_dir())
    previews = PreviewService(db, settings)
    app.aboutToQuit.connect(previews.close)
    try:   # instances de Word laissées par une session précédente interrompue (plantage, coupure)
        from core.extractors import kill_orphan_words
        kill_orphan_words()
    except Exception:
        pass
    import threading

    from core.exports import purge_old
    threading.Thread(target=purge_old, args=(settings,), daemon=True).start()   # durée de conservation
    win = MainWindow(db, settings, previews)
    win.show()
    return app.exec()
