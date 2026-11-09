"""Fusion du cursus et préparation des demandes avec des relevés Word (données fictives).

Reproduit le blocage observé au guichet (Scolarix 2.0.1 : « Fusionner et exporter » tournait plus de 10 min) :
plusieurs relevés d'une même compilation Word, un Word qui ne répond plus, des aperçus du guichet en file.
Aucun Word réel n'est nécessaire : le convertisseur est remplacé (monkeypatch).
"""
from __future__ import annotations

import threading
import time

import pytest
from conftest import make_settings
from docx import Document
from test_preview import FakeConverter
from test_text_rules import make_releve_docx

import core.extractors as extractors
import core.preview as preview
from core.database import Database
from core.extractors import WordConverter, docx_pages_text
from core.indexer import Indexer
from core.service import Service, ServiceError

MAT = "99EM201"          # étudiant fictif dont on fusionne le cursus
AUTRE = "99EM777"        # autre étudiant fictif, présent seulement dans la compilation


def _add_releve(doc, matricule, nom, niveau, semestre, annee):
    doc.add_paragraph(f"NOM ET PRENOM: {nom}                MATRICULE : {matricule}")
    doc.add_paragraph("NÉ(E) LE : 29/03/1990          A : DOUALA")
    doc.add_paragraph(f"NIVEAU: {niveau}          ANNÉE ACADÉMIQUE: {annee}          FILIÈRE: MEDECINE")
    doc.add_paragraph(f"CYCLE : LICENCE          SEMESTRE: {semestre}")
    t = doc.add_table(rows=1, cols=6)
    for i, v in enumerate(("19", "IMMUNOPATHOLOGIE", "14,50", "B+", "4", "VALIDEE")):
        t.cell(0, i).text = v


def compilation(path, blocs):
    """Fichier collectif : plusieurs relevés (plusieurs étudiants), un par page."""
    make_releve_docx(path, *blocs[0][:2])
    doc = Document(str(path))
    body = doc.element.body
    for el in list(body):                        # on garde l'en-tête, on refait le corps
        if el.tag.endswith("}p") or el.tag.endswith("}tbl"):
            body.remove(el)
    for k, bloc in enumerate(blocs):
        if k:
            doc.add_page_break()
        _add_releve(doc, *bloc)
    doc.save(str(path))
    return path


@pytest.fixture()
def fonds(tmp_path, data_dir, monkeypatch):
    """3 fichiers Word : une compilation (3 relevés de MAT + 2 d'un autre étudiant) et deux fichiers d'un relevé."""
    archives = tmp_path / "archives"
    archives.mkdir()
    compilation(archives / "EM1 compil.docx", [
        (MAT, "ETUDIANT FICTIF", "I", "I", "2010 - 2011"),
        (AUTRE, "AUTRE FICTIF", "I", "I", "2010 - 2011"),
        (MAT, "ETUDIANT FICTIF", "I", "II", "2010 - 2011"),
        (AUTRE, "AUTRE FICTIF", "I", "II", "2010 - 2011"),   # deux pages du même étudiant seraient jumelées
        (MAT, "ETUDIANT FICTIF", "II", "III", "2011 - 2012"),
    ])
    compilation(archives / "EM4 S1.docx", [(MAT, "ETUDIANT FICTIF", "II", "IV", "2011 - 2012")])
    compilation(archives / "EM5 S1.docx", [(MAT, "ETUDIANT FICTIF", "III", "V", "2012 - 2013")])
    settings = make_settings(tmp_path, exports_root=str(tmp_path / "Exports"))
    db = Database(settings.db_path, cache_dir=settings.resolved_cache_dir())
    Indexer(settings, db).run(archives, only_new=False, workers=1)
    FakeConverter.calls = []
    monkeypatch.setattr(preview, "WordConverter", FakeConverter)
    svc = preview.PreviewService(db, settings)
    service = Service(settings, svc)
    events: list[dict] = []
    service.emit = lambda ev, data: events.append(dict(data, event=ev))
    yield service, svc, db, events
    svc.close()


def word_ids(service):
    rels = [r for r in service.db.cursus(MAT) if r.is_word]
    assert len(rels) == 5, [(r.matricule, r.periode) for r in rels]
    return [r.id for r in rels]


