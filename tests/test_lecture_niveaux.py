"""Études médicales (« EM4 S1.docx »), doublons d'archive, noms aux espaces manquants, matricules illisibles,
relecture après changement de l'analyse. Toutes les données sont INVENTÉES (tools/generate_samples.py)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import make_settings

import generate_samples
from core import alerts
from core.database import Database
from core.extractors import PageText
from core.indexer import Indexer
from core.normalize import best_display_name, same_person_name
from core.parsing import PARSER_VERSION, content_fingerprint, hints_from_path, parse_pages
from core.service import Service, page_span_label


def _parse(lines, path="x.docx"):
    rels, warns = parse_pages([PageText(1, "\n".join(lines))], path)
    return rels[0], warns


def _replace(lines, old, new):
    return [ln.replace(old, new) for ln in lines]


# ------------------------------------------------------------------ niveaux et semestres
@pytest.mark.parametrize("annee,sem_ecrit,path,attendu", [
    (4, 1, "EM4 S1.docx", ("Niveau 4", 7)),          # semestre écrit dans l'année
    (4, 2, "EM4 S2.docx", ("Niveau 4", 8)),
    (5, 3, "EM5 S1.docx", ("Niveau 5", 9)),          # semestre écrit dans le cycle Master (avant : « Master 5 · S3 »)
    (5, 4, "Relevé em10708.docx", ("Niveau 5", 10)),  # pas d'indice dans le nom du fichier
])
def test_etudes_medicales_jamais_master(annee, sem_ecrit, path, attendu):
    r, _ = _parse(generate_samples.em_releve("19MM901", annee, sem_ecrit), path)
    assert (r["niveau"], r["semestre"]) == attendu
    assert r["filiere_code"] == "M" and r["cycle"] == "MASTER"      # le cycle reste tracé, sans servir de niveau
    assert 2 * int(attendu[0][-1]) - 1 <= r["semestre"] <= 2 * int(attendu[0][-1])


def test_niveau_ecrit_dans_le_cycle_master():
    """« NIVEAU: I » du cycle Master, semestre absolu VIII (avant : « Master 1 · S8 »)."""
    lines = _replace(generate_samples.em_releve("19MM901", 4, 2), "NIVEAU: IV SEMESTRE: II", "NIVEAU: I SEMESTRE: VIII")
    lines = _replace(lines, "4 2 30", "1 8 30")
    r, _ = _parse(lines, "Relevé em10708 UPDATE.docx")
    assert (r["niveau"], r["semestre"]) == ("Niveau 4", 8)


def test_sans_entete_ni_nom_de_fichier_le_matricule_suffit():
    lines = [ln for ln in generate_samples.em_releve("19MM901", 5, 3) if not ln.startswith("ETUDES")]
    lines = _replace(lines, "FILIÈRE: MEDECINE", "")
    r, _ = _parse(lines, "scan.docx")
    assert (r["niveau"], r["semestre"], r["filiere_code"]) == ("Niveau 5", 9, "M")


def test_vrai_master_inchange():
    s = generate_samples.student("16MS901", "FICTIF", "Master", "MS", 2, "2017-2018")
    r, _ = _parse(generate_samples.relevé_lines(s), "M2_SantePublique.pdf")
    assert (r["niveau"], r["filiere_code"]) == ("Master 2", "MS")


def test_indices_du_nom_de_fichier():
    h = hints_from_path(r"\\SRV\DOCUMENTS ACADEMIQUE FMSP 2022-\RN NON TRAITES\EM4 S1.docx")
    assert (h.annee_etudes, h.filiere_code, h.semestre) == (4, "M", 1)
    assert hints_from_path(r"X:\Relevé em10708 UPDATE.docx").annee_etudes is None


def test_validé_avec_moyenne_nulle_a_verifier():
    lines = generate_samples.em_releve("19MM901", 4, 1)
    lines = [ln if not ln.startswith("4 1 30") else "4 1 30 0,00 2,7 B- SEMESTRE VALIDE" for ln in lines]
    r, warns = _parse(lines, "EM4 S1.docx")
    assert r["statut"] == "À VÉRIFIER" and any("moyenne de 0,00" in w for w in warns)


def test_empreinte_du_contenu():
    a = generate_samples.em_releve("19MM901", 4, 1)
    meme = ["EN-TÊTE DIFFÉRENT"] + list(reversed(a))                  # autre mise en page, autre ordre de lecture
    k = next(i for i, ln in enumerate(a) if ln.startswith("UE "))
    corrige = a[:k] + [re.sub(r"\d+,\d\d", "19,99", a[k], count=1)] + a[k + 1:]
    assert content_fingerprint("19MM901", "\n".join(a)) == content_fingerprint("19MM901", "\n".join(meme))
    assert content_fingerprint("19MM901", "\n".join(a)) != content_fingerprint("19MM901", "\n".join(corrige))
    assert content_fingerprint("19MM902", "\n".join(a)) != content_fingerprint("19MM901", "\n".join(a))
    # barème des grades, MGP et gabarit vide : pas assez de notes pour conclure
    assert content_fingerprint("19MM901", "A + 4.0 Excellent [18-20]\n0,00 0,00 0,00 0,00 2,7") is None


def test_matricule_illisible_propre_au_fichier():
    lines = generate_samples.em_releve("19MM901", 4, 1, matricule=False)
    a, _ = _parse(lines, r"X:\A\EM4 S1.docx")
    b, _ = _parse(lines, r"X:\B\EM4 S1.docx")
    assert a["matricule"].startswith("ILLISIBLE-P1-") and b["matricule"].startswith("ILLISIBLE-P1-")
    assert a["matricule"] != b["matricule"]


def test_noms_aux_espaces_manquants():
    assert same_person_name("AURORE BELLENGO ESSAMA", "Aurore Belle-Ngo Essama")
    assert not same_person_name("AURORE BELLE", "AURORE BELLA")
    assert best_display_name(["AURORE BELLENGO ESSAMA", "AURORE BELLE NGO ESSAMA",
                              "AURORE BELLENGO ESSAMA"]) == "AURORE BELLE NGO ESSAMA"


# ------------------------------------------------------------------ fonds fictif indexé
@pytest.fixture(scope="module")
def em(tmp_path_factory, data_dir):
    base = tmp_path_factory.mktemp("em")
    files = generate_samples.generate_medecine_em(base / "archives")
    settings = make_settings(base, ocr_enabled=False)
    db = Database(settings.db_path, cache_dir=settings.resolved_cache_dir())
    Indexer(settings, db).run(base / "archives", only_new=False, workers=1)
    return db, settings, files, base / "archives"


def _ids(db, path):
    return {r.id for r in db._select("f.chemin = ?", [str(Path(path).resolve())])}


def test_cursus_un_seul_exemplaire_par_releve(em):
    db, _, files, _ = em
    cur = db.cursus("19MM901")
    assert [r.periode for r in cur] == ["Niveau 4 · S7", "Niveau 4 · S8", "Niveau 5 · S9", "Niveau 5 · S10"]
    s7 = cur[0]
    assert Path(s7.chemin).name == "Relevé em10901 UPDATE.docx"          # version « UPDATE » préférée
    assert {Path(d["chemin"]).name for d in s7.doublons} == {"Relevé em10901.docx", "EM4 S1.docx"}
    assert all(d["meme_contenu"] for d in s7.doublons)
    assert "3 EXEMPLAIRES" in s7.flags and not any("RELEVÉS S" in f for f in s7.flags)
    assert not any("Master" in (r.niveau or "") for r in db._select("1=1", []))


def test_guichet_et_demandes_sans_doublon(em):
    db, settings, files, _ = em
    found = db.search("19MM901")
    assert len(found) == 4 and len({r.id for r in found}) == 4
    compil = next(iter(_ids(db, files["em4s1"]) & {r.id for r in db._select("r.matricule='19MM901'", [])}))
    rel = db.get(compil)                                   # ancien lien vers l'exemplaire de la compilation
    assert len(rel.doublons) == 2 and db.level_siblings(rel) == []
    assert len(db.collapse([rel, db.get(rel.doublons[0]["id"])])) == 1
    svc = Service(settings, db_factory=lambda: db)
    assert svc.groups([r.id for r in found])[0]["total"] == 4
    did = db.create_demande("G1", "test", "19MM901")
    assert db.add_demande_item(did, compil) is True
    assert db.add_demande_item(did, rel.doublons[0]["id"]) is False       # même relevé, autre fichier
    assert len(db.demande_items(did)) == 1
    msgs = alerts.compute(rel, [], False)
    assert any("Même relevé dans 3 fichiers" in m for m, _ in msgs)
    assert len([r for r in db.extraction("M", 2022, 2022) if r.matricule == "19MM901"]) == 4


def test_cursus_info_pages_nom_et_matricule_illisible(em):
    db, settings, files, _ = em
    info = Service(settings, db_factory=lambda: db).cursus_info("19MM901")
    assert info["nom"] == "AURORE BELLE NGO ESSAMA"                       # pas la forme collée
    assert [r["pages_label"] for r in info["releves"]] == ["1", "1 (p. 2)", "1", "1 (p. 2)"]
    assert info["releves"][1]["pages_fichier"] == "Page 2"
    assert info["correlated"] == []                                       # plus de « ILLISIBLE-P3 » proposé
    sans = info["sans_matricule"]
    assert len(sans) == 1 and sans[0]["nom_complet"] == "AURORE BELLENGO ESSAMA"
    # le relevé illisible de la page 3 d'un AUTRE fichier (autre étudiante) n'est jamais mêlé
    assert len(db._select("r.matricule = ?", [sans[0]["matricule"]])) == 1
    illisibles = {r.matricule for r in db._select("r.matricule LIKE 'ILLISIBLE%'", [])}
    assert len(illisibles) == 3


def test_page_du_releve_et_non_du_fichier():
    from types import SimpleNamespace
    r = SimpleNamespace(nb_pages=1, numero_page=41, page_fin=41)
    assert page_span_label(r) == "1 (p. 41)"
    assert page_span_label(SimpleNamespace(nb_pages=2, numero_page=41, page_fin=42)) == "2 (p. 41–42)"
    assert page_span_label(SimpleNamespace(nb_pages=1, numero_page=1, page_fin=1)) == "1"


def test_update_corrige_et_notes_differentes(tmp_path, data_dir):
    db = Database(tmp_path / "i.db")
    base = dict(matricule="19MM904", nom_complet="FICTIF UPDATE", niveau="Niveau 4", niveau_num=4, semestre=7,
                semestre_fin=7, annee_academique="2022-2023", statut="VALIDÉ", decision="SEMESTRE VALIDE",
                moyenne="12,50", numero_page=1, page_fin=1)
    db.store_result("/a/Releve 904.docx", "DOCX", 1, 1.0, 1, None, "OK", None, [dict(base, empreinte="aaa")], [])
    db.store_result("/a/Releve 904 UPDATE.docx", "DOCX", 1, 1.0, 1, None, "OK", None,
                    [dict(base, empreinte="bbb")], [])                    # une note corrigée, même moyenne
    cur = db.cursus("19MM904")
    assert len(cur) == 1 and cur[0].chemin.endswith("UPDATE.docx") and cur[0].doublons[0]["meme_contenu"] is False
    assert any(t == "amber" and "notes différentes" in m for m, t in alerts.compute(cur[0], [], False))
    # notes différentes SANS version corrigée identifiée : deux relevés, l'agent tranche
    db.store_result("/a/Autre 905.docx", "DOCX", 1, 1.0, 1, None, "OK", None,
                    [dict(base, matricule="19MM905", empreinte="ccc")], [])
    db.store_result("/a/Copie 905.docx", "DOCX", 1, 1.0, 1, None, "OK", None,
                    [dict(base, matricule="19MM905", empreinte="ddd")], [])
    cur = db.cursus("19MM905")
    assert len(cur) == 2 and "2 RELEVÉS S7" in cur[0].flags


# ------------------------------------------------------------------ relecture quand l'analyse change
def test_differentiel_relit_les_fichiers_analyses_par_une_ancienne_version(tmp_path, data_dir):
    generate_samples.generate_medecine_em(tmp_path / "archives")
    settings = make_settings(tmp_path, ocr_enabled=False)
    db = Database(settings.db_path, cache_dir=settings.resolved_cache_dir())
    Indexer(settings, db).run(tmp_path / "archives", only_new=True, workers=1)
    assert set(v[2] for v in db.file_signatures(with_version=True).values()) == {PARSER_VERSION}
    c = Indexer(settings, db).run(tmp_path / "archives", only_new=True, workers=1)
    assert c.extraits == 0 and c.relus == 0 and c.ignores == c.detectes
    before = {(r.chemin, r.numero_page): r.id for r in db._select("1=1", [])}
    did = db.create_demande("G1", "test", "19MM901")
    db.add_demande_item(did, db.cursus("19MM901")[0].id)
    # base indexée par une version antérieure (colonne vide) : relue sans « Vider l'index »
    db.conn.execute("UPDATE fichiers SET version_analyse = NULL")
    db.conn.execute("UPDATE etudiants_releves SET niveau = 'Master 4'")
    db.conn.commit()
    c = Indexer(settings, db).run(tmp_path / "archives", only_new=True, workers=1)
    assert c.relus == c.detectes == c.extraits
    assert not db._select("r.niveau LIKE 'Master%'", [])
    after = {(r.chemin, r.numero_page): r.id for r in db._select("1=1", [])}
    assert after == before                                  # mêmes identifiants : la demande reste valable
    assert db.demande_items(did)[0][1] is not None
