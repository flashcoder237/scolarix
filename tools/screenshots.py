"""Captures d'écran automatiques (tests visuels, documentation).

    QT_QPA_PLATFORM=offscreen python tools/screenshots.py ./captures [requête]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.config import load_settings  # noqa: E402
from core.database import Database  # noqa: E402
from ui.app import _font, resource_path  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "captures")
    query = sys.argv[2] if len(sys.argv) > 2 else "tchoua"
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    app.setFont(_font())
    app.setStyleSheet(resource_path("ui", "theme_fmsp.qss").read_text(encoding="utf-8"))
    s = load_settings()
    win = MainWindow(Database(s.resolved_db_path()), s)
    win.resize(1600, 1000)
    win.show()

    steps = []

    def shot(name):
        def f():
            app.processEvents()
            win.grab().save(str(out / f"{name}.png"))
        return f

    steps.append(lambda: win.guichet.omni.edit.setText(query))
    steps.append(lambda: None)
    steps.append(shot("1_guichet"))
    steps.append(lambda: win.guichet.move_selection(5))
    steps.append(lambda: win.guichet.preview.dup_switch.setChecked(True))
    steps.append(shot("2_guichet_selection_duplicata"))
    steps.append(lambda: (win.guichet.preview.dup_switch.setChecked(False), win.guichet.view_seg.changed.emit("groupe")))
    steps.append(shot("3_vue_groupee"))
    steps.append(lambda: win.go(1))
    steps.append(shot("4_indexation"))
    steps.append(lambda: win.go(2))
    steps.append(shot("5_extraction"))
    steps.append(lambda: win.go(3))
    steps.append(shot("6_verification"))
    steps.append(app.quit)

    def run(i=0):
        if i < len(steps):
            steps[i]()
            QTimer.singleShot(400, lambda: run(i + 1))

    QTimer.singleShot(500, run)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
