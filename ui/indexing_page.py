"""Écran 1 : Console d'administration & d'indexation (usage ponctuel / mise à jour).

Le robot tourne dans un processus séparé (QProcess) : l'interface reste fluide et le guichet peut
rechercher pendant que l'indexation continue.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
                               QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
                               QAbstractItemView, QHeaderView)

from core.config import Settings, save_settings
from core.database import Database
from core.indexer import default_workers

from .icons import icon
from .widgets import (Badge, Card, HBars, IconChip, OptionTile, PillBars, Segmented, Toast, card_header,
                      eyebrow, muted, title)

FORMAT_LABELS = {"DOCX": ("Word .docx", "#1D4ED8"), "DOC": ("Word .doc 97-2003", "#6D28D9"),
                 "PDF_TEXT": ("PDF texte", "#15803D"), "PDF_SCAN": ("PDF scanné (OCR)", "#B45309")}
LEVELS = {"ERREUR": ("red", "x"), "AVERTISSEMENT": ("amber", "alert"), "INFO": ("blue", "info")}


def indexer_command() -> tuple[str, list[str]]:
    if getattr(sys, "frozen", False):
        return sys.executable, []
    exe = sys.executable
    if sys.platform == "win32":  # pythonw : pas de fenêtre console qui clignote pendant l'indexation
        candidate = Path(exe).with_name("pythonw.exe")
        if candidate.exists():
            exe = str(candidate)
    return exe, [str(Path(__file__).resolve().parent.parent / "main.py")]


class KpiCard(QFrame):
    def __init__(self, icon_name, tone, label, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(6)
        lay.addWidget(IconChip(icon_name, tone, 38))
        lay.addWidget(muted(label, 8.5))
        self.value = QLabel("—")
        f = self.value.font()
        f.setPointSizeF(18)
        f.setWeight(QFont.Weight.Bold)
        self.value.setFont(f)
        if tone == "red":
            self.value.setStyleSheet("color:#B91C1C;")
        lay.addWidget(self.value)


class StageCard(QFrame):
    def __init__(self, n, label, tech, parent=None):
        super().__init__(parent)
        self.setObjectName("soft")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(4)
        top = QHBoxLayout()
        num = QLabel(str(n))
        num.setFixedSize(32, 32)
        num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        num.setStyleSheet("QLabel{background:#FFFFFF;border-radius:10px;color:#1D4ED8;font-weight:800;}")
        top.addWidget(num)
        top.addStretch(1)
        self.badge = Badge("En attente", "gray")
        top.addWidget(self.badge)
        lay.addLayout(top)
        t = QLabel(label)
        f = t.font()
        f.setWeight(QFont.Weight.DemiBold)
        t.setFont(f)
        lay.addWidget(t)
        self.value = QLabel("0")
        f2 = self.value.font()
        f2.setPointSizeF(15)
        f2.setWeight(QFont.Weight.Bold)
        self.value.setFont(f2)
        lay.addWidget(self.value)
        lay.addWidget(muted(tech, 8))


class IndexingPage(QWidget):
    progress = Signal(int, int, bool)   # done, total, running
    finished = Signal()

    def __init__(self, db: Database, settings: Settings, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self.db, self.s = db, settings
        self.proc: QProcess | None = None
        self._stopping = False
        self._buf = b""
        self._filter = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        body.setObjectName("page")
        scroll.setWidget(body)
        root = QVBoxLayout(body)
        root.setContentsMargins(0, 0, 8, 8)
        root.setSpacing(16)

        head = QHBoxLayout()
        c = QVBoxLayout()
        c.setSpacing(4)
        c.addWidget(eyebrow("Administration · usage ponctuel"))
        c.addWidget(title("Console d'indexation", 22, "h1"))
        head.addLayout(c, 1)
        self.last = Badge("", "gray", big=True)
        head.addWidget(self.last, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(head)

        kpis = QGridLayout()
        kpis.setSpacing(12)
        self.k = {}
        for i, (key, ic, tone, label) in enumerate((
                ("releves", "doc", "blue", "Relevés indexés"), ("etudiants", "layers", "violet", "Étudiants distincts"),
                ("pages", "eye", "green", "Pages analysées"), ("multi_pages", "link", "amber", "Relevés sur plusieurs pages"),
                ("echecs", "x", "red", "Fichiers en échec"))):
            card = KpiCard(ic, tone, label)
            self.k[key] = card
            kpis.addWidget(card, 0, i)
        root.addLayout(kpis)

        row = QHBoxLayout()
        row.setSpacing(16)
        # ---------------- progression
        prog = Card(padding=20, spacing=14)
        ph = QHBoxLayout()
        ph.addWidget(card_header("Robot d'indexation · processus séparé", "Progression"), 1)
        self.status = Badge("PRÊT", "gray", big=True)
        ph.addWidget(self.status, 0, Qt.AlignmentFlag.AlignTop)
        prog.lay.addLayout(ph)
        big = QHBoxLayout()
        self.pct = QLabel("0 %")
        f = self.pct.font()
        f.setPointSizeF(34)
        f.setWeight(QFont.Weight.Bold)
        self.pct.setFont(f)
        big.addWidget(self.pct)
        self.detail = muted("Choisissez le dossier des archives puis lancez l'indexation.", 9.5)
        big.addWidget(self.detail, 1)
        self.run_btn = QPushButton(icon("play", "#FFFFFF", 20), "")
        self.run_btn.setObjectName("ink")
        self.run_btn.setFixedSize(56, 56)
        self.run_btn.setToolTip("Lancer l'indexation")
        self.run_btn.setAccessibleName("Lancer ou arrêter l'indexation")
        self.run_btn.setStyleSheet("QPushButton{border-radius:18px;}")
        self.run_btn.clicked.connect(self.toggle_run)
        big.addWidget(self.run_btn)
        prog.lay.addLayout(big)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        prog.lay.addWidget(self.bar)
        cur = QFrame()
        cur.setObjectName("soft")
        cl = QHBoxLayout(cur)
        cl.setContentsMargins(12, 10, 12, 10)
        cl.addWidget(IconChip("doc", "blue", 36))
        self.current = muted("Fichier en cours : —", 9)
        self.current.setStyleSheet("color:#0F172A;")
        cl.addWidget(self.current, 1)
        prog.lay.addWidget(cur)
        stages = QGridLayout()
        stages.setSpacing(10)
        self.stages = [StageCard(1, "Détection MIME", ".doc · .docx · PDF texte · PDF scan"),
                       StageCard(2, "Extraction texte", "PDFium · python-docx · Word/LibreOffice (.doc) · Tesseract"),
                       StageCard(3, "Parsing regex", "Blocs étudiant : matricule, nom, filière, page"),
                       StageCard(4, "Écriture SQLite", "etudiants_releves + index FTS5")]
        for i, sc in enumerate(self.stages):
            stages.addWidget(sc, 0, i)
        prog.lay.addLayout(stages)
        info = QLabel("Le guichet recherche et imprime déjà les relevés indexés pendant que le robot travaille. "
                      "Arrêter puis relancer en différentiel reprend là où il s'est arrêté.")
        info.setWordWrap(True)
        info.setStyleSheet("QLabel{background:#EEF4FF;color:#1D4ED8;border-radius:14px;padding:10px 12px;font-weight:600;}")
        prog.lay.addWidget(info)
        row.addWidget(prog, 6)

        # ---------------- configuration
        conf = Card(padding=20, spacing=12)
        conf.lay.addWidget(card_header("Configuration", "Source & traitement"))
        src = QHBoxLayout()
        self.src = QLineEdit(self.s.source_root or db.get_meta("source_root", "") or "")
        self.src.setObjectName("field")
        self.src.setPlaceholderText(r"\\SRV-FMSP\Archives_Releves")
        self.src.setAccessibleName("Dossier racine des archives")
        src.addWidget(self.src, 1)
        browse = QPushButton("Parcourir…")
        browse.setObjectName("ink")
        browse.clicked.connect(self.browse)
        src.addWidget(browse)
        conf.lay.addLayout(src)
        ro = QLabel(f"Lecture seule : aucune écriture, suppression ni renommage dans ce dossier. "
                    f"Index local : {self.s.resolved_db_path()}")
        ro.setWordWrap(True)
        ro.setStyleSheet("QLabel{background:#ECFDF3;color:#15803D;border-radius:14px;padding:10px 12px;font-weight:600;font-size:8.5pt;}")
        conf.lay.addWidget(ro)
        grid = QGridLayout()
        grid.setSpacing(10)
        self.opts = {
            "ocr_enabled": OptionTile("scan", "violet", "OCR des scans", "Tesseract sur les PDF d'imprimante (plus lent).",
                                      self.s.ocr_enabled),
            "ocr_preprocess": OptionTile("refresh", "blue", "Deskew + contraste Otsu",
                                         "Redresse les scans de travers avant l'OCR.", self.s.ocr_preprocess),
            "word_com_enabled": OptionTile("word", "blue", "Passerelle COM Word",
                                           "Lit les .doc 97-2003 (sinon LibreOffice).", self.s.word_com_enabled),
            "docx_convert_at_index": OptionTile("eye", "green", "Préconvertir les .docx",
                                                "Aperçu immédiat, mais indexation bien plus lente. Sinon : "
                                                "aperçu produit à la 1re consultation.", self.s.docx_convert_at_index),
            "differential": OptionTile("refresh", "amber", "Réindexation différentielle",
                                       "Ignore les fichiers inchangés depuis la dernière passe.", self.s.differential),
        }
        for i, (key, tile) in enumerate(self.opts.items()):
            tile.toggled.connect(lambda v, k=key: self._set_opt(k, v))
            grid.addWidget(tile, i // 2, i % 2)
        integ = QPushButton(icon("link", "#1D4ED8", 18), "  Vérification d'intégrité\n  (liens orphelins)")
        integ.setObjectName("secondary")
        integ.setMinimumHeight(90)
        integ.clicked.connect(self.check_integrity)
        grid.addWidget(integ, 2, 1)
        conf.lay.addLayout(grid)
        perf = QHBoxLayout()
        perf.addWidget(QLabel("Processus parallèles"))
        self.workers = QSpinBox()
        self.workers.setRange(0, 16)
        self.workers.setSpecialValueText(f"Auto ({default_workers()})")
        self.workers.setValue(int(self.s.index_workers or 0))
        self.workers.setAccessibleName("Nombre de processus parallèles d'indexation")
        self.workers.valueChanged.connect(self._set_workers)
        perf.addWidget(self.workers)
        perf.addStretch(1)
        self.excl_btn = QPushButton()
        self.excl_btn.setObjectName("secondary")
        self.excl_btn.clicked.connect(self.edit_exclusions)
        perf.addWidget(self.excl_btn)
        conf.lay.addLayout(perf)
        self._update_excl_btn()
        self.toast = Toast()
        conf.lay.addWidget(self.toast)
        row.addWidget(conf, 5)
        root.addLayout(row)

        # ---------------- statistiques
        st = QHBoxLayout()
        st.setSpacing(16)
        fcard = Card(padding=20)
        fcard.lay.addWidget(card_header("Fonds documentaire", "Formats détectés"))
        self.formats = HBars()
        fcard.lay.addWidget(self.formats)
        fcard.lay.addStretch(1)
        st.addWidget(fcard, 1)
        ycard = Card(padding=20)
        self.years_head = card_header("Relevés par année académique", "—")
        ycard.lay.addWidget(self.years_head)
        self.years = PillBars(height=210)
        ycard.lay.addWidget(self.years)
        st.addWidget(ycard, 2)
        root.addLayout(st)

        # ---------------- journal d'anomalies
        log = Card(padding=16, spacing=10)
        lh = QHBoxLayout()
        lh.addWidget(card_header("Journal d'audit", "Rapport d'anomalies"), 1)
        self.filter = Segmented([(None, "Tous"), ("ERREUR", "Erreurs"), ("AVERTISSEMENT", "Avertissements"),
                                 ("INFO", "Info")], None)
        self.filter.changed.connect(self._set_filter)
        lh.addWidget(self.filter, 0, Qt.AlignmentFlag.AlignBottom)
        log.lay.addLayout(lh)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["NIVEAU", "FICHIER", "MESSAGE", "HEURE", ""])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setShowGrid(False)
        self.table.setWordWrap(True)
        self.table.setMinimumHeight(300)
        hh = self.table.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for i, w in ((0, 150), (3, 130), (4, 150)):
            self.table.setColumnWidth(i, w)
        log.lay.addWidget(self.table)
        root.addWidget(log)
        self.refresh()

    # ---------------------------------------------------------------- données
    def _set_opt(self, key: str, value: bool) -> None:
        setattr(self.s, key, value)
        save_settings(self.s)

    def _set_workers(self, value: int) -> None:
        self.s.index_workers = int(value)
        save_settings(self.s)

    def _update_excl_btn(self) -> None:
        n = len(self.s.exclusions)
        self.excl_btn.setText(f"Exclusions ({n})…" if n else "Exclure des sous-dossiers…")
        self.excl_btn.setToolTip("\n".join(self.s.exclusions) or "Aucun dossier ni motif exclu")

    def edit_exclusions(self) -> None:
        from .settings_dialog import ExclusionsDialog
        self.s.source_root = self.src.text().strip() or self.s.source_root
        if ExclusionsDialog(self.s, self).exec():
            self._update_excl_btn()

    def _set_filter(self, value) -> None:
        self._filter = value
        self.refresh_log()

    def refresh(self) -> None:
        st = self.db.stats()
        for key, card in self.k.items():
            card.value.setText(f"{st.get(key, 0):,}".replace(",", " "))
        self.formats.set_data([(FORMAT_LABELS.get(k, (k, "#334155"))[0], v, FORMAT_LABELS.get(k, (k, "#334155"))[1])
                               for k, v in st["formats"].items() if k in FORMAT_LABELS])
        years = st["annees"]
        self.years.set_data([(a[2:4] + "-" + a[7:9] if len(a) >= 9 else a, n, "#1D4ED8") for a, n in years])
        if years:
            self.years_head.title_label.setText(f"{st['releves']:,} relevés · {years[0][0][:4]} → {years[-1][0][-4:]}"
                                                .replace(",", " "))
        last = self.db.get_meta("derniere_indexation")
        self.last.set(f"Dernière indexation : {last}" if last else "Aucune indexation", "blue" if last else "gray")
        self.refresh_log()

    def refresh_log(self) -> None:
        rows = self.db.anomalies(self._filter, 500)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            tone, _ = LEVELS.get(r["niveau"], ("gray", "info"))
            self.table.setCellWidget(i, 0, _wrap(Badge(r["niveau"], tone)))
            it = QTableWidgetItem(_short(r["chemin"]))
            it.setToolTip(r["chemin"] or "")
            it.setFont(QFont("Consolas", 9))
            self.table.setItem(i, 1, it)
            self.table.setItem(i, 2, QTableWidgetItem(r["message"]))
            self.table.setItem(i, 3, QTableWidgetItem((r["horodatage"] or "")[5:16]))
            if r["chemin"]:
                b = QPushButton(r["action"] or "Ouvrir le dossier")
                b.setObjectName("secondary")
                b.clicked.connect(lambda _=False, p=r["chemin"]: _reveal(p))
                self.table.setCellWidget(i, 4, _wrap(b))

    # ---------------------------------------------------------------- actions
    def browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Dossier racine des archives", self.src.text() or str(Path.home()))
        if d:
            self.src.setText(d)

    def toggle_run(self) -> None:
        if self.proc and self.proc.state() != QProcess.ProcessState.NotRunning:
            # Arrêt propre : les fichiers en cours se terminent, les instances de Word sont fermées.
            if not self._stopping:
                self._stopping = True
                self.proc.write(b"stop\n")
                self.status.set("ARRÊT…", "amber")
                self.detail.setText("Arrêt demandé : fin des fichiers en cours… (cliquer encore pour forcer)")
                QTimer.singleShot(20000, self._force_stop)
            else:
                self.proc.kill()
            return
        src = self.src.text().strip()
        if not src or not Path(src).exists():
            self.toast.show_message("Dossier source introuvable : choisissez le dossier racine des archives.", "red")
            return
        self.s.source_root = src
        save_settings(self.s)
        program, args = indexer_command()
        args += ["--index", "--json", "--stdin-control", "--source", src, "--db", str(self.s.resolved_db_path()),
                 "--only-new" if self.s.differential else "--full"]
        self._stopping = False
        if not self.s.ocr_enabled:
            args.append("--no-ocr")
        self.proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        self.proc.setProcessEnvironment(env)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._finished)
        self._buf = b""
        self.proc.start(program, args)
        self.status.set("EN COURS", "blue")
        self.run_btn.setIcon(icon("stop", "#FFFFFF", 20))
        self.run_btn.setToolTip("Arrêter l'indexation")
        for sc in self.stages:
            sc.badge.set("En cours", "blue")
        self.detail.setText("Analyse de l'arborescence…")

    def _read(self) -> None:
        self._buf += bytes(self.proc.readAllStandardOutput())
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            try:
                ev = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            self._event(ev)

    def _event(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "start":
            self.total = ev["total"]
            self.stages[0].value.setText(str(ev["total"]))
            self.stages[0].badge.set("Terminé", "green")
            bits = [f"{ev.get('a_traiter', ev['total'])} fichier(s) à lire", f"{ev.get('workers', 1)} processus"]
            if ev.get("exclus"):
                bits.append(f"{ev['exclus']} élément(s) exclu(s)")
            if ev.get("retires"):
                bits.append(f"{ev['retires']} fichier(s) exclu(s) retiré(s) de l'index")
            self.current.setText(" · ".join(bits))
        elif t == "progress":
            done, total = ev["done"], max(1, ev["total"])
            pct = done * 100 // total
            self.pct.setText(f"{pct} %")
            self.bar.setValue(pct)
            c = ev["counters"]
            eta = ""
            if ev.get("eta_s"):
                e = int(ev["eta_s"])
                eta = f" · restant ≈ {e // 3600} h {e % 3600 // 60:02d} min" if e >= 3600 else f" · restant ≈ {e // 60} min {e % 60} s"
            rate = f" · {ev['rate']:.1f} fichiers/s" if ev.get("rate") else ""
            self.detail.setText(f"{done} / {total} fichiers · {c['ignores']} inchangé(s) · {c['echecs']} en échec{rate}{eta}")
            if ev.get("file"):
                self.current.setText(f"Dernier fichier lu : {_short(ev['file'], 90)}")
            self.stages[1].value.setText(str(c["extraits"]))
            self.stages[2].value.setText(str(c["blocs"]))
            self.stages[3].value.setText(str(c["ecrits"]))
            self.progress.emit(done, total, True)
        elif t == "stage":
            label = {"ocr": f"OCR de {ev.get('pages')} page(s)", "conversion": f"Conversion → PDF ({ev.get('backend')})"}
            self.current.setText(f"{label.get(ev['stage'], ev['stage'])} : {_short(ev['file'], 80)}")
        elif t == "warning":
            self.toast.show_message(ev["message"], "amber")
        elif t == "done":
            for sc in self.stages:
                sc.badge.set("Terminé", "green")
            self.detail.setText(f"Terminé en {ev['duree_s']} s · {ev['ecrits']} relevé(s) écrit(s) · "
                                f"{ev['ignores']} inchangé(s) · {ev['echecs']} en échec")

    def _force_stop(self) -> None:
        if self._stopping and self.proc and self.proc.state() != QProcess.ProcessState.NotRunning:
            self.proc.kill()

    def _finished(self, code, status) -> None:
        self._read()
        was_stopping, self._stopping = self._stopping, False
        stopped = status == QProcess.ExitStatus.CrashExit or code != 0 or was_stopping
        if stopped:
            self.status.set("ARRÊTÉ", "amber")
            err = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace").strip().splitlines()
            msg = "Indexation arrêtée. Relancez en mode différentiel pour reprendre."
            if err and code not in (0, 9, 15) and status != QProcess.ExitStatus.CrashExit:
                msg = f"Le robot s'est arrêté : {err[-1][:200]}"
            self.toast.show_message(msg, "amber")
            for sc in self.stages:
                if sc.badge.text().endswith("En cours"):
                    sc.badge.set("Interrompu", "amber")
        else:
            self.status.set("TERMINÉ", "green")
            self.pct.setText("100 %")
            self.bar.setValue(100)
        self.run_btn.setIcon(icon("play", "#FFFFFF", 20))
        self.run_btn.setToolTip("Lancer l'indexation")
        self.progress.emit(0, 0, False)
        self.refresh()
        self.finished.emit()

    def check_integrity(self) -> None:
        missing = [r["chemin"] for r in self.db.orphan_files()]
        for chemin in missing:
            self.db.add_anomaly("AVERTISSEMENT", chemin, "Lien orphelin : fichier déplacé, renommé ou supprimé.", "Relocaliser")
        if missing:
            self.toast.show_message(f"{len(missing)} lien(s) orphelin(s) trouvé(s). Relancez une indexation différentielle "
                                    "sur le nouveau dossier : seuls les fichiers déplacés seront relus.", "amber")
        else:
            self.toast.show_message("Intégrité vérifiée : tous les fichiers indexés sont présents.")
        self.refresh_log()

    def is_running(self) -> bool:
        return bool(self.proc and self.proc.state() != QProcess.ProcessState.NotRunning)


def _short(path: str | None, n: int = 70) -> str:
    if not path:
        return ""
    return path if len(path) <= n else "…" + path[-(n - 1):]


def _wrap(w: QWidget) -> QWidget:
    holder = QWidget()
    lay = QHBoxLayout(holder)
    lay.setContentsMargins(6, 4, 6, 4)
    lay.addWidget(w)
    lay.addStretch(1)
    return holder


def _reveal(path: str) -> None:
    p = Path(path)
    if sys.platform == "win32" and p.exists():
        subprocess.Popen(["explorer", "/select,", str(p)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent if p.parent.exists() else Path.home())))

