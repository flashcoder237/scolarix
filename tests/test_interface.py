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
    monkeypatch.setattr(Settings, "export_dir", lambda self, nature="Releves": tmp_path)
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
    d = window.db.deliveries("18M042")[0]                  # numéro de délivrance + empreinte enregistrés
    assert d["reference"].endswith(f"/{__import__('datetime').date.today().year}") and d["sha256"]


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
    dlg.export()                                   # fusion en arrière-plan
    assert wait_for(app, lambda: list(tmp_path.glob("FMSP_18M042_*_Cursus_*.pdf")) and not dlg._busy, 120)
    out = list(tmp_path.glob("FMSP_18M042_*_Cursus_*.pdf"))
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
    assert "Times New Roman" in rules.table.item(1, 6).text()
    assert rules.sample_fmt.font().family() == "Times New Roman" and rules.sample_fmt.font().bold()
    rules.table.setCurrentCell(0, 1)
    assert rules.font.currentText() == "(police d'origine)"          # chaque règle garde sa mise en forme
    rules.add_rule()
    rules.table.item(rules.table.rowCount() - 1, 2).setText("(")
    rules.table.item(rules.table.rowCount() - 1, 5).setCheckState(Qt.CheckState.Checked)
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


def test_demandes_au_guichet(app, window, tmp_path, monkeypatch):
    """Une personne demande plusieurs relevés : demande, ajout (Ctrl+D), préparation, impression, remise."""
    from PySide6.QtWidgets import QInputDialog

    import ui.demandes_ui as dui
    window.s.exports_root = str(tmp_path / "Exports")
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Le père de l'étudiant", True)))
    printed = []
    monkeypatch.setattr(dui, "print_pdf", lambda path, s: printed.append(path) or "test")
    g = window.guichet
    g.run_search("18M042")
    assert wait_for(app, lambda: g.model.rowCount() >= 3)
    for row in (0, 1, 2):
        g.on_row(row)
        g.add_to_demande()                                   # 1er ajout : crée la demande
    bar = g.demande_bar
    did = bar.active
    assert did and window.db.demande(did)["n"] == 3 and window.db.demande(did)["demandeur"] == "Le père de l'étudiant"
    g.on_row(0)
    g.add_to_demande()
    assert window.db.demande(did)["n"] == 3                  # pas de doublon
    assert sum("✓ DEMANDE" in r.flags for r in g.model.rows) == 3
    dlg = dui.DemandeDialog(window.db, window.s, did, window, previews=window.previews)
    dlg.print_all()                                          # prépare d'abord, puis imprime
    assert wait_for(app, lambda: window.db.demande(did)["statut"] == "IMPRIMÉE", 120)
    assert wait_for(app, lambda: len(printed) == 2, 30)      # bordereau + A_IMPRIMER_TOUT
    folder = window.db.demande(did)["dossier"]
    assert "Demandes" in folder and window.db.demande(did)["numero"] in folder
    dlg.mark_done()
    assert window.db.demande(did)["statut"] == "REMISE"
    page = window.demandes
    page.seg.set_value("remises")
    page.refresh()
    assert page.table.rowCount() >= 1 and page.table.item(0, 5).text() == "REMISE"
    page.table.selectRow(0)
    page.activate_selected()
    assert window.stack.currentIndex() == 0
    dlg.deleteLater()


def test_finitions_polices_sons_animations(app, window):
    from PySide6.QtWidgets import QLabel

    from ui import effects
    from ui.app import resource_path
    fams = effects.load_fonts(resource_path("assets"))
    assert {"Inter", "JetBrains Mono"} <= fams and effects.has_font("Inter Display")
    from ui.widgets import mono_font, title
    assert mono_font().families()[0] == "JetBrains Mono"
    assert title("Grand titre", 22).font().family() == "Inter Display"
    lab = QLabel("0")
    effects.count_to(lab, 42)
    assert wait_for(app, lambda: lab.text() == "42", 5)
    window.s.ui_animations = False
    effects.configure(window.s)
    effects.count_to(lab, 7)
    assert lab.text() == "7"                                   # sans animation : immédiat
    window.s.ui_animations = True
    effects.configure(window.s)
    effects.play("green")                                      # sons coupés pendant les tests : sans effet
    window.guichet.preview.toast.show_message("Essai", "green", sound="impression")
    assert window.guichet.preview.toast.isVisible()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton
    btn = window.findChild(QPushButton)
    assert btn.cursor().shape() == Qt.CursorShape.PointingHandCursor or not btn.isVisible()


# ------------------------------------------------------------------------------------------------
# Constats de la simulation d'usage au guichet (1366×768) — chacun verrouillé par un test.

def _inside(window, w) -> bool:
    """Le widget est entièrement visible dans la fenêtre (pas sous la ligne de flottaison)."""
    from PySide6.QtCore import QPoint
    top = w.mapTo(window, QPoint(0, 0))
    return w.isVisible() and top.y() >= 0 and top.y() + w.height() <= window.height() and top.x() + w.width() <= window.width()