def files_converted():
    return sorted({c[2] or "" for c in FakeConverter.calls})


# ------------------------------------------------------------------ une conversion par fichier + progression
def test_une_seule_conversion_par_fichier_et_progression(fonds):
    service, _svc, _db, events = fonds
    out = service.cursus_build(MAT, word_ids(service), cover=True, op="op-1")
    assert out["missing"] == [] and out["pages"] == 1 + 5
    # 5 relevés dans 3 fichiers : 3 conversions, pas 5 (la compilation n'est convertie qu'une fois)
    assert len(FakeConverter.calls) == 3
    conv = [e for e in events if e["phase"] == "conversion"]
    assert [(e["i"], e["n"]) for e in conv] == [(1, 3), (2, 3), (3, 3)]
    assert all(e["op"] == "op-1" and e["event"] == "progress" for e in events)
    assert any("EM4 S1.docx" in e["message"] and e["message"].startswith("Conversion ") for e in conv)
    assert events[-1]["phase"] == "fusion"
    # deuxième fusion : tout est en cache, aucune conversion
    FakeConverter.calls = []
    events.clear()
    assert service.cursus_build(MAT, word_ids(service), op="op-2")["missing"] == []
    assert FakeConverter.calls == [] and not [e for e in events if e["phase"] == "conversion"]


def test_demande_une_conversion_par_fichier(fonds):
    service, _svc, db, events = fonds
    did = service.demande_create(matricule=MAT, nom="ETUDIANT FICTIF", demandeur="test")["demande"]["id"]
    for rid in word_ids(service):
        service.demande_add(did, rid)
    res = service.demande_prepare(did, op="dem-1")
    assert len(res["prets"]) == 5 and res["bloques"] == []
    assert len(FakeConverter.calls) == 3
    assert [e["i"] for e in events if e["phase"] == "conversion"] == [1, 2, 3]


# ------------------------------------------------------------------ Word bloqué : écarté avec motif, sans attente
class FakeWord:
    """Objet COM factice : Documents.Open bloque (boîte de dialogue invisible) sur la compilation, jusqu'à ce que
    le processus soit arrêté ; les autres fichiers sont convertis normalement."""

    def __init__(self, killed: threading.Event):
        self.killed = killed
        self.Documents = self

    def Open(self, path, **kw):
        assert kw["ReadOnly"] is True and kw["AddToRecentFiles"] is False and kw["ConfirmConversions"] is False
        assert kw["OpenAndRepair"] is False and "document" in path      # jamais l'original : une copie neutre
        if any(AUTRE in p.text for p in docx_pages_text(extractors.Path(path))):
            self.killed.wait(30)                                         # bloqué jusqu'à l'arrêt de WINWORD
            raise RuntimeError(-2147023174, "Le serveur RPC n'est pas disponible.")
        return FakeDoc(path)


class FakeDoc:
    def __init__(self, path):
        self.path = path

    def ExportAsFixedFormat(self, OutputFileName, ExportFormat, **kw):
        assert ExportFormat == 17 and kw["OpenAfterExport"] is False
        FakeConverter().convert(extractors.Path(self.path), extractors.Path(OutputFileName))

    def Close(self, SaveChanges=0):
        pass


def test_word_bloque_est_arrete_et_le_releve_ecarte(fonds, monkeypatch):
    service, svc, _db, _events = fonds
    killed, kills = threading.Event(), []

    class HangingWord(WordConverter):
        def __init__(self, settings):
            super().__init__(settings, timeout=0.5)
            self._com_ok, self._soffice = True, None

        def _word_app(self):
            self.word_pid = 4242                       # PID du Word lancé par Scolarix (et lui seul)
            return FakeWord(killed)

    def fake_kill(pid):
        kills.append(pid)
        killed.set()
    monkeypatch.setattr(preview, "WordConverter", HangingWord)
    monkeypatch.setattr(extractors, "kill_pid", fake_kill)
    t0 = time.monotonic()
    out = service.cursus_build(MAT, word_ids(service), op="op-hang")
    assert time.monotonic() - t0 < 15                  # plus jamais 15 min d'attente
    assert kills == [4242]                             # un seul arrêt, et seulement NOTRE Word
    assert out["pages"] == 1 + 2                       # les 2 fichiers sains sont fusionnés
    assert len(out["missing"]) == 3 and all("ne répondait plus" in m for m in out["missing"])


