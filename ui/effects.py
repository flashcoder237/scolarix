"""Finitions de l'interface : polices modernes embarquées, curseurs, sons discrets, petites animations.

* Polices : Inter (texte), Inter Display (titres), JetBrains Mono (matricules, références) — licence
  SIL OFL, embarquées dans l'application : même rendu sur tous les postes, sans rien installer.
* Curseurs : main sur tout ce qui se clique (boutons, onglets, listes déroulantes, lignes de résultats).
* Sons : très courts et discrets (succès, attention, erreur, ajout, impression). Désactivables.
* Animations : apparition des messages, fondu entre les écrans, compteurs qui défilent.
  Désactivables (Réglages › Poste) pour les postes lents ou par confort.
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPropertyAnimation, Qt, QTimer, QUrl, QVariantAnimation
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QAbstractButton, QAbstractItemView, QComboBox, QGraphicsOpacityEffect, QTabBar, QWidget

TEXT_FAMILY = "Inter"
TITLE_FAMILY = "Inter Display"
MONO_FAMILY = "JetBrains Mono"

_state = {"sounds": True, "animations": True, "volume": 0.35, "fonts": set()}
_effects: dict[str, object] = {}


def configure(settings) -> None:
    _state["sounds"] = bool(getattr(settings, "ui_sounds", True)) and os.environ.get("SCOLARIX_SOUND", "1") != "0"
    _state["animations"] = bool(getattr(settings, "ui_animations", True))


def animations_on() -> bool:
    return _state["animations"]


# ------------------------------------------------------------------ polices
def load_fonts(assets_dir: Path) -> set[str]:
    families: set[str] = set()
    for f in sorted((assets_dir / "fonts").glob("*.[ot]tf")):
        fid = QFontDatabase.addApplicationFont(str(f))
        if fid >= 0:
            families.update(QFontDatabase.applicationFontFamilies(fid))
    _state["fonts"] = families
    return families


def has_font(family: str) -> bool:
    return family in _state["fonts"] or family in set(QFontDatabase.families())


# ------------------------------------------------------------------ curseurs
class _CursorFilter(QObject):
    """Main pointée sur tout élément cliquable, posée une fois quand le widget est affiché."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and not obj.property("_cursor_set"):
            obj.setProperty("_cursor_set", True)
            if isinstance(obj, (QAbstractButton, QComboBox, QTabBar)):
                obj.setCursor(Qt.CursorShape.PointingHandCursor)
            elif isinstance(obj, QAbstractItemView) and obj.objectName() in ("results", "demandes"):
                obj.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        return False


def install_cursors(app) -> None:
    f = _CursorFilter(app)
    app.installEventFilter(f)
    app._cursor_filter = f        # garder une référence


# ------------------------------------------------------------------ sons
SOUNDS = {"green": "succes", "amber": "attention", "red": "erreur", "ajout": "ajout", "impression": "impression"}


def init_sounds(assets_dir: Path) -> None:
    try:
        from PySide6.QtMultimedia import QSoundEffect
    except Exception:
        _state["sounds"] = False
        return
    for name in set(SOUNDS.values()):
        path = assets_dir / "sounds" / f"{name}.wav"
        if path.exists():
            eff = QSoundEffect()
            eff.setSource(QUrl.fromLocalFile(str(path)))
            eff.setVolume(_state["volume"])
            _effects[name] = eff


def play(kind: str) -> None:
    """kind : green / amber / red (ton d'un message) ou ajout / impression."""
    if not _state["sounds"]:
        return
    eff = _effects.get(SOUNDS.get(kind, kind))
    if eff is not None:
        try:
            eff.play()
        except Exception:
            pass


# ------------------------------------------------------------------ animations
def fade_in(widget: QWidget, ms: int = 180) -> None:
    if not animations_on() or widget is None:
        return
    eff = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    # l'effet est retiré à la fin : pas de coût de rendu permanent (ni de conflit avec les ombres)
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)


def slide_in(widget: QWidget, ms: int = 220) -> None:
    """Apparition d'un message : la hauteur s'ouvre en douceur, puis fondu."""
    if not animations_on() or widget is None:
        return
    target = widget.sizeHint().height()
    anim = QPropertyAnimation(widget, b"maximumHeight", widget)
    anim.setDuration(ms)
    anim.setStartValue(0)
    anim.setEndValue(max(target, 1))
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.finished.connect(lambda: widget.setMaximumHeight(16777215))
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    fade_in(widget, ms)


def count_to(label, value: int, fmt=str, ms: int = 450) -> None:
    """Compteur qui défile jusqu'à sa nouvelle valeur (cartes d'état)."""
    try:
        start = int("".join(ch for ch in label.text() if ch.isdigit()) or 0)
    except ValueError:
        start = 0
    if not animations_on() or start == value:
        label.setText(fmt(value))
        return
    anim = QVariantAnimation(label)
    anim.setDuration(ms)
    anim.setStartValue(start)
    anim.setEndValue(value)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.valueChanged.connect(lambda v: label.setText(fmt(int(v))))
    anim.start(QVariantAnimation.DeletionPolicy.DeleteWhenStopped)


def pulse(widget: QWidget, times: int = 2) -> None:
    """Petit clignement doux pour attirer l'œil (ex. barre « Demande en cours » après un ajout)."""
    if not animations_on() or widget is None:
        return
    eff = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(260 * times)
    anim.setKeyValueAt(0.0, 1.0)
    for k in range(times):
        anim.setKeyValueAt((2 * k + 1) / (2 * times), 0.55)
        anim.setKeyValueAt((2 * k + 2) / (2 * times), 1.0)
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)


class Dots:
    """« Préparation de l'aperçu… » animé (points qui défilent) tant qu'une tâche tourne."""

    def __init__(self, setter, base: str, parent=None):
        self.setter, self.base, self.k = setter, base, 0
        self.timer = QTimer(parent)
        self.timer.timeout.connect(self._tick)

    def start(self):
        self.k = 0
        self._tick()
        if animations_on():
            self.timer.start(400)

    def _tick(self):
        self.setter(self.base + "." * (1 + self.k % 3))
        self.k += 1

    def stop(self):
        self.timer.stop()
