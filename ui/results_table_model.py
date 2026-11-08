"""Modèle de tableau haute performance + délégué de rendu (surlignage de la frappe, pastilles)."""
from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from core.database import Releve
from core.normalize import norm_matricule, norm_text

from .widgets import BLUE, INK, MUTED, TONES, flag_tone, mono_font, statut_tone

COLUMNS = ["Matricule", "Nom & prénom", "Période", "Année", "Statut", "Pages"]
FIL_SHORT = {"M": "Médecine", "P": "Pharmacie", "B": "Biomédical", "MS": "Master", "D": "Doctorat"}
ReleveRole = Qt.ItemDataRole.UserRole + 1


class ResultsModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[Releve] = []
        self.query_name = ""
        self.query_mat = ""

    def set_results(self, rows: list[Releve], query: str) -> None:
        self.beginResetModel()
        self.rows = rows
        self.query_name = norm_text(query)
        self.query_mat = norm_matricule(query)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section].upper()
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r = self.rows[index.row()]
        c = index.column()
        if role == ReleveRole:
            return r
        if role == Qt.ItemDataRole.DisplayRole:
            return [r.matricule, r.nom_complet, f"{FIL_SHORT.get(r.filiere_code or '', r.filiere or '?')} · {_periode_court(r)}",
                    r.annee_academique or "?", r.statut or "—", _pages(r)][c]
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{r.matricule} {r.nom_complet} {r.periode} {r.annee_academique} {r.statut}"
        if role == Qt.ItemDataRole.ToolTipRole and c == 1:
            return r.nom_complet + (("\n" + " · ".join(r.flags)) if r.flags else "")
        return None

    def releve_at(self, row: int) -> Releve | None:
        return self.rows[row] if 0 <= row < len(self.rows) else None


def _periode_court(r: Releve) -> str:
    """« N2 · S3 » ou « Master 1 » (le libellé complet est dans l'aperçu)."""
    if 0 < r.niveau_num < 100:
        base = f"N{r.niveau_num}"
    else:
        base = r.niveau or "?"
    return f"{base} · {r.sem_label}" if r.semestre else base


def _ligne2(r: Releve) -> str:
    if (r.session or "").lower().startswith("rattrap"):
        return "Session de rattrapage"
    if r.moyenne:
        return f"Moy. {r.moyenne}" + (f" · {r.grade}" if r.grade else "")
    if r.session:
        return f"Session {r.session.lower()}"
    return ""   # rien d'inventé : l'ancien format n'indique ni moyenne ni session


def _pages(r: Releve) -> str:
    return f"{r.numero_page}–{r.page_fin}" if r.page_fin > r.numero_page else str(r.numero_page)


def _highlight_span(text: str, needle: str) -> tuple[int, int]:
    """Position de la saisie dans le texte affiché (insensible aux accents et à la casse)."""
    if not needle:
        return -1, 0
    nt = norm_text(text)
    if len(nt) != len(text):
        # apostrophes ou ponctuation retirées : on cherche directement en majuscules
        idx = text.upper().find(needle)
        return idx, len(needle)
    return nt.find(needle), len(needle)


