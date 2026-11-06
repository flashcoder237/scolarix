"""Tests de fumée de l'interface (Qt hors écran) : chaque écran se construit et réagit sans erreur."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QModelIndex, QRect, Qt  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem  # noqa: E402

from conftest import HAS_CONVERTER  # noqa: E402

from core.config import DEFAULT_TEXT_RULES  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, cond, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture()
def window(app, indexed, tmp_path, monkeypatch):
    from core.config import Settings
    from ui.main_window import MainWindow
    db, settings = indexed[0], indexed[1]
    monkeypatch.setattr(Settings, "export_dir", lambda self: tmp_path)
    monkeypatch.setattr("ui.settings_dialog.save_settings", lambda s: None)
    monkeypatch.setattr("ui.indexing_page.save_settings", lambda s: None)
    win = MainWindow(db, settings)
    win.resize(1366, 768)
    win.show()
    app.processEvents()
    yield win
    win.previews.close()
    win.hide()
    win.deleteLater()


def test_guichet_recherche_apercu_export(app, window, tmp_path):
    g = window.guichet
    g.run_search("18M042")
    assert wait_for(app, lambda: g.model.rowCount() > 0)
    keys = [(r.niveau_num, r.semestre or 0) for r in g.model.rows]
    assert keys == sorted(keys)                              # chronologique
    g.on_row(0)
    assert g.preview.rel is not None and g.preview.rel.matricule == "18M042"
    assert g.preview.btn_print.isEnabled()
    g.export_current()
    assert list(tmp_path.glob("FMSP_18M042_*.pdf"))
    assert window.db.journal(matricule="18M042")


def test_guichet_releve_word_apercu_a_la_demande(app, window):
    g = window.guichet
    g.run_search("tchoua")
    assert wait_for(app, lambda: g.model.rowCount() > 0)
    word = [i for i, r in enumerate(g.model.rows) if r.is_word]
    if not word:
        pytest.skip("pas de relevé Word dans les résultats")
    needs = window.previews.needs_render(g.model.rows[word[0]])
    g.on_row(word[0])
    if not needs:
        return
    assert wait_for(app, lambda: g.preview.render_state in ("prêt", "échec"), 90)
    if HAS_CONVERTER:
        assert g.preview.render_state == "prêt" and g.preview.rel.preview_pdf and g.preview.btn_print.isEnabled()
    else:   # poste sans Word ni LibreOffice : message clair, impression bloquée
        assert g.preview.render_state == "échec" and not g.preview.btn_print.isEnabled()


def test_guichet_vue_groupee_et_zero_resultat(app, window):
    g = window.guichet
    g.run_search("18M042")
    wait_for(app, lambda: g.model.rowCount() > 0)
    g.set_view("groupe")
    top = g.tree.topLevelItem(0)
    assert top is not None and ("manquant" in top.text(3) or "Tous présents" in top.text(3))
    g.run_search("zzzzzzzz")
    wait_for(app, lambda: g.model.rowCount() == 0)
    assert g.preview.details.text() == ""                    # pas de détails périmés


def test_delegue_de_rendu_noms_longs(app, window):
    from ui.results_table_model import RowDelegate
    g = window.guichet
    g.run_search("tchoua")
    wait_for(app, lambda: g.model.rowCount() > 0)
    rel = g.model.rows[0]
    rel.nom = "NOM EXTREMEMENT LONG QUI NE TIENT PAS DANS LA COLONNE DU TABLEAU"
    img = QImage(400, 60, QImage.Format.Format_ARGB32)
    img.fill(0xFFFFFFFF)
    p = QPainter(img)
    opt = QStyleOptionViewItem()
    opt.rect = QRect(0, 0, 160, 52)
    for col in range(g.model.columnCount()):
        RowDelegate().paint(p, opt, g.model.index(0, col, QModelIndex()))
    p.end()
    # rien n'est dessiné à droite de la cellule (découpage)
    assert all(img.pixelColor(x, 26).name() in ("#ffffff", "#fafbfc", "#eef4ff") for x in range(200, 400, 7))
    assert rel.nom_complet in g.model.data(g.model.index(0, 1), Qt.ItemDataRole.ToolTipRole)


def test_cursus_dialog(app, window, tmp_path):
    from ui.cursus_dialog import CursusDialog
    dlg = CursusDialog(window.db, window.s, "18M042", window, previews=window.previews)
    assert dlg.tree.topLevelItemCount() >= 6
    dlg.export()
    out = list(tmp_path.glob("FMSP_18M042_*_Cursus.pdf"))
    assert out and out[0].stat().st_size > 1000
    dlg.deleteLater()


def test_reglages_regles_et_exclusions(app, window):
    from ui.settings_dialog import ExclusionsDialog, SettingsDialog
    dlg = SettingsDialog(window.s, window)
    rules = dlg.rules
    assert rules.table.rowCount() == len(DEFAULT_TEXT_RULES)
    rules.preview()
    assert "3 remplacement" in rules.result.text() and "Ref N°" in rules.result.text()
    # mise en forme : police, taille, gras, italique de la règle sélectionnée
    rules.table.setCurrentCell(1, 1)
    rules.font.setCurrentText("Times New Roman")
    rules.size.setValue(9.5)
    rules.bold.setCurrentIndex(rules.bold.findData(True))
    rules.italic.setCurrentIndex(rules.italic.findData(False))
    r1 = rules.rules()[1]
    assert (r1["police"], r1["taille"], r1["gras"], r1["italique"]) == ("Times New Roman", 9.5, True, False)
    assert "Times New Roman" in rules.table.item(1, 5).text()
    assert rules.sample_fmt.font().family() == "Times New Roman" and rules.sample_fmt.font().bold()
    rules.table.setCurrentCell(0, 1)
    assert rules.font.currentText() == "(police d'origine)"          # chaque règle garde sa mise en forme
    rules.add_rule()
    rules.table.item(rules.table.rowCount() - 1, 2).setText("(")
    rules.table.item(rules.table.rowCount() - 1, 4).setCheckState(Qt.CheckState.Checked)
    assert rules.validate() is not None                       # expression invalide refusée
    dlg.save()
    assert dlg.result() == 0                                  # non enregistré
    rules.remove_rule() if rules.table.selectedIndexes() else rules.table.removeRow(rules.table.rowCount() - 1)
    dlg.exclusions._add("*DIPLOME*")
    dlg.workers.setValue(3)
    dlg.save()
    assert window.s.exclusions == ["*DIPLOME*"] and window.s.index_workers == 3
    assert len(window.s.text_rules) == len(DEFAULT_TEXT_RULES)
    ex = ExclusionsDialog(window.s, window)
    assert ex.editor.items() == ["*DIPLOME*"]
    window.s.exclusions, window.s.index_workers = [], 0


def test_page_indexation_evenements(app, window):
    page = window.indexing
    page._event({"type": "start", "total": 100, "a_traiter": 80, "workers": 3, "exclus": 2, "retires": 1})
    assert "3 processus" in page.current.text() and "exclu" in page.current.text()
    page._event({"type": "progress", "done": 50, "total": 100, "file": "x.pdf", "eta_s": 4000, "rate": 2.5,
                 "counters": {"extraits": 30, "blocs": 40, "ecrits": 40, "ignores": 20, "echecs": 0}})
    assert page.pct.text() == "50 %" and "2.5 fichiers/s" in page.detail.text() and "h" in page.detail.text()
    page._event({"type": "done", "duree_s": 12.0, "ecrits": 40, "ignores": 20, "echecs": 0})
    assert "Terminé" in page.detail.text()
    page._update_excl_btn()
    assert "Exclure" in page.excl_btn.text() or "Exclusions" in page.excl_btn.text()


def test_extraction_et_verification(app, window, tmp_path):
    ex = window.extraction
    ex.run()
    assert ex.rows
    import ui.extraction_page as ep
    ep.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(tmp_path / "extraction.xlsx"), ""))
    ex.export("xlsx")
    assert list(tmp_path.glob("*.xlsx"))
    ver = window.verification
    ver.matricule.setText("18M042") if hasattr(ver, "matricule") else None
    if hasattr(ver, "find_original"):
        ver.find_original()
        assert ver.original is None or ver.original.matricule == "18M042"