def test_fil_word_irrecuperable_remplace(fonds, monkeypatch):
    """Filet de sécurité : Word n'a pas pu être arrêté (PID inconnu) ; le fil bloqué est abandonné, le suivant
    travaille normalement."""
    service, svc, db, _events = fonds
    release = threading.Event()

    class Stuck(FakeConverter):
        def convert(self, src, dest, fmt="pdf", label=None):
            if label == "EM1 compil.docx":
                release.wait(30)
            super().convert(src, dest, fmt, label)
    monkeypatch.setattr(preview, "WordConverter", Stuck)
    monkeypatch.setattr(preview.PreviewService, "job_timeout", property(lambda self: 0.5))
    rels = {r.chemin.rsplit("/", 1)[-1]: r for r in db.cursus(MAT) if r.is_word}
    try:
        with pytest.raises(extractors.ExtractionError, match="ne répond plus"):
            svc.ensure(rels["EM1 compil.docx"])
        ok = svc.ensure(rels["EM4 S1.docx"])            # nouveau fil Word : pas bloqué derrière l'ancien
        assert ok.preview_pdf and ok.page_verifiee
    finally:
        release.set()


# ------------------------------------------------------------------ annulation
def test_annulation_arrete_entre_deux_fichiers(fonds):
    service, _svc, db, events = fonds
    before = len(db.deliveries())

    def emit(ev, data):
        events.append(data)
        if data["phase"] == "conversion" and data["i"] == 1:
            service.cancel("op-x")                     # l'agent clique « Annuler » pendant le 1er fichier
    service.emit = emit
    with pytest.raises(ServiceError, match="annulée"):
        service.cursus_build(MAT, word_ids(service), op="op-x")
    assert len(FakeConverter.calls) == 1               # le 2e et le 3e fichier ne sont jamais convertis
    assert len(db.deliveries()) == before              # aucun numéro de délivrance consommé
    # annulation reçue avant même le début : retenue
    service.cancel("op-y")
    with pytest.raises(ServiceError, match="annulée"):
        service.cursus_build(MAT, word_ids(service), op="op-y")


def test_annulation_pendant_une_conversion_longue(fonds, monkeypatch):
    service, _svc, _db, _events = fonds
    release = threading.Event()

    class Slow(FakeConverter):
        def convert(self, src, dest, fmt="pdf", label=None):
            release.wait(30)
            super().convert(src, dest, fmt, label)
    monkeypatch.setattr(preview, "WordConverter", Slow)
    threading.Timer(0.3, lambda: service.cancel("op-slow")).start()
    t0 = time.monotonic()
    try:
        with pytest.raises(ServiceError, match="annulée"):
            service.cursus_build(MAT, word_ids(service), op="op-slow")
        assert time.monotonic() - t0 < 5               # rendu à l'agent sans attendre la fin de Word
    finally:
        release.set()


# ------------------------------------------------------------------ aperçus du guichet remplacés
def test_apercu_du_guichet_remplace_par_le_plus_recent(fonds, monkeypatch):
    service, svc, db, _events = fonds
    release, started = threading.Event(), threading.Event()

    class Gate(FakeConverter):
        def convert(self, src, dest, fmt="pdf", label=None):
            if label == "EM5 S1.docx":
                started.set()
                release.wait(30)
            super().convert(src, dest, fmt, label)
    monkeypatch.setattr(preview, "WordConverter", Gate)
    rels = {r.chemin.rsplit("/", 1)[-1]: r for r in db.cursus(MAT) if r.is_word}
    results: dict[str, object] = {}

    def ask(name):
        try:
            results[name] = svc.ensure(rels[name], interactive=True)
        except Exception as exc:
            results[name] = exc
    t1 = threading.Thread(target=ask, args=("EM5 S1.docx",))   # en cours dans le fil Word
    t1.start()
    assert started.wait(10)
    t2 = threading.Thread(target=ask, args=("EM1 compil.docx",))  # en file…
    t2.start()
    time.sleep(0.3)
    t3 = threading.Thread(target=ask, args=("EM4 S1.docx",))   # … remplacé par celui-ci
    t3.start()
    time.sleep(0.3)
    release.set()
    for t in (t1, t2, t3):
        t.join(10)
    assert isinstance(results["EM1 compil.docx"], preview.PreviewCancelled)
    assert not isinstance(results["EM4 S1.docx"], Exception)
    assert not any("compil" in (lbl or "") for lbl in files_converted())
    assert len(FakeConverter.calls) == 2


