"""Composants réutilisables du Design System FMSP (cartes, pastilles, interrupteurs, graphiques...)."""
from __future__ import annotations

from PySide6.QtCore import (QEasingCurve, QObject, QPointF, QPropertyAnimation, QRectF, QRunnable, QSize, Qt,
                            QTimer, Signal, Property)
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
                               QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from .icons import pixmap

# Tokens (cf. Design System v3.1)
BG = "#F1F5F9"
SURFACE = "#FFFFFF"
SOFT = "#F6F8FB"
INK = "#0F172A"
MUTED = "#475569"
BLUE = "#1D4ED8"
TONES = {
    "green": ("#15803D", "#ECFDF3"),
    "amber": ("#B45309", "#FFF7E6"),
    "red": ("#B91C1C", "#FEF1F1"),
    "blue": ("#1D4ED8", "#EEF4FF"),
    "violet": ("#6D28D9", "#F3F0FF"),
    "gray": ("#334155", "#F1F5F9"),
}
MONO = "Consolas, 'Cascadia Mono', 'JetBrains Mono', 'DejaVu Sans Mono', monospace"


def statut_tone(statut: str | None) -> str:
    s = (statut or "").upper()
    if s.startswith("ADMIS") or s.startswith("VALID"):
        return "green"
    if not s:
        return "gray"
    if "VÉRIFIER" in s or "VERIFIER" in s:
        return "amber"
    return "red"


def flag_tone(flag: str) -> str:
    if flag in ("SCAN OCR",):
        return "gray"
    if flag in ("FICHIER INTROUVABLE",):
        return "red"
    return "amber"


def mono_font(size: float = 10.5, bold: bool = True) -> QFont:
    f = QFont()
    f.setFamilies(["Consolas", "Cascadia Mono", "JetBrains Mono", "DejaVu Sans Mono", "monospace"])
    f.setPointSizeF(size)
    f.setBold(bold)
    return f


def shadow(widget: QWidget, blur: int = 30, dy: int = 10, alpha: int = 28) -> None:
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(QColor(15, 23, 42, alpha))
    widget.setGraphicsEffect(eff)


class Card(QFrame):
    def __init__(self, parent=None, padding: int = 18, spacing: int = 12, soft_shadow: bool = True):
        super().__init__(parent)
        self.setObjectName("card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(padding, padding, padding, padding)
        self.lay.setSpacing(spacing)
        if soft_shadow:
            shadow(self)


def eyebrow(text: str) -> QLabel:
    lab = QLabel(text.upper())
    lab.setObjectName("eyebrow")
    f = lab.font()
    f.setPointSizeF(8)
    f.setWeight(QFont.Weight.DemiBold)
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
    lab.setFont(f)
    return lab


def title(text: str, size: float = 13, obj: str = "h2") -> QLabel:
    lab = QLabel(text)
    lab.setObjectName(obj)
    f = lab.font()
    f.setPointSizeF(size)
    f.setWeight(QFont.Weight.DemiBold)
    lab.setFont(f)
    return lab


def muted(text: str = "", size: float = 9) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("muted")
    lab.setWordWrap(True)
    f = lab.font()
    f.setPointSizeF(size)
    lab.setFont(f)
    return lab


def card_header(eyebrow_text: str, title_text: str) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    lay.addWidget(eyebrow(eyebrow_text))
    t = title(title_text, 13)
    lay.addWidget(t)
    w.title_label = t  # type: ignore[attr-defined]
    return w


class Badge(QLabel):
    """Pastille de statut arrondie avec point coloré."""

    def __init__(self, text: str = "", tone: str = "gray", parent=None, big: bool = False):
        super().__init__(parent)
        self._big = big
        self.set(text, tone)

    def set(self, text: str, tone: str = "gray") -> None:
        fg, bg = TONES.get(tone, TONES["gray"])
        size = "9pt" if self._big else "8pt"
        pad = "5px 12px" if self._big else "3px 10px"
        self.setStyleSheet(f"QLabel{{color:{fg};background:{bg};border-radius:11px;padding:{pad};"
                           f"font-size:{size};font-weight:700;}}")
        self.setText(f"● {text}")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.ensurePolished()
        self.setFixedSize(self.sizeHint())


class IconChip(QLabel):
    def __init__(self, icon_name: str, tone: str = "blue", size: int = 42, filled: bool = False, parent=None):
        super().__init__(parent)
        fg, bg = TONES.get(tone, TONES["blue"])
        if filled:
            fg, bg = "#FFFFFF", fg
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setPixmap(pixmap(icon_name, fg, int(size * 0.45)))
        self.setStyleSheet(f"QLabel{{background:{bg};border-radius:{int(size * 0.32)}px;}}")


class Switch(QAbstractButton):
    """Interrupteur (toggle) accessible au clavier : espace pour basculer."""

    def __init__(self, checked: bool = False, parent=None, on_color: str = BLUE):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(48, 28)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._on = QColor(on_color)
        self._pos = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def get_knob(self) -> float:
        return self._pos

    def set_knob(self, v: float) -> None:
        self._pos = v
        self.update()

    knob = Property(float, get_knob, set_knob)

    def sizeHint(self) -> QSize:
        return QSize(48, 28)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        off = QColor("#7B8798")
        c = QColor(off.red() + (self._on.red() - off.red()) * self._pos,
                   off.green() + (self._on.green() - off.green()) * self._pos,
                   off.blue() + (self._on.blue() - off.blue()) * self._pos)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c if self.isEnabled() else QColor("#CBD5E1"))
        p.drawRoundedRect(QRectF(0, 0, 48, 28), 14, 14)
        if self.hasFocus():
            p.setPen(QPen(QColor(BLUE), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(1, 1, 46, 26), 13, 13)
            p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QRectF(4 + 20 * self._pos, 4, 20, 20))