class RowDelegate(QStyledItemDelegate):
    ROW_H = 52

    def sizeHint(self, option, index):
        return QSize(super().sizeHint(option, index).width(), self.ROW_H)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index):
        model: ResultsModel = index.model()
        r: Releve = index.data(ReleveRole)
        rect = QRectF(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        p.save()
        p.setClipRect(option.rect)   # aucun texte ne déborde sur la colonne voisine
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # fond de ligne
        if selected:
            p.fillRect(rect, QColor("#EEF4FF"))
            pen = QPen(QColor(BLUE), 1.5)
            p.setPen(pen)
            p.drawLine(rect.topLeft(), rect.topRight())
            p.drawLine(rect.bottomLeft(), rect.bottomRight())
            if index.column() == 0:
                p.drawLine(rect.topLeft(), rect.bottomLeft())
            if index.column() == model.columnCount() - 1:
                p.drawLine(rect.topRight(), rect.bottomRight())
        elif hover:
            p.fillRect(rect, QColor("#F5F7FA"))
        elif index.row() % 2:
            p.fillRect(rect, QColor("#FAFBFC"))

        inner = rect.adjusted(10, 0, -8, 0)
        col = index.column()
        base = option.font
        if col == 0:
            f = mono_font(10.5)
            mat = r.matricule
            idx, n = (mat.find(model.query_mat), len(model.query_mat)) if len(model.query_mat) >= 2 else (-1, 0)
            self._draw_highlighted(p, inner, mat, idx, n, f, QColor(INK))
        elif col == 1:
            f = QFont(base)
            f.setWeight(QFont.Weight.DemiBold)
            nom = r.nom or r.nom_complet
            prenom = r.prenom or ""
            # Nom sur la première ligne, drapeaux (rattrapage, 2 pages, score flou...) sur la seconde
            line = QRectF(inner.left(), inner.top() + 4, inner.width(), inner.height() / 2) if r.flags else inner
            idx, n = _highlight_span(nom, model.query_name.split(" ")[0] if model.query_name else "")
            x = self._draw_highlighted(p, line, nom.upper(), idx, n, f, QColor(INK))
            if prenom and x + 5 < inner.right():
                p.setFont(QFont(base))
                p.setPen(QColor(INK))
                width = max(0.0, inner.right() - x - 5)
                p.drawText(QRectF(x + 5, line.top(), width, line.height()), Qt.AlignmentFlag.AlignVCenter,
                           QFontMetricsF(QFont(base)).elidedText(prenom, Qt.TextElideMode.ElideRight, width))
            if r.flags:
                fx = inner.left()
                cy = inner.top() + inner.height() * 0.74
                for flag in r.flags:
                    w = self._pill(p, fx, cy, flag, flag_tone(flag), small=True, limit=inner.right())
                    if w <= 0:
                        break
                    fx += w + 5
        elif col == 2:     # deux lignes : la période (« N4 · S7-S8 ») d'abord, la filière dessous
            p.setPen(QColor("#334155"))
            p.setFont(base)
            p.drawText(QRectF(inner.left(), inner.top() + 7, inner.width(), inner.height() / 2 - 4),
                       Qt.AlignmentFlag.AlignBottom,
                       QFontMetricsF(base).elidedText(_periode_court(r), Qt.TextElideMode.ElideRight, inner.width()))
            f = QFont(base)
            f.setPointSizeF(base.pointSizeF() * 0.88)
            p.setFont(f)
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(inner.left(), inner.center().y() + 1, inner.width(), inner.height() / 2),
                       Qt.AlignmentFlag.AlignTop, FIL_SHORT.get(r.filiere_code or "", r.filiere or "?"))
        elif col == 3:
            p.setFont(base)
            p.setPen(QColor("#334155"))
            p.drawText(QRectF(inner.left(), inner.top() + 7, inner.width(), inner.height() / 2 - 4),
                       Qt.AlignmentFlag.AlignBottom, r.annee_academique or "?")
            f = QFont(base)
            f.setPointSizeF(base.pointSizeF() * 0.88)
            p.setFont(f)
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(inner.left(), inner.center().y() + 1, inner.width(), inner.height() / 2),
                       Qt.AlignmentFlag.AlignTop, _ligne2(r))
        elif col == 4:
            self._pill(p, inner.left(), inner.center().y(), r.statut or "—", statut_tone(r.statut))
        elif col == 5:
            p.setFont(mono_font(9.5, False))
            p.setPen(QColor("#334155"))
            p.drawText(inner, Qt.AlignmentFlag.AlignVCenter, _pages(r))
        p.restore()

    def _draw_highlighted(self, p, rect: QRectF, text: str, idx: int, n: int, font: QFont, color: QColor) -> float:
        fm = QFontMetricsF(font)
        p.setFont(font)
        x = rect.left()
        if fm.horizontalAdvance(text) > rect.width():   # nom trop long : « … » plutôt qu'un débordement
            text = fm.elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
            if idx + n > len(text) - 1:
                idx, n = -1, 0
        parts = [(text, False)] if idx < 0 or n <= 0 else [(text[:idx], False), (text[idx:idx + n], True), (text[idx + n:], False)]
        for chunk, hl in parts:
            if not chunk:
                continue
            w = fm.horizontalAdvance(chunk)
            if hl:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#FEF08A"))
                p.drawRoundedRect(QRectF(x - 1, rect.center().y() - fm.height() / 2, w + 2, fm.height()), 3, 3)
            p.setPen(color)
            p.drawText(QRectF(x, rect.top(), w + 4, rect.height()), Qt.AlignmentFlag.AlignVCenter, chunk)
            x += w
        return x

    def _pill(self, p: QPainter, x: float, cy: float, text: str, tone: str, small: bool = False, limit: float = 1e9) -> float:
        fg, bg = TONES.get(tone, TONES["gray"])
        f = QFont()
        f.setPointSizeF(7.2 if small else 8)
        f.setWeight(QFont.Weight.Bold)
        fm = QFontMetricsF(f)
        w = fm.horizontalAdvance(text) + 24
        if x + w > limit:
            return 0
        h = 18 if small else 22
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(bg))
        p.drawRoundedRect(QRectF(x, cy - h / 2, w, h), h / 2, h / 2)
        p.setBrush(QColor(fg))
        p.drawEllipse(QRectF(x + 8, cy - 3, 6, 6))
        p.setPen(QColor(fg))
        p.setFont(f)
        p.drawText(QRectF(x + 17, cy - h / 2, w - 17, h), Qt.AlignmentFlag.AlignVCenter, text)
        return w
