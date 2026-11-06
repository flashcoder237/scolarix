"""Écran 2 : Guichet d'exploitation (usage quotidien) — split-view permanent, zéro pop-up."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QThreadPool, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QScrollArea, QSplitter, QStackedWidget, QTableView, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from core import exports, pdf_tools
from core.config import Settings
from core.database import Database, Releve, missing_periods
from core.guard import PrintBlocked, check_before_print
from core.printer_service import print_pdf

from .cursus_dialog import CursusDialog
from .demandes_ui import DemandeBar
from .omnibox_widget import Omnibox
from .pdf_preview_panel import PreviewPanel
from .results_table_model import ResultsModel, RowDelegate
from .widgets import (TONES, Badge, Card, Segmented, StatCard, Task, eyebrow, muted, statut_tone, title)

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
        "novembre", "décembre"]
FILIERE_PILLS = [(None, "Toutes"), ("M", "Médecine"), ("P", "Pharmacie"), ("B", "Biomédical"), ("MS", "Masters")]


def date_fr(d: datetime | None = None) -> str:
    d = d or datetime.now()
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {d.year}"


def greeting() -> str:
    h = datetime.now().hour
    return "Bonjour" if h < 12 else ("Bon après-midi" if h < 18 else "Bonsoir")


class GuichetPage(QWidget):
    open_settings = Signal()

    def __init__(self, db: Database, settings: Settings, previews=None, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self.db, self.s = db, settings
        self.results: list[Releve] = []
        self._all: list[Releve] = []
        self._consulted: set[int] = set()
        self.filiere: str | None = None
        self.tolerant = True

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        # --------------------------------------------------- en-tête
        header = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(4)
        self.date_label = eyebrow(f"{date_fr()} · Guichet d'exploitation")
        col.addWidget(self.date_label)
        self.hello = title("", 22, "h1")
        col.addWidget(self.hello)
        header.addLayout(col, 1)
        ro = Badge("Sources en lecture seule", "green", big=True)
        header.addWidget(ro, 0, Qt.AlignmentFlag.AlignBottom)
        self.poste_btn = QPushButton()
        self.poste_btn.setObjectName("ghost")
        self.poste_btn.setToolTip("Réglages du poste : opérateur, imprimante, chemins")
        self.poste_btn.clicked.connect(self.open_settings)
        header.addWidget(self.poste_btn, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(header)

        # --------------------------------------------------- cartes d'état
        stats = QGridLayout()
        stats.setSpacing(12)
        self.card_index = StatCard("shield", "green", "Base d'index", "—")
        self.card_indexing = StatCard("database", "blue", "Indexation", "—", progress=True)
        self.card_prints = StatCard("printer", "violet", "Imprimés aujourd'hui", "—")
        self.card_anom = StatCard("alert", "amber", "Anomalies à revoir", "—")
        for i, c in enumerate((self.card_index, self.card_indexing, self.card_prints, self.card_anom)):
            stats.addWidget(c, 0, i)
        root.addLayout(stats)

        # --------------------------------------------------- demande en cours (une personne = une demande)
        self.demande_bar = DemandeBar(db, settings, lambda: self.current)
        self.demande_bar.previews = previews
        self.demande_bar.changed.connect(self._mark_demande)
        root.addWidget(self.demande_bar)

        # --------------------------------------------------- Zone A : Omnibox + filtres
        search = Card(padding=16, spacing=12)
        row = QHBoxLayout()
        row.setSpacing(16)
        self.omni = Omnibox()
        self.omni.search.connect(self.run_search)
        self.omni.navigate.connect(self.move_selection)
        row.addWidget(self.omni, 1)
        meta = QVBoxLayout()
        meta.setSpacing(2)
        self.count = title("", 11.5)
        meta.addWidget(self.count)
        self.timing = muted("Recherche au fil de la frappe (debounce 120 ms)", 8.5)
        meta.addWidget(self.timing)
        row.addLayout(meta)
        search.lay.addLayout(row)
        filters = QHBoxLayout()
        filters.setSpacing(6)
        filters.addWidget(eyebrow("Filières"))
        filters.addSpacing(4)
        self.pill_group = QButtonGroup(self)
        self.pills: dict = {}
        for code, label in FILIERE_PILLS:
            b = QPushButton(label)
            b.setObjectName("pill")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, c=code: self.set_filiere(c))
            self.pill_group.addButton(b)
            self.pills[code] = (b, label)
            filters.addWidget(b)
        self.pills[None][0].setChecked(True)
        filters.addSpacing(24)
        filters.addWidget(eyebrow("Mode"))
        self.mode = Segmented([(False, "Exacte"), (True, "Tolérance fautes / phonétique")], True)
        self.mode.changed.connect(self.set_mode)
        filters.addWidget(self.mode)
        filters.addStretch(1)
        search.lay.addLayout(filters)
        root.addWidget(search)

        # --------------------------------------------------- Split-view (QSplitter 55 / 45)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)

        results = Card(padding=10, spacing=10)
        rh = QHBoxLayout()
        rh.setContentsMargins(8, 6, 8, 0)
        rcol = QVBoxLayout()
        rcol.setSpacing(2)
        rcol.addWidget(eyebrow("Résultats · triés par pertinence"))
        rcol.addWidget(title("Relevés trouvés", 13))
        rh.addLayout(rcol, 1)
        self.view_seg = Segmented([("releve", "Par relevé"), ("groupe", "Groupée par étudiant")], "releve")
        self.view_seg.changed.connect(self.set_view)
        rh.addWidget(self.view_seg, 0, Qt.AlignmentFlag.AlignBottom)
        results.lay.addLayout(rh)

        self.model = ResultsModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegate(RowDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        self.table.setMouseTracking(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(RowDelegate.ROW_H)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for i, w in enumerate((92, 0, 138, 108, 112, 56)):
            if w:
                hh.resizeSection(i, w)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setMinimumSectionSize(48)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.selectionModel().currentRowChanged.connect(lambda cur, _prev: self.on_row(cur.row()))
        self.table.setAccessibleName("Résultats de recherche")

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["MATRICULE / NIVEAU", "NOM & PRÉNOM", "ANNÉE · MOYENNE", "STATUT", "PAGES"])
        self.tree.setAlternatingRowColors(False)
        self.tree.setUniformRowHeights(False)
        self.tree.currentItemChanged.connect(self.on_tree_item)
        for i, w in enumerate((170, 240, 170, 120, 70)):
            self.tree.setColumnWidth(i, w)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.tree)
        results.lay.addWidget(self.stack, 1)
        keys = QLabel("Échap nouvel étudiant   ·   ↑ ↓ parcourir + aperçu   ·   Ctrl+P imprimer   ·   "
                      "Ctrl+E exporter   ·   Ctrl+Maj+E cursus")
        keys.setStyleSheet("QLabel{background:#F6F8FB;border-radius:14px;padding:9px 12px;color:#475569;font-size:8.5pt;}")
        keys.setWordWrap(True)
        results.lay.addWidget(keys)
        split.addWidget(results)

        self.preview = PreviewPanel()
        self.previews = previews
        self.preview.previews = previews
        self.preview.rules_active = settings.text_rules_enabled and any(r.get("actif", True) for r in settings.text_rules)
        self.preview.print_requested.connect(self.print_current)
        self.preview.export_requested.connect(self.export_current)
        self.preview.cursus_requested.connect(self.open_cursus)
        self.preview.open_location_requested.connect(self.open_location)
        self.preview.releve_updated.connect(self._on_releve_updated)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # pas de défilement horizontal
        holder = QWidget()
        holder.setObjectName("page")
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 6, 0)
        hl.addWidget(self.preview)
        self.preview.setMinimumWidth(420)
        scroll.setWidget(holder)
        split.addWidget(scroll)
        split.setStretchFactor(0, 55)
        split.setStretchFactor(1, 45)
        split.setSizes([580, 420])
        root.addWidget(split, 1)

        self.refresh_header()
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self.refresh_stats)
        self._stats_timer.start(10_000)
        self.refresh_stats()
        self.run_search("")

    # ------------------------------------------------------------ en-tête & état
    def refresh_header(self) -> None:
        self.hello.setText(f"{greeting()}, {self.s.operateur}")
        self.poste_btn.setText(f"  Poste {self.s.poste}  ▾")

    def refresh_stats(self) -> None:
        try:
            st = self.db.stats()
            today = datetime.now().strftime("%Y-%m-%d")
            prints = self.db.conn.execute(
                "SELECT action, COUNT(*) FROM journal WHERE horodatage >= ? AND action IN ('IMPRESSION','DUPLICATA') "
                "GROUP BY action", (today,)).fetchall()
            counts = {a: n for a, n in prints}
            anomalies = self.db.conn.execute(
                "SELECT COUNT(*) FROM anomalies WHERE niveau IN ('ERREUR','AVERTISSEMENT')").fetchone()[0]
        except Exception:
            return
        self.card_index.set(f"{st['releves']:,} relevés · {st['etudiants']:,} étudiants".replace(",", " "))
        if not getattr(self, "_indexing_live", False):
            last = self.db.get_meta("derniere_indexation")
            self.card_indexing.set(f"Dernière passe : {last}" if last else "Jamais lancée", None)
        self.card_prints.set(f"{counts.get('IMPRESSION', 0) + counts.get('DUPLICATA', 0)} relevés · "
                             f"{counts.get('DUPLICATA', 0)} duplicata(s)")
        self.card_anom.set(f"{anomalies} fichier(s)")

    def on_indexing_progress(self, done: int, total: int, running: bool) -> None:
        self._indexing_live = running
        if running and total:
            self.card_indexing.set(f"{done * 100 // total} % · {done} / {total} fichiers", done / total)
        else:
            self.refresh_stats()

    # ------------------------------------------------------------ recherche
    def run_search(self, text: str) -> None:
        t0 = time.perf_counter()
        q = text.strip()
        self._all = self.db.search(q, None, self.tolerant, self.s.fuzzy_threshold, self.s.max_results) if q else []
        ms = (time.perf_counter() - t0) * 1000
        self.apply_filter()
        self.timing.setText(f"{ms:.0f} ms · recherche au fil de la frappe (debounce 120 ms)" if q
                            else "Tapez un matricule (18M042, 18-M-042…), un nom ou une année")

    def apply_filter(self) -> None:
        counts = {None: len(self._all)}
        for r in self._all:
            counts[r.filiere_code] = counts.get(r.filiere_code, 0) + 1
        for code, (b, label) in self.pills.items():
            n = counts.get(code, 0)
            b.setText(f"{label}  {n}" if self._all else label)
        self.results = [r for r in self._all if self.filiere is None or r.filiere_code == self.filiere]
        n_st = len({r.matricule for r in self.results})
        self.count.setText(f"{len(self.results)} relevé{'s' if len(self.results) > 1 else ''} · "
                           f"{n_st} étudiant{'s' if n_st > 1 else ''}" if self.omni.text().strip() else "Prêt")
        ids = self.demande_bar.releve_ids()
        for r in self.results:
            if r.id in ids and "DANS LA DEMANDE" not in r.flags:
                r.flags.append("DANS LA DEMANDE")
        self.model.set_results(self.results, self.omni.text())
        if self.stack.currentIndex() == 1:   # vue groupée affichée seulement : pas de requêtes inutiles
            self._fill_tree()
        if self.results:
            self.table.selectRow(0)
            self.table.setCurrentIndex(self.model.index(0, 0))
            self.on_row(0)
        else:
            self.preview.clear()

    def set_filiere(self, code) -> None:
        self.filiere = code
        self.apply_filter()

    def set_mode(self, tolerant) -> None:
        self.tolerant = bool(tolerant)
        self.run_search(self.omni.text())

    def set_view(self, mode) -> None:
        self.stack.setCurrentIndex(0 if mode == "releve" else 1)
        if mode != "releve":
            self._fill_tree()

    def _fill_tree(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        groups: dict[str, list[Releve]] = {}
        for r in self.results:
            groups.setdefault(r.matricule, []).append(r)
        dossiers = self.db.dossiers(groups) if groups else {}
        bold = QFont()
        bold.setWeight(QFont.Weight.DemiBold)
        for mat, rels in groups.items():
            rels = sorted(rels, key=lambda r: (r.niveau_num, r.semestre or 0, r.annee_academique or ""))
            dossier = dossiers.get(mat) or rels      # tout le dossier, pas seulement les relevés trouvés
            missing = missing_periods(dossier)
            n_ok = sum(1 for r in dossier if r.is_success)
            latest = max(dossier or rels, key=lambda r: (r.annee_academique or "", r.niveau_num, r.semestre or 0))
            cover = " ".join(dict.fromkeys(
                (f"S{r.semestre}" if r.semestre else
                 (f"N{r.niveau_num}" if 0 < r.niveau_num < 100 else (r.niveau or "?").replace("Master ", "M")))
                for r in rels))
            etat = (", ".join(missing) + " manquant" + ("s" if len(missing) > 1 else "")) if missing else \
                f"Tous présents · {n_ok}/{len(dossier)} validé(s)"
            top = QTreeWidgetItem([mat, latest.nom_complet, f"{len(rels)} relevé(s) · {cover}", etat, ""])
            top.setFont(0, bold)
            top.setFont(1, bold)
            top.setForeground(3, QColor(TONES["red" if missing else ("green" if n_ok == len(dossier) else "amber")][0]))
            top.setData(0, Qt.ItemDataRole.UserRole, rels[0].id)
            for r in rels:
                child = QTreeWidgetItem([r.periode, ", ".join(r.flags) or "", f"{r.annee_academique} · moy. {r.moyenne or '—'}",
                                         r.statut or "—", r.pages_label.replace("Pages ", "").replace("Page ", "")])
                child.setData(0, Qt.ItemDataRole.UserRole, r.id)
                child.setForeground(3, QColor(TONES[statut_tone(r.statut)][0]))
                child.setForeground(1, QColor(TONES["amber"][0]))
                top.addChild(child)
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)
        self.tree.blockSignals(False)

    def move_selection(self, step: int) -> None:
        if self.stack.currentIndex() == 1:
            cur = self.tree.currentItem()
            nxt = (self.tree.itemBelow(cur) if step > 0 else self.tree.itemAbove(cur)) if cur else self.tree.topLevelItem(0)
            if nxt:
                self.tree.setCurrentItem(nxt)
            return
        if not self.results:
            return
        row = max(0, min(len(self.results) - 1, self.table.currentIndex().row() + step))
        idx = self.model.index(row, 0)
        self.table.selectionModel().setCurrentIndex(
            idx, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        self.table.scrollTo(idx)

    def on_tree_item(self, item, _prev=None) -> None:
        if item is None:
            return
        rid = item.data(0, Qt.ItemDataRole.UserRole)
        rel = next((r for r in self.results if r.id == rid), None)
        if rel:
            self._show(rel)

    def on_row(self, row: int) -> None:
        rel = self.model.releve_at(row)
        if rel:
            self._show(rel)

    def _show(self, rel: Releve) -> None:
        self.preview.set_releve(rel, self.db.level_siblings(rel))
        if rel.id not in self._consulted:
            self._consulted.add(rel.id)
            self.db.log("CONSULTATION", self.s.operateur, self.s.poste, rel.id, rel.matricule,
                        f"{rel.periode} · {rel.annee_academique} · {rel.statut or '?'} · {rel.pages_label}")

    def add_to_demande(self) -> None:
        msg = self.demande_bar.add_current()
        if msg:
            self.preview.toast.show_message(msg, "amber" if msg.startswith(("Attention", "Ce relevé est déjà",
                                                                              "Sélectionnez")) else "green")

    def _mark_demande(self) -> None:
        """Les relevés déjà dans la demande en cours sont signalés dans la liste des résultats."""
        ids = self.demande_bar.releve_ids()
        for r in getattr(self.model, "rows", []):
            had = "DANS LA DEMANDE" in r.flags
            if (r.id in ids) != had:
                if had:
                    r.flags.remove("DANS LA DEMANDE")
                else:
                    r.flags.append("DANS LA DEMANDE")
        if hasattr(self.model, "layoutChanged"):
            self.model.layoutChanged.emit()

    def _on_releve_updated(self, rel: Releve) -> None:
        """Aperçu Word produit : les numéros de page exacts remplacent l'estimation dans la liste."""
        for i, r in enumerate(self.model.rows):
            if r.id == rel.id:
                rel.flags, rel.score = r.flags, r.score
                self.model.rows[i] = rel
        for i, r in enumerate(self.results):
            if r.id == rel.id:
                self.results[i] = rel

    # ------------------------------------------------------------ actions
    @property
    def current(self) -> Releve | None:
        return self.preview.rel

    def _ready(self, rel: Releve) -> Releve | None:
        """Relevé prêt à imprimer, après le contrôle de dernière ligne (core/guard.py). L'aperçu Word n'est
        JAMAIS produit ici (fil de l'interface) : s'il manque, on lance sa préparation en arrière-plan."""
        if self.preview.render_state == "en cours":
            self.preview.toast.show_message("Aperçu Word en préparation : réessayez dès qu'il s'affiche.", "amber")
            return None
        if self.previews is not None and self.previews.needs_render(rel):
            if self.preview.render_state == "échec":
                self.preview.toast.show_message("Aperçu Word indisponible pour ce relevé : impression impossible.", "red")
            else:
                self.preview._render_async(rel)
                self.preview.toast.show_message("Préparation de l'aperçu Word… relancez l'impression quand il "
                                                "s'affiche.", "amber")
            return None
        try:
            check_before_print(rel)
        except PrintBlocked as exc:
            self.preview.toast.show_message(str(exc), "red", 9000)
            self.db.log("IMPRESSION BLOQUÉE", self.s.operateur, self.s.poste, rel.id, rel.matricule, str(exc)[:300])
            return None
        return rel

    def _trace(self, rel: Releve) -> str:
        return " · en-tête corrigé" if rel.is_word and self.preview.rules_active else ""

    def print_current(self) -> None:
        rel = self.current and self._ready(self.current)
        if not rel:
            return
        dup = self.preview.duplicata
        ref = self.db.new_delivery("DUPLICATA IMPRIMÉ" if dup else "IMPRESSION", self.s.poste, self.s.operateur,
                                   rel.matricule, rel.id)
        tmp = pdf_tools.extract_pages(rel.preview_pdf, rel.numero_page, rel.page_fin,
                                      stamp=pdf_tools.stamp_text() if dup else None,
                                      footer=pdf_tools.footer_text(ref) if self.s.delivery_ref_stamp else None)
        self.db.complete_delivery(ref, None, exports.sha256_file(tmp))
        self.preview.toast.show_message(f"Envoi à l'imprimante… ({rel.pages_label})", "blue", 4000)
        task = Task(print_pdf, tmp, self.s)

        def ok(how):
            self.db.log("DUPLICATA" if dup else "IMPRESSION", self.s.operateur, self.s.poste, rel.id, rel.matricule,
                        f"Réf. {ref} · {rel.periode} · {rel.annee_academique} · {rel.pages_label}"
                        + (" · recto-verso" if rel.nb_pages > 1 else "") + self._trace(rel))
            self.preview.toast.show_message(
                f"Envoyé à l'imprimante — {rel.pages_label}" + (" · timbre Duplicata apposé" if dup else "")
                + f" · réf. {ref}")
            self.refresh_stats()
            _cleanup(tmp)

        def ko(err):
            self.preview.toast.show_message(f"Impression impossible : {err}", "red")
            _cleanup(tmp)

        task.signals.done.connect(ok)
        task.signals.failed.connect(ko)
        QThreadPool.globalInstance().start(task)

    def export_current(self) -> None:
        rel = self.current and self._ready(self.current)
        if not rel:
            return
        dup = self.preview.duplicata
        try:
            dest = exports.dest_for(self.s, exports.DUPLICATAS if dup else exports.RELEVES,
                                    pdf_tools.export_filename(rel, "_DUPLICATA" if dup else ""))
            ref = self.db.new_delivery("DUPLICATA" if dup else "EXPORT PDF", self.s.poste, self.s.operateur,
                                       rel.matricule, rel.id)
            pdf_tools.extract_pages(rel.preview_pdf, rel.numero_page, rel.page_fin, dest,
                                    stamp=pdf_tools.stamp_text() if dup else None,
                                    footer=pdf_tools.footer_text(ref) if self.s.delivery_ref_stamp else None)
            self.db.complete_delivery(ref, str(dest), exports.sha256_file(dest))
        except Exception as exc:
            self.preview.toast.show_message(f"Export impossible : {exc}", "red")
            return
        self.db.log("EXPORT PDF", self.s.operateur, self.s.poste, rel.id, rel.matricule,
                    f"Réf. {ref} · {dest}" + (" · DUPLICATA" if dup else "") + self._trace(rel))
        self.last_export = dest
        self.preview.toast.show_message(f"Exporté (réf. {ref}) : {dest.name} — dossier "
                                        f"{exports.short_location(self.s, dest)}", ms=15000,
                                        action=("Ouvrir le dossier", lambda: exports.reveal(dest)))

    def open_cursus(self) -> None:
        rel = self.current
        if rel:
            CursusDialog(self.db, self.s, rel.matricule, self, previews=self.previews).exec()
            self.refresh_stats()

    def open_location(self) -> None:
        rel = self.current
        if not rel:
            return
        path = Path(rel.chemin)
        if sys.platform == "win32" and path.exists():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
        self.preview.toast.show_message("Dossier source ouvert (fichier surligné). Les originaux restent en lecture seule.")

    def focus_search(self) -> None:
        self.omni.focus_and_select()


def _cleanup(path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