def test_ecran_bas_document_et_imprimer_visibles(app, window):
    g = window.guichet
    app.processEvents()
    assert g.compact and not g.stats_box.isVisible() and g.mini.isVisible()
    g.run_search("18M042")
    assert wait_for(app, lambda: g.model.rowCount() > 0)
    app.processEvents()
    assert _inside(window, g.preview.btn_print)               # bouton Imprimer sans défiler
    assert _inside(window, g.preview.viewer) or g.preview.viewer.height() >= 140
    assert g.preview.line.isVisible() and "18M042" in g.preview.line.text()
    window.resize(1920, 1080)                                # en-tête complet, aperçu resserré
    assert wait_for(app, lambda: not g.compact, 5)
    app.processEvents()
    assert g.stats_box.isVisible() and g.preview.compact and _inside(window, g.preview.btn_print)
    assert g.preview.viewer.height() >= 250                  # le document reste lisible en 1080p
    window.resize(2560, 1440)                                # grand écran : tuiles et grandes actions
    assert wait_for(app, lambda: not g.preview.compact, 5)
    assert all(t.isVisible() for t in g.preview._tile_frames)
    window.resize(1366, 768)
    assert wait_for(app, lambda: g.compact, 5)


def test_message_flottant_toujours_visible(app, window):
    g = window.guichet
    g.run_search("18M042")
    assert wait_for(app, lambda: g.model.rowCount() > 0)
    g.export_current()
    app.processEvents()
    assert g.preview.toast.isVisible() and _inside(window, g.preview.toast)
    assert "Exporté" in g.preview.toast.lab.text()


def test_etat_apercu_word_ne_colle_pas_au_releve_suivant(app, window):
    """↓ sur un relevé Word (aperçu en préparation) puis sur un PDF : Ctrl+P ne doit pas être refusé."""
    g = window.guichet
    g.run_search("18M042")
    assert wait_for(app, lambda: g.model.rowCount() > 0)
    pdf = next(i for i, r in enumerate(g.model.rows) if not r.is_word)
    g.preview.render_state = "en cours"                      # comme si un aperçu Word tournait encore
    g.on_row(pdf)
    assert g.preview.render_state == ""
    assert g._ready(g.current) is not None


def test_impression_programmee_pendant_apercu_word(app, window, monkeypatch):
    g = window.guichet
    g.run_search("18M042")
    assert wait_for(app, lambda: g.model.rowCount() > 0)
    g.on_row(0)
    rel = g.current
    state = {"needs": True}
    monkeypatch.setattr(window.previews, "needs_render", lambda r: state["needs"])
    monkeypatch.setattr(g.preview, "_render_async", lambda r: setattr(g.preview, "render_state", "en cours"))
    calls = []
    monkeypatch.setattr(g, "print_current", lambda: calls.append("print"))
    GuichetPage = type(g)
    GuichetPage.print_current(g)                             # Ctrl+P trop tôt
    assert g._pending == ("print", rel.id) and "automatiquement" in g.preview.toast.lab.text()
    state["needs"] = False
    g._on_releve_updated(rel)                                # l'aperçu est prêt
    assert wait_for(app, lambda: calls == ["print"], 5) and g._pending is None
    # changer de relevé annule l'impression programmée
    g._pending = ("print", rel.id)
    other = next(i for i, r in enumerate(g.model.rows) if r.id != rel.id)
    g.on_row(other)
    assert g._pending is None and "annulée" in g.preview.toast.lab.text()