def test_cursus_partage_le_travail_d_un_apercu_en_cours(fonds, monkeypatch):
    """Le cursus demande un fichier dont l'aperçu du guichet est déjà en file : même travail, et il n'est plus
    remplaçable (le cursus l'attend)."""
    service, svc, db, _events = fonds
    release, started = threading.Event(), threading.Event()

    class Gate(FakeConverter):
        def convert(self, src, dest, fmt="pdf", label=None):
            if label == "EM5 S1.docx" and not started.is_set():
                started.set()
                release.wait(30)
            super().convert(src, dest, fmt, label)
    monkeypatch.setattr(preview, "WordConverter", Gate)
    rels = {r.chemin.rsplit("/", 1)[-1]: r for r in db.cursus(MAT) if r.is_word}
    threading.Thread(target=lambda: svc.ensure(rels["EM5 S1.docx"], interactive=True), daemon=True).start()
    assert started.wait(10)
    queued = threading.Thread(target=lambda: svc.ensure(rels["EM1 compil.docx"], interactive=True), daemon=True)
    queued.start()
    time.sleep(0.2)
    out: dict = {}
    th = threading.Thread(target=lambda: out.update(service.cursus_build(MAT, word_ids(service))))
    th.start()
    time.sleep(0.3)
    release.set()
    th.join(20)
    assert out and out["missing"] == []
    assert len(FakeConverter.calls) == 3               # chaque fichier une seule fois, guichet et cursus confondus


def _wait_idle(svc, timeout=20):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        with svc._lock:
            if not svc._jobs:
                return
        time.sleep(0.05)
    raise AssertionError("travaux Word toujours en file")


def test_apercu_prepare_d_avance_les_autres_fichiers_de_l_etudiant(fonds):
    """Après l'aperçu d'un relevé, les autres fichiers Word du même étudiant sont convertis en arrière-plan :
    le cursus qui suit ne convertit plus rien (aucune attente au guichet)."""
    service, svc, db, _events = fonds
    rels = db.cursus(MAT)
    first = next(r for r in rels if "EM4" in r.chemin)
    service.preview(first.id)
    _wait_idle(svc)
    converted = {c[2] for c in FakeConverter.calls}
    assert converted == {"EM1 compil.docx", "EM4 S1.docx", "EM5 S1.docx"}
    FakeConverter.calls = []
    out = service.cursus_build(MAT, [r.id for r in rels], cover=False, duplicata=False)
    assert FakeConverter.calls == [] and out["pages"] >= len(rels)


def test_preparation_d_avance_cede_la_place_a_l_agent(fonds, monkeypatch):
    """Les préparations anticipées encore en attente sont abandonnées dès que l'agent ouvre un autre aperçu."""
    service, svc, db, _events = fonds
    gate = threading.Event()
    real = FakeConverter.convert

    def slow(self, src, dest, fmt="pdf", label=None):
        gate.wait(5)
        return real(self, src, dest, fmt, label)

    monkeypatch.setattr(FakeConverter, "convert", slow)
    rels = db.cursus(MAT)
    em4 = next(r for r in rels if "EM4" in r.chemin)
    others = [r for r in rels if r.fichier_id != em4.fichier_id]
    assert svc.prefetch(others) == 2
    t = threading.Thread(target=lambda: svc.ensure(em4, interactive=True))
    t.start()
    time.sleep(0.2)
    gate.set()
    t.join(10)
    _wait_idle(svc)
    names = [c[2] for c in FakeConverter.calls]
    assert "EM4 S1.docx" in names and len(names) <= 2     # au plus le fichier anticipé déjà commencé


def test_word_demarre_a_l_avance_sans_word_ne_fait_rien(fonds):
    _service, svc, _db, _events = fonds
    svc.warm_up()
    _wait_idle(svc)
    assert not WordConverter(svc.s).warm()          # pas de Word sous Linux : aucun effet, aucune erreur