class NavButton(QAbstractButton):
    """Bouton du rail : carré arrondi avec icône, libellé dessous ; actif = bleu plein avec lueur."""

    def __init__(self, icon_name: str, label: str, parent=None):
        super().__init__(parent)
        self.icon_name = icon_name
        self.label = label
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(76, 76)
        self.setToolTip(label)
        self.setAccessibleName(label)
        self._hover = False

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF((self.width() - 48) / 2, 2, 48, 48)
        active = self.isChecked()
        if active:
            for i, a in enumerate((18, 12, 6)):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(29, 78, 216, a))
                p.drawRoundedRect(box.adjusted(-2 - i * 2, 6 + i * 2, 2 + i * 2, 8 + i * 2), 18, 18)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(BLUE) if active else QColor("#E8EDF4" if self._hover else "#F3F5F8"))
        p.drawRoundedRect(box, 16, 16)
        pm = pixmap(self.icon_name, "#FFFFFF" if active else MUTED, 21)
        p.drawPixmap(int(box.center().x() - 10.5), int(box.center().y() - 10.5), pm)
        f = self.font()
        f.setPointSizeF(8)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(QColor(BLUE if active else MUTED))
        p.drawText(QRectF(0, 54, self.width(), 20), Qt.AlignmentFlag.AlignHCenter, self.label)
        if self.hasFocus():
            p.setPen(QPen(QColor(BLUE), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(box.adjusted(-3, -3, 3, 3), 18, 18)


class Segmented(QFrame):
    """Contrôle segmenté (pastilles dans un conteneur gris)."""
    changed = Signal(object)

    def __init__(self, options: list[tuple[object, str]], current=None, parent=None):
        super().__init__(parent)
        self.setObjectName("segbox")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[object, QPushButton] = {}
        for value, label in options:
            b = QPushButton(label)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, v=value: self.changed.emit(v))
            self.group.addButton(b)
            lay.addWidget(b)
            self.buttons[value] = b
        self.set_value(current if current is not None else options[0][0])
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_value(self, value) -> None:
        if value in self.buttons:
            self.buttons[value].setChecked(True)

    def value(self):
        for v, b in self.buttons.items():
            if b.isChecked():
                return v
        return None


class Alert(QFrame):
    def __init__(self, text: str, tone: str = "amber", parent=None):
        super().__init__(parent)
        fg, bg = TONES.get(tone, TONES["amber"])
        if tone == "amber":
            fg = "#92400E"
        self.setObjectName("alert")
        self.setStyleSheet(f"QFrame#alert{{background:{bg};border-radius:14px;}} QLabel{{color:{fg};background:transparent;}}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 12, 8)
        lay.setSpacing(10)
        ico = QLabel()
        ico.setFixedSize(28, 28)
        ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ico.setPixmap(pixmap("check" if tone == "green" else "info", fg, 15))
        ico.setStyleSheet("QLabel{background:#FFFFFF;border-radius:14px;}")
        lay.addWidget(ico, 0, Qt.AlignmentFlag.AlignTop)
        lab = QLabel(text)
        lab.setWordWrap(True)
        f = lab.font()
        f.setWeight(QFont.Weight.DemiBold)
        f.setPointSizeF(9.5)
        lab.setFont(f)
        lay.addWidget(lab, 1)


class Toast(QFrame):
    """Message de confirmation qui disparaît seul."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(10, 8, 12, 8)
        self.ico = QLabel()
        self.ico.setFixedSize(28, 28)
        self.ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lay.addWidget(self.ico)
        self.lab = QLabel()
        self.lab.setWordWrap(True)
        self.lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lay.addWidget(self.lab, 1)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, text: str, tone: str = "green", ms: int = 8000) -> None:
        fg, bg = TONES.get(tone, TONES["green"])
        self.setObjectName("toast")
        self.setStyleSheet(f"QFrame#toast{{background:{bg};border-radius:14px;}} QLabel{{color:{fg};font-weight:600;background:transparent;}}")
        self.ico.setPixmap(pixmap("check" if tone == "green" else "alert", fg, 15))
        self.ico.setStyleSheet("QLabel{background:#FFFFFF;border-radius:14px;}")
        self.lab.setText(text)
        self.show()
        self._timer.start(ms)


class StatCard(QFrame):
    """Carte d'état compacte : puce d'icône + libellé + valeur (+ barre optionnelle)."""

    def __init__(self, icon_name: str, tone: str, label: str, value: str = "—", progress: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        shadow(self, 24, 8, 22)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 16, 12)
        lay.setSpacing(12)
        lay.addWidget(IconChip(icon_name, tone, 42))
        col = QVBoxLayout()
        col.setSpacing(3)
        self.label = muted(label, 8.5)
        self.value = QLabel(value)
        f = self.value.font()
        f.setPointSizeF(11.5)
        f.setWeight(QFont.Weight.Bold)
        self.value.setFont(f)
        col.addWidget(self.label)
        col.addWidget(self.value)
        self.bar = None
        if progress:
            self.bar = ThinBar()
            col.addWidget(self.bar)
        lay.addLayout(col, 1)

    def set(self, value: str, ratio: float | None = None) -> None:
        self.value.setText(value)
        if self.bar is not None:
            self.bar.setVisible(ratio is not None)
            if ratio is not None:
                self.bar.set_ratio(ratio)


class ThinBar(QWidget):
    def __init__(self, parent=None, color: str = BLUE):
        super().__init__(parent)
        self.setFixedHeight(5)
        self._r = 0.0
        self._c = QColor(color)

    def set_ratio(self, r: float) -> None:
        self._r = max(0.0, min(1.0, r))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#E8EDF4"))
        p.drawRoundedRect(QRectF(0, 0, self.width(), 5), 2.5, 2.5)
        p.setBrush(self._c)
        p.drawRoundedRect(QRectF(0, 0, self.width() * self._r, 5), 2.5, 2.5)


def _hatch(p: QPainter, rect: QRectF, radius: float, c1="#EAEFF5", c2="#F6F8FB") -> None:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    p.save()
    p.setClipPath(path)
    p.fillRect(rect, QColor(c2))
    pen = QPen(QColor(c1), 4)
    p.setPen(pen)
    x = rect.left() - rect.height()
    while x < rect.right():
        p.drawLine(QPointF(x, rect.bottom()), QPointF(x + rect.height(), rect.top()))
        x += 9
    p.restore()


class PillBars(QWidget):
    """Barres verticales en gélule, piste hachurée (cf. maquette)."""

    def __init__(self, parent=None, height: int = 190):
        super().__init__(parent)
        self.setMinimumHeight(height)
        self.data: list[tuple[str, float, str]] = []   # (libellé, valeur, couleur)
        self.value_fmt = lambda v: f"{v:,.0f}".replace(",", " ")

    def set_data(self, data) -> None:
        self.data = list(data)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.data:
            p.setPen(QColor(MUTED))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Aucune donnée indexée")
            return
        n = len(self.data)
        top, bottom = 22, 22
        h = self.height() - top - bottom
        slot = self.width() / n
        bw = min(30.0, slot * 0.62)
        vmax = max(v for _, v, _ in self.data) or 1
        f = self.font()
        f.setPointSizeF(7.5)
        p.setFont(f)
        for i, (label, v, color) in enumerate(self.data):
            x = i * slot + (slot - bw) / 2
            track = QRectF(x, top, bw, h)
            _hatch(p, track, bw / 2)
            fh = max(bw, h * v / vmax) if v else 0
            if fh:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(color))
                p.drawRoundedRect(QRectF(x, top + h - fh, bw, fh), bw / 2, bw / 2)
            p.setPen(QColor("#334155"))
            p.drawText(QRectF(i * slot, 2, slot, 18), Qt.AlignmentFlag.AlignCenter, self.value_fmt(v))
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(i * slot, self.height() - 18, slot, 18), Qt.AlignmentFlag.AlignCenter, label)


class HBars(QWidget):
    """Barres horizontales en gélule avec libellé et valeur."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.data: list[tuple[str, float, str]] = []

    def set_data(self, data) -> None:
        self.data = list(data)
        self.setMinimumHeight(max(40, 44 * len(self.data)))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.data:
            p.setPen(QColor(MUTED))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Aucune donnée")
            return
        vmax = max(v for _, v, _ in self.data) or 1
        for i, (label, v, color) in enumerate(self.data):
            y = i * 44
            p.setPen(QColor(INK))
            p.drawText(QRectF(0, y, self.width() - 60, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            p.drawText(QRectF(self.width() - 60, y, 60, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{v:,.0f}".replace(",", " "))
            track = QRectF(0, y + 22, self.width(), 13)
            _hatch(p, track, 6.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(color))
            p.drawRoundedRect(QRectF(0, y + 22, max(13.0, self.width() * v / vmax), 13), 6.5, 6.5)


class Ring(QWidget):
    """Anneau de conformité (n conformes / total)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(96, 96)
        self.ok, self.total = 0, 0

    def set(self, ok: int, total: int) -> None:
        self.ok, self.total = ok, total
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(8, 8, 80, 80)
        p.setPen(QPen(QColor("#FECACA"), 10, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawArc(r, 0, 360 * 16)
        if self.total:
            p.setPen(QPen(QColor("#15803D"), 10, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawArc(r, 90 * 16, -int(360 * 16 * self.ok / self.total))
        p.setPen(QColor(INK))
        f = self.font()
        f.setPointSizeF(13)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(0, 26, 96, 26), Qt.AlignmentFlag.AlignCenter, f"{self.ok}/{self.total}" if self.total else "—")
        f.setPointSizeF(7)
        f.setBold(False)
        p.setFont(f)
        p.setPen(QColor(MUTED))
        p.drawText(QRectF(0, 52, 96, 16), Qt.AlignmentFlag.AlignCenter, "conformes")


class OptionTile(QFrame):
    """Tuile d'option avec puce d'icône et interrupteur (cf. « Favorite devices »)."""
    toggled = Signal(bool)

    def __init__(self, icon_name: str, tone: str, label: str, hint: str, checked: bool, parent=None):
        super().__init__(parent)
        self._tone = tone
        self._icon = icon_name
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(8)
        top = QHBoxLayout()
        self.chip = QLabel()
        self.chip.setFixedSize(38, 38)
        self.chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.chip)
        top.addStretch(1)
        self.switch = Switch(checked, on_color=INK)
        self.switch.setAccessibleName(label)
        self.switch.toggled.connect(self._refresh)
        self.switch.toggled.connect(self.toggled)
        top.addWidget(self.switch)
        lay.addLayout(top)
        t = QLabel(label)
        f = t.font()
        f.setWeight(QFont.Weight.DemiBold)
        t.setFont(f)
        t.setWordWrap(True)
        lay.addWidget(t)
        lay.addWidget(muted(hint, 8.5))
        self._refresh(checked)

    def _refresh(self, on: bool) -> None:
        fg, bg = TONES[self._tone]
        if on:
            self.setStyleSheet("OptionTile{background:#FFFFFF;border:1px solid #E2E8F0;border-radius:18px;}")
            self.chip.setStyleSheet(f"QLabel{{background:{fg};border-radius:12px;}}")
            self.chip.setPixmap(pixmap(self._icon, "#FFFFFF", 18))
        else:
            self.setStyleSheet(f"OptionTile{{background:{SOFT};border:1px solid {SOFT};border-radius:18px;}}")
            self.chip.setStyleSheet(f"QLabel{{background:{bg};border-radius:12px;}}")
            self.chip.setPixmap(pixmap(self._icon, fg, 18))

    def isChecked(self) -> bool:
        return self.switch.isChecked()


# --------------------------------------------------------------- tâches de fond
class _TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Task(QRunnable):
    """Exécute une fonction dans le QThreadPool : l'interface ne gèle jamais (loi des 100 ms)."""

    _alive: set = set()   # garde une référence tant que le résultat n'est pas livré à l'interface

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.setAutoDelete(False)
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = _TaskSignals()
        Task._alive.add(self)
        self.signals.done.connect(lambda *_: Task._alive.discard(self))
        self.signals.failed.connect(lambda *_: Task._alive.discard(self))

    def run(self):
        try:
            res = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # remonté à l'interface sous forme de message
            self.signals.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.signals.done.emit(res)


def hline() -> QFrame:
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet("background:#EEF1F5;")
    return f


def brush(color: str) -> QBrush:
    return QBrush(QColor(color))