def test_ctrl_d_sur_un_autre_etudiant_propose_une_nouvelle_demande(app, window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    import ui.widgets as W
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Lui-même", True)))
    g = window.guichet
    g.run_search("18P203")
    assert wait_for(app, lambda: g.model.rowCount() > 0)
    g.on_row(0)
    g.add_to_demande()
    first = g.demande_bar.active
    assert window.db.demande(first)["matricule"] == "18P203"
    g.run_search("19P017")                                   # une autre personne
    assert wait_for(app, lambda: g.model.rowCount() > 0)
    g.on_row(0)
    asked = []
    monkeypatch.setattr(W, "ask_choice", lambda *a, **k: asked.append(a[2]) or -1)
    g.add_to_demande()                                       # Annuler : rien n'est ajouté nulle part
    assert asked and window.db.demande(first)["n"] == 1 and g.demande_bar.active == first
    monkeypatch.setattr(W, "ask_choice", lambda *a, **k: 0)  # « Nouvelle demande pour … »
    g.add_to_demande()
    second = g.demande_bar.active
    assert second != first and window.db.demande(second)["matricule"] == "19P017"
    assert window.db.demande(first)["n"] == 1                # la demande de la 1re personne est intacte
    g.demande_bar.set_active(first)
    monkeypatch.setattr(W, "ask_choice", lambda *a, **k: 1)  # « Ajouter quand même » : choix explicite
    g.add_to_demande()
    assert window.db.demande(first)["n"] == 2 and "Attention" in g.preview.toast.lab.text()


def test_meme_personne_autre_filiere():
    from ui.demandes_ui import same_person
    assert same_person("TCHOUA Alain", "Alain TCHOUA") and same_person("TCHOUA Alain", "TCHOUA ALAIN Junior")
    assert not same_person("KOUAME Estelle", "TCHOUA Marie-Claire") and not same_person(None, "X")


def test_etats_vides_guident_l_agent(app, window):
    g = window.guichet
    g.run_search("zzqxw")
    assert wait_for(app, lambda: g.stack.currentWidget() is g.empty)
    assert "zzqxw" in g.empty_title.text() and "Pistes" in g.empty_text.text()
    g.run_search("18M042")
    assert wait_for(app, lambda: g.model.rowCount() > 0) and g.stack.currentWidget() is g.table
    g.set_filiere("P")                                       # rien en Pharmacie pour ce matricule
    assert g.stack.currentWidget() is g.empty and g.empty_btn.isVisible() and "toutes les filières" in g.empty_btn.text()
    g.empty_btn.click()
    assert g.filiere is None and g.model.rowCount() > 0 and g.stack.currentWidget() is g.table


def test_navigation_rafraichit_la_bonne_page(app, window, monkeypatch):
    calls = []
    monkeypatch.setattr(window.extraction, "reload_years", lambda: calls.append("extraction"))
    monkeypatch.setattr(window.indexing, "refresh", lambda: calls.append("indexation"))
    window.go(window.stack.indexOf(window.extraction))
    window.go(window.stack.indexOf(window.indexing))
    window.go(window.stack.indexOf(window.demandes))
    assert calls == ["extraction", "indexation"]
    created = []
    monkeypatch.setattr(window.demandes, "create", lambda: created.append(1))
    window._new_demande()                                    # Ctrl+N sur l'écran Demandes
    assert created == [1]
    window.go(0)


def test_dialogue_demande_bouton_principal_et_annulation(app, window, monkeypatch):
    import ui.demandes_ui as dui
    import ui.widgets as W
    did = window.db.create_demande("T", "test", "18M042", "TCHOUA Alain", "Lui-même")
    rel = window.db.cursus("18M042")[0]
    window.db.add_demande_item(did, rel.id)
    dlg = dui.DemandeDialog(window.db, window.s, did, window)
    assert dlg.btn_print.objectName() == "primary" and dlg.btn_prepare.objectName() == "ghost"
    monkeypatch.setattr(W, "ask_choice", lambda *a, **k: -1)
    dlg.cancel()                                             # un clic malheureux ne suffit plus
    assert window.db.demande(did)["statut"] == "EN COURS"
    monkeypatch.setattr(W, "ask_choice", lambda *a, **k: 0)
    dlg.cancel()
    assert window.db.demande(did)["statut"] == "ANNULÉE"
    dlg.deleteLater()


def test_impression_d_une_demande_journalisee_par_releve(app, window, tmp_path, monkeypatch):
    import ui.demandes_ui as dui
    window.s.exports_root = str(tmp_path / "Exports")
    monkeypatch.setattr(dui, "print_pdf", lambda path, s: "test")
    did = window.db.create_demande("T", "test", "18M042", "TCHOUA Alain", "Lui-même")
    rels = [r for r in window.db.cursus("18M042") if not r.is_word][:2]
    for r in rels:
        window.db.add_demande_item(did, r.id)
    numero = window.db.demande(did)["numero"]
    dlg = dui.DemandeDialog(window.db, window.s, did, window, previews=window.previews)
    dlg.print_all()
    assert wait_for(app, lambda: window.db.demande(did)["statut"] == "IMPRIMÉE", 120)
    lines = [j for j in window.db.journal("18M042") if j["action"] == "IMPRESSION" and numero in (j["detail"] or "")]
    assert len(lines) == len(rels)                            # compte dans « Imprimés aujourd'hui »
    dlg.deleteLater()


def test_verification_retrouve_l_original_toute_seule(app, window, tmp_path, monkeypatch):
    g = window.guichet
    g.run_search("18M042")
    assert wait_for(app, lambda: g.model.rowCount() > 0)
    g.on_row(next(i for i, r in enumerate(g.model.rows) if not r.is_word))
    g.export_current()
    exported = g.last_export
    ver = window.verification
    import ui.verification_page as vp
    monkeypatch.setattr(vp.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(exported), "")))
    ver.matricule.clear()
    ver.original = None
    ver.load_submitted()                                     # aucun matricule saisi
    assert wait_for(app, lambda: bool(ver.rows), 60)
    assert ver.matricule.text() == "18M042" and ver.original.id == g.current.id
    assert "conforme" in ver.v_title.text().lower()
