"""Zone C : récapitulatif de sécurité, bandeaux d'alerte, aperçu PDF haute définition et actions."""
from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QMargins, QPointF, QThreadPool, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

from core import pdf_tools
from core.database import Releve

from .icons import icon
from .widgets import (Alert, Badge, Card, IconChip, Segmented, Switch, Task, Toast, eyebrow, mono_font, muted,
                      statut_tone, title)


class PdfViewer(QFrame):
    """QPdfView avec cache des documents ouverts (l'aperçu suit les flèches ↑ ↓ sans latence)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("PdfViewer{background:#EEF1F5;border-radius:16px;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        self.stack = QStackedWidget()
        self.view = QPdfView()
        self.view.setPageMode(QPdfView.PageMode.SinglePage)
        self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.view.setPageSpacing(12)
        self.view.setDocumentMargins(QMargins(10, 10, 10, 10))
        pal = self.view.palette()
        pal.setColor(QPalette.ColorRole.Dark, QColor("#EEF1F5"))
        pal.setColor(QPalette.ColorRole.Window, QColor("#EEF1F5"))
        pal.setColor(QPalette.ColorRole.Base, QColor("#EEF1F5"))
        self.view.setPalette(pal)
        self.view.viewport().setPalette(pal)
        self.view.viewport().setAutoFillBackground(True)
        self.placeholder = QLabel()
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setStyleSheet("color:#475569;font-size:10pt;padding:30px;")
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.placeholder)
        lay.addWidget(self.stack)
        self._docs: OrderedDict[str, QPdfDocument] = OrderedDict()
        self.setMinimumHeight(420)

    def _doc(self, path: str) -> QPdfDocument | None:
        if path in self._docs:
            self._docs.move_to_end(path)
            return self._docs[path]
        doc = QPdfDocument(self)
        if doc.load(path) != QPdfDocument.Error.None_:
            return None
        self._docs[path] = doc
        while len(self._docs) > 4:
            _, old = self._docs.popitem(last=False)
            old.close()
            old.deleteLater()
        return doc

    def show_page(self, path: str | None, page_index: int) -> bool:
        if not path or not Path(path).exists():
            self.show_message("Aperçu indisponible : fichier source ou cache d'aperçu introuvable.")
            return False
        doc = self._doc(path)
        if doc is None:
            self.show_message("Le PDF n'a pas pu être ouvert (protégé ou endommagé).")
            return False
        if self.view.document() is not doc:
            self.view.setDocument(doc)
        self.stack.setCurrentWidget(self.view)
        self.view.pageNavigator().jump(max(0, min(page_index, doc.pageCount() - 1)), QPointF(), 0)
        return True

    def forget(self, path: str) -> None:
        doc = self._docs.pop(path, None)
        if doc is not None:
            if self.view.document() is doc:
                self.view.setDocument(None)
            doc.close()

    def show_message(self, text: str) -> None:
        self.placeholder.setText(text)
        self.stack.setCurrentWidget(self.placeholder)

    def zoom(self, factor: float | None) -> None:
        if factor is None:
            self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            return
        current = self.view.zoomFactor()
        self.view.setZoomMode(QPdfView.ZoomMode.Custom)
        self.view.setZoomFactor(max(0.3, min(4.0, current * factor)))


class PreviewPanel(QWidget):
    print_requested = Signal()
    export_requested = Signal()
    releve_updated = Signal(object)
    cursus_requested = Signal()
    open_location_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rel: Releve | None = None
        self._page = 0
        self._stamped: str | None = None
        self.previews = None          # core.preview.PreviewService (aperçu Word produit à la demande)
        self.rules_active = False     # corrections de texte des relevés Word
        self._rendering: int | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        # --- récapitulatif de sécurité
        self.recap = Card(padding=16, spacing=12)
        head = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(eyebrow("Contrôle avant impression"))
        self.name = title("Aucun relevé sélectionné", 16, "h1")
        col.addWidget(self.name)
        self.details = muted("", 9)
        col.addWidget(self.details)
        head.addLayout(col, 1)
        self.statut = Badge("", "gray", big=True)
        head.addWidget(self.statut, 0, Qt.AlignmentFlag.AlignTop)
        self.recap.lay.addLayout(head)
        grid = QGridLayout()
        grid.setSpacing(8)
        self._grid = grid
        self._tile_frames: list[QFrame] = []
        self._grid_cols = 4
        self.tiles = {}
        for i, (key, label) in enumerate((("mat", "Matricule"), ("niv", "Année · niveau"), ("ses", "Semestre"),
                                          ("pages", "À imprimer"))):
            tile = QFrame()
            tile.setObjectName("soft")
            tl = QVBoxLayout(tile)
            tl.setContentsMargins(12, 9, 12, 9)
            tl.setSpacing(3)
            tl.addWidget(eyebrow(label))
            val = QLabel("—")
            if key == "mat":
                val.setFont(mono_font(14))
            else:
                f = val.font()
                f.setWeight(QFont.Weight.Bold)
                f.setPointSizeF(10.5)
                val.setFont(f)
            tl.addWidget(val)
            self.tiles[key] = val
            self._tile_frames.append(tile)
            grid.addWidget(tile, 0, i)
        self.recap.lay.addLayout(grid)
        self.alerts = QVBoxLayout()
        self.alerts.setSpacing(6)
        self.recap.lay.addLayout(self.alerts)
        lay.addWidget(self.recap)

        # --- visionneuse
        viewer_card = Card(padding=12, spacing=10)
        bar = QHBoxLayout()
        self.page_seg_holder = QHBoxLayout()
        bar.addLayout(self.page_seg_holder)
        bar.addStretch(1)
        fit = QPushButton(icon("fit", "#334155", 16), " Ajuster à la largeur")
        fit.setObjectName("ghost")
        fit.clicked.connect(lambda: self.viewer.zoom(None))
        bar.addWidget(fit)
        for name, factor, tip in (("zoom_out", 1 / 1.2, "Zoom arrière"), ("zoom_in", 1.2, "Zoom avant")):
            b = QPushButton(icon(name, "#334155", 17), "")
            b.setObjectName("ghost")
            b.setToolTip(tip)
            b.setAccessibleName(tip)
            b.clicked.connect(lambda _=False, f=factor: self.viewer.zoom(f))
            bar.addWidget(b)
        viewer_card.lay.addLayout(bar)
        self.viewer = PdfViewer()
        self.viewer.show_message("Tapez un matricule ou un nom dans la barre de recherche.")
        viewer_card.lay.addWidget(self.viewer, 1)
        lay.addWidget(viewer_card, 1)

        # --- actions (hiérarchie : 1 primaire, 2 secondaires, 1 tertiaire)
        actions = Card(padding=14, spacing=10)
        self.btn_print = QPushButton(icon("printer", "#FFFFFF", 19), "  Imprimer ce relevé   Ctrl+P")
        self.btn_print.setObjectName("primary")
        self.btn_print.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_print.clicked.connect(self.print_requested)
        actions.lay.addWidget(self.btn_print)
        row = QHBoxLayout()
        self.btn_export = QPushButton(icon("download", "#1D4ED8", 16), "  Exporter PDF   Ctrl+E")
        self.btn_export.setObjectName("secondary")
        self.btn_export.clicked.connect(self.export_requested)
        self.btn_cursus = QPushButton(icon("layers", "#1D4ED8", 16), "  Cursus complet   Ctrl+Maj+E")
        self.btn_cursus.setObjectName("secondary")
        self.btn_cursus.clicked.connect(self.cursus_requested)
        row.addWidget(self.btn_export)
        row.addWidget(self.btn_cursus)
        actions.lay.addLayout(row)
        dup = QFrame()
        dup.setObjectName("soft")
        dl = QHBoxLayout(dup)
        dl.setContentsMargins(10, 8, 12, 8)
        dl.addWidget(IconChip("stamp", "amber", 36))
        dcol = QVBoxLayout()
        dcol.setSpacing(1)
        t = QLabel("Timbre « Duplicata conforme »")
        f = t.font()
        f.setWeight(QFont.Weight.DemiBold)
        t.setFont(f)
        dcol.addWidget(t)
        dcol.addWidget(muted("Horodaté et journalisé · l'original reste intact", 8.5))
        dl.addLayout(dcol, 1)
        self.dup_switch = Switch(False)
        self.dup_switch.setAccessibleName("Apposer le timbre Duplicata conforme")
        self.dup_switch.toggled.connect(lambda _: self._refresh_view())
        dl.addWidget(self.dup_switch)
        actions.lay.addWidget(dup)
        src = QHBoxLayout()
        self.source = muted("", 8.5)
        self.source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        src.addWidget(self.source, 1)
        self.btn_open = QPushButton("Ouvrir l'emplacement du fichier original")
        self.btn_open.setObjectName("link")
        self.btn_open.clicked.connect(self.open_location_requested)
        src.addWidget(self.btn_open)
        actions.lay.addLayout(src)
        self.toast = Toast()
        actions.lay.addWidget(self.toast)
        lay.addWidget(actions)
        self.set_enabled(False)

    def resizeEvent(self, event):
        # Écran étroit (1366×768) : les 4 tuiles passent sur 2 lignes au lieu d'être coupées.
        cols = 2 if self.width() < 640 else 4
        if cols != self._grid_cols:
            self._grid_cols = cols
            for t in self._tile_frames:
                self._grid.removeWidget(t)
            for i, t in enumerate(self._tile_frames):
                self._grid.addWidget(t, i // cols, i % cols)
        super().resizeEvent(event)

    # ----------------------------------------------------------------
    @property
    def duplicata(self) -> bool:
        return self.dup_switch.isChecked()

    def set_enabled(self, on: bool) -> None:
        for b in (self.btn_print, self.btn_export, self.btn_cursus, self.btn_open):
            b.setEnabled(on)

    def clear(self) -> None:
        self.rel = None
        self._drop_stamped()
        self.name.setText("Aucun relevé sélectionné")
        self.details.setText("")
        self.statut.hide()
        for v in self.tiles.values():
            v.setText("—")
        self._clear_alerts()
        self.source.setText("")
        self.viewer.show_message("Aucun résultat. Vérifiez la saisie ou activez la tolérance aux fautes.")
        self._rebuild_pages(0)
        self.set_enabled(False)

    def _clear_alerts(self) -> None:
        while self.alerts.count():
            w = self.alerts.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()

    def set_releve(self, rel: Releve, siblings: list[Releve]) -> None:
        self.rel = rel
        self._page = 0
        self.name.setText(f"{(rel.nom or rel.nom_complet).upper()} {rel.prenom or ''}".strip())
        self.statut.set(rel.statut or "Décision non lue", statut_tone(rel.statut))
        self.statut.show()
        self.tiles["mat"].setText(rel.matricule)
        self.tiles["niv"].setText(f"{rel.annee_academique or '?'} · {rel.niveau or '?'}")
        sem = f"S{rel.semestre}" if rel.semestre else "?"
        if rel.credits is not None:
            sem += f" · {rel.credits}/30 crédits"
        if (rel.session or "").lower().startswith("rattrap"):
            sem += " · rattrapage"
        self.tiles["ses"].setText(sem)
        bits = []
        if rel.moyenne:
            bits.append(f"Moyenne {rel.moyenne}/20")
        if rel.grade:
            bits.append(f"grade {rel.grade}" + (f" (MGP {rel.mgp})" if rel.mgp else ""))
        if rel.decision_deduite:
            bits.append(f"décision déduite : {rel.decision}")
        elif rel.decision:
            bits.append(f"décision : « {rel.decision} »")
        if rel.date_connue:
            bits.append(f"né(e) le {rel.date_connue}" + (f" à {rel.lieu_naissance}" if rel.lieu_naissance else ""))
        elif rel.date_naissance:
            bits.append("date de naissance non renseignée sur le relevé")
        self.details.setText(" · ".join(bits))
        self.tiles["pages"].setText(rel.pages_label + (" liées" if rel.nb_pages > 1 else ""))
        self._clear_alerts()
        for text, tone in self._alerts(rel, siblings):
            self.alerts.addWidget(Alert(text, tone))
        self.source.setText(f"Source : {rel.chemin} · {rel.type_fichier}")
        self.source.setToolTip(rel.chemin)
        self._rebuild_pages(rel.nb_pages)
        self.set_enabled(True)
        if self.previews is not None and self.previews.needs_render(rel):
            self._render_async(rel)
        else:
            self._refresh_view()

    def _render_async(self, rel: Releve) -> None:
        """Première consultation d'un relevé Word : le PDF d'aperçu est produit en arrière-plan."""
        self.btn_print.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.viewer.show_message("Préparation de l'aperçu Word… (première consultation de ce fichier, "
                                 "quelques secondes)")
        self._rendering = rel.id
        task = Task(self.previews.ensure, rel)

        def done(new_rel):
            if self.rel is None or self.rel.id != new_rel.id:
                return   # l'agent est passé à un autre relevé entre-temps
            self.rel = new_rel
            self.tiles["pages"].setText(new_rel.pages_label + (" liées" if new_rel.nb_pages > 1 else ""))
            self._rebuild_pages(new_rel.nb_pages)
            self.set_enabled(True)
            self._refresh_view()
            self.releve_updated.emit(new_rel)

        def failed(err):
            if self.rel is None or self.rel.id != rel.id:
                return
            self.viewer.show_message(f"Aperçu indisponible : {err}")
            self.alerts.addWidget(Alert(f"Aperçu Word impossible : {err}", "red"))
            self.btn_print.setEnabled(False)
            self.btn_export.setEnabled(False)

        task.signals.done.connect(done)
        task.signals.failed.connect(failed)
        QThreadPool.globalInstance().start(task)

    def _drop_stamped(self) -> None:
        if self._stamped:
            self.viewer.forget(self._stamped)
            try:
                os.unlink(self._stamped)
            except OSError:
                pass
            self._stamped = None

    def _alerts(self, rel: Releve, siblings: list[Releve]) -> list[tuple[str, str]]:
        out = []
        ok_sib = [s for s in siblings if s.is_success]
        if not Path(rel.chemin).exists():
            out.append(("Fichier source introuvable (déplacé ou renommé) : lancez la vérification d'intégrité.", "red"))
        periode = rel.periode
        if not rel.is_success and ok_sib:
            s = ok_sib[0]
            out.append((f"Relevé NON VALIDÉ ({periode}, {rel.annee_academique}). Un relevé {s.statut} existe pour la "
                        f"même période ({s.annee_academique}, {s.pages_label.lower()}"
                        + (", rattrapage" if (s.session or "").lower().startswith("rattrap") else "")
                        + ") : vérifiez lequel délivrer.", "red"))
        elif siblings:
            autres = ", ".join(f"{s.annee_academique} {s.statut or '?'}" for s in siblings[:3])
            out.append((f"{len(siblings) + 1} relevés existent pour {periode} (autres : {autres}). "
                        f"Vous êtes sur celui de {rel.annee_academique}.", "amber"))
        elif rel.statut and not rel.is_success:
            cred = f" ({rel.credits}/30 crédits)" if rel.credits is not None else ""
            out.append((f"« {rel.decision or rel.statut} »{cred} : {periode} non validé. "
                        "Délivrance soumise à la scolarité.", "red"))
        elif not rel.statut:
            out.append(("Décision du jury non lue automatiquement : contrôlez-la sur l'aperçu.", "amber"))
        if rel.nb_pages > 1:
            pages = " et ".join(str(p) for p in range(rel.numero_page, rel.page_fin + 1))
            out.append((f"Ce relevé comporte {rel.nb_pages} pages ({pages}) : elles partent ensemble en recto-verso.", "amber"))
        if rel.score < 100:
            out.append((f"Résultat approché ({rel.score} %) : le nom indexé est « {rel.nom_complet} ». "
                        "Confirmez l'identité avec l'étudiant.", "amber"))
        if rel.type_fichier == "PDF_SCAN":
            conf = f" (confiance {rel.ocr_confiance:.0f} %)" if rel.ocr_confiance is not None else ""
            tone = "amber" if (rel.ocr_confiance or 100) < 70 else "blue"
            out.append((f"Texte issu de l'OCR{conf} : contrôlez le matricule sur l'image avant impression.", tone))
        if rel.decision_deduite:
            out.append(("Ancien format (2012-2014) : aucune décision semestrielle n'est imprimée sur ce relevé ; "
                        "le statut est déduit de la colonne JURY des UE. Vérifiez sur l'aperçu.", "blue"))
        if rel.date_naissance and not rel.date_connue:
            out.append((f"Date de naissance fictive sur le relevé ({rel.date_naissance}) : à faire corriger "
                        "par la scolarité avant délivrance.", "amber"))
        if rel.is_word and self.rules_active:
            out.append(("En-tête corrigé à l'aperçu et à l'impression (règles de Réglages › Corrections). "
                        "Le fichier d'origine n'est pas modifié.", "blue"))
        if not rel.preview_pdf and not rel.is_word:
            out.append(("Aperçu indisponible : fichier source introuvable.", "blue"))
        if not out:
            out.append(("Document prêt — aucune anomalie détectée.", "green"))
        return out

    def _rebuild_pages(self, n: int) -> None:
        while self.page_seg_holder.count():
            w = self.page_seg_holder.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()
        if not self.rel or n <= 0:
            return
        options = [(i, f"Page {self.rel.numero_page + i}") for i in range(n)]
        seg = Segmented(options, 0)
        seg.changed.connect(self._go_page)
        self.page_seg_holder.addWidget(seg)

    def _go_page(self, i: int) -> None:
        self._page = i
        self._refresh_view()

    def _refresh_view(self) -> None:
        rel = self.rel
        if rel is None:
            return
        src = rel.preview_pdf
        if self.previews is not None and self.previews.needs_render(rel):
            return   # aperçu en préparation
        if self.duplicata and src and Path(src).exists():
            self._drop_stamped()
            self._stamped = str(pdf_tools.extract_pages(src, rel.numero_page, rel.page_fin,
                                                        stamp=pdf_tools.stamp_text()))
            self.viewer.show_page(self._stamped, self._page)
        else:
            self.viewer.show_page(src, rel.numero_page - 1 + self._page)

    def next_page_key(self) -> None:
        if self.rel and self.rel.nb_pages > 1:
            self._go_page((self._page + 1) % self.rel.nb_pages)
