"""Génère un faux fonds d'archives FMSP pour tester l'application sans données réelles.

    python tools/generate_samples.py ./archives_demo

Contenu produit (données fictives) :
- fichiers collectifs PDF texte, .docx et .doc (si LibreOffice est installé) ;
- un PDF scanné de travers (pour l'OCR et le redressement) ;
- 18M042 TCHOUA Alain : cursus N1 à N7 avec niveau 6 manquant, N3 en session normale (AJOURNÉ)
  ET en rattrapage (ADMIS), N4 sur 2 pages (34–35) ;
- 15B077 TCHOUA Alain : même personne en Biomédical (même date de naissance) ;
- homonymes et quasi-homonymes : TCHOUA Marie-Claire, TCHOUWA Serge, KOUAME / KOUAM, N'DOUKOU ;
- matricules écrits avec tirets ou slashs (18-M-203, 17/M/090) ;
- un .doc corrompu, pour vérifier que l'indexation continue malgré l'erreur.

``generate_medecine_em(root)`` (option ``--medecine-em``) produit à part le cas « études médicales »
observé sur l'installation réelle, avec des données INVENTÉES : compilations Word « EM4 S1.docx »…
(« ÉTUDES MÉDICALES 4ème ANNÉE », « CYCLE: MASTER », semestre écrit dans l'année ou dans le cycle),
le même relevé extrait dans un fichier individuel et sa copie « … UPDATE », un nom aux espaces manquants
et des en-têtes sans matricule lisible.
"""
from __future__ import annotations

import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

RNG = random.Random(42)

UES = {
    "M": ["Sémiologie médicale", "Pharmacologie générale", "Microbiologie médicale", "Immunologie",
          "Anatomie pathologique", "Physiopathologie", "Santé publique", "Stage hospitalier"],
    "P": ["Chimie pharmaceutique", "Pharmacognosie", "Pharmacie galénique", "Biochimie",
          "Toxicologie", "Pharmacologie", "Bactériologie", "Stage officinal"],
    "B": ["Biologie cellulaire", "Biochimie structurale", "Génétique", "Biostatistiques",
          "Physiologie", "Hématologie", "Parasitologie", "Travaux pratiques"],
    "MS": ["Épidémiologie", "Méthodes de recherche", "Économie de la santé", "Biostatistiques avancées",
           "Politiques de santé", "Gestion de programmes", "Séminaire", "Mémoire"],
}
FIL_LABEL = {"M": "Médecine Générale", "P": "Pharmacie", "B": "Sciences Biomédicales", "MS": "Master Santé Publique"}
NOMS = ["ABENA", "FOTSO", "MBARGA", "NJOYA", "ATANGANA", "ESSOMBA", "EKOTTO", "BELLO", "NANA", "TAMO",
        "DJOUMESSI", "MOUKOURI", "NGASSA", "ONANA", "WAMBA", "YOMBI", "ZANGA", "KENFACK", "MEKONGO", "TALLA"]
PRENOMS = ["Christelle", "Gaëlle", "Yannick", "Ibrahim", "Brigitte", "Rodrigue", "Sandrine", "Aïssatou",
           "Hervé", "Larissa", "Franck", "Murielle", "Arnaud", "Carine", "Joël", "Patricia"]


def student(mat, nom, prenom, fil, niv, annee, session="Normale", statut="ADMIS", dob=None, pages=1,
            continuation_header=True, mat_display=None, seed=0):
    return dict(mat=mat, nom=nom, prenom=prenom, fil=fil, niv=niv, annee=annee, session=session, statut=statut,
                dob=dob or f"{RNG.randint(1, 28):02d}/{RNG.randint(1, 12):02d}/{RNG.randint(1995, 2003)}",
                pages=pages, cont=continuation_header, mat_display=mat_display or mat, seed=seed or RNG.randint(1, 99))


def notes(s):
    base = 10.5 if s["statut"] == "ADMIS" else 6.0
    vals = [base + ((s["seed"] * 37 + i * 53) % 60) / 10 for i in range(8)]
    return vals


def niveau_label(s):
    return f"Master {s['niv']}" if s["fil"] == "MS" else str(s["niv"])


def relevé_lines(s, part=None):
    """Lignes d'un relevé. part=None : relevé complet ; 1 ou 2 : première / deuxième page."""
    vals = notes(s)
    ues = UES[s["fil"]]
    lines = []
    if part in (None, 1) or s["cont"]:
        lines += [
            "RÉPUBLIQUE DU CAMEROUN - Paix - Travail - Patrie",
            "UNIVERSITÉ DE DOUALA",
            "FACULTÉ DE MÉDECINE ET DES SCIENCES PHARMACEUTIQUES",
            "",
            "RELEVÉ DE NOTES" + (" (suite)" if part == 2 else ""),
            f"Année académique : {s['annee']}        Session : {s['session']}",
        ]
        if part != 2:
            lines += [f"Noms et Prénoms : {s['nom']} {s['prenom']}"]
        lines += [f"Matricule : {s['mat_display']}        Né(e) le : {s['dob']}"]
        if part != 2:
            lines += [f"Filière : {FIL_LABEL[s['fil']]}        Niveau : {niveau_label(s)}"]
        lines += [""]
    rng = range(8) if part is None else (range(4) if part == 1 else range(4, 8))
    lines.append("Code      Unité d'enseignement                 Crédits   Note/20   Résultat")
    pre = {"M": "MED", "P": "PHA", "B": "BMD", "MS": "MSP"}[s["fil"]]
    for i in rng:
        res = "Validée" if vals[i] >= 10 else "Non validée"
        lines.append(f"{pre}{s['niv']}{i + 1:02d}    {ues[i]:<36} {[6, 4, 5, 3, 5, 6, 3, 6][i]:>3}      "
                     f"{vals[i]:5.2f}    {res}".replace(".", ","))
    if part in (None, 2):
        moy = sum(vals) / len(vals)
        lines += ["", f"Moyenne annuelle : {moy:.2f} / 20".replace(".", ","),
                  f"Décision du jury : {s['statut']}", "", "Fait à Douala, le ____________          Le Doyen"]
    else:
        lines += ["", "... suite au verso"]
    return lines


def pages_for(students, start_page=1, filler_before=0):
    """Liste de pages (listes de lignes). filler_before : pages d'autres étudiants avant."""
    pages = []
    for s in students:
        if s["pages"] == 2:
            pages.append(relevé_lines(s, 1))
            pages.append(relevé_lines(s, 2))
        else:
            pages.append(relevé_lines(s))
    return pages


def write_pdf(path: Path, pages):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    for lines in pages:
        y = h - 60
        for line in lines:
            size = 13 if line.startswith("RELEVÉ") else 10
            c.setFont("Helvetica-Bold" if size == 13 or line.startswith("UNIVERSITÉ") else "Helvetica", size)
            c.drawString(50, y, line)
            y -= 18
        c.showPage()
    c.save()


def write_docx(path: Path, pages):
    import docx
    from docx.enum.text import WD_BREAK
    from docx.shared import Pt
    path.parent.mkdir(parents=True, exist_ok=True)
    d = docx.Document()
    style = d.styles["Normal"]
    style.font.name = "Courier New"
    style.font.size = Pt(9)
    for i, lines in enumerate(pages):
        for line in lines:
            d.add_paragraph(line)
        if i < len(pages) - 1:
            d.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    d.save(str(path))


def to_doc(docx_path: Path, doc_path: Path) -> bool:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([soffice, "--headless", "--convert-to", "doc", "--outdir", tmp, str(docx_path)],
                       capture_output=True, timeout=120)
        out = Path(tmp) / (docx_path.stem + ".doc")
        if out.exists():
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(out), doc_path)
            return True
    return False


def write_scanned_pdf(path: Path, pages):
    """PDF image : rendu 150 DPI, rotation 1,8°, bruit léger (comme un scan d'imprimante)."""
    import pypdfium2 as pdfium
    from PIL import Image, ImageFilter
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.pdf"
        write_pdf(src, pages)
        doc = pdfium.PdfDocument(str(src))
        images = []
        for i in range(len(doc)):
            img = doc[i].render(scale=150 / 72).to_pil().convert("L")
            img = img.rotate(-1.8, expand=True, fillcolor=255, resample=Image.BICUBIC)
            img = img.filter(ImageFilter.GaussianBlur(0.4))
            images.append(img.convert("RGB"))
        doc.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        images[0].save(path, save_all=True, append_images=images[1:], resolution=150)


def fillers(n, fil, niv, annee, prefix_year):
    out = []
    for _ in range(n):
        mat = f"{prefix_year}{fil}{RNG.randint(100, 399):03d}"
        out.append(student(mat, RNG.choice(NOMS), RNG.choice(PRENOMS), fil, niv, annee,
                           statut=RNG.choice(["ADMIS"] * 5 + ["AJOURNÉ"])))
    return out


def generate(root: Path) -> None:
    if root.exists():
        shutil.rmtree(root)
    tchoua_dob = "12/03/1999"
    T = lambda niv, annee, **kw: student("18M042", "TCHOUA", "Alain", "M", niv, annee, dob=tchoua_dob, seed=3, **kw)  # noqa

    # ---- Cursus 18M042 (niveau 6 absent volontairement)
    write_pdf(root / "2018-2019/Medecine/N1_SN_Releves.pdf",
              pages_for(fillers(3, "M", 1, "2018-2019", "18") + [T(1, "2018-2019")] + fillers(2, "M", 1, "2018-2019", "18")))
    write_docx(root / "2019-2020/Medecine/N2_SN_Releves.docx",
               pages_for(fillers(2, "M", 2, "2019-2020", "18") + [T(2, "2019-2020")]))
    # N3 : session normale en .doc (AJOURNÉ), rattrapage en .docx (ADMIS)
    n3 = root / "2020-2021/Medecine/N3_SN_Releves.docx"
    write_docx(n3, pages_for(fillers(3, "M", 3, "2020-2021", "18") + [T(3, "2020-2021", statut="AJOURNÉ")]
                             + [student("20M118", "KAMGA", "Paul Hervé", "M", 3, "2020-2021", statut="AJOURNÉ")]))
    if to_doc(n3, n3.with_suffix(".doc")):
        n3.unlink()
    write_docx(root / "2020-2021/Medecine/N3_SR_Releves.docx",
               pages_for([T(3, "2020-2021", session="Rattrapage", statut="ADMIS")]
                         + fillers(2, "M", 3, "2020-2021", "18")))
    # N4 : relevé sur 2 pages (avec et sans en-tête de suite)
    write_pdf(root / "2021-2022/Medecine/N4_SN_Releves.pdf",
              pages_for(fillers(4, "M", 4, "2021-2022", "18")
                        + [T(4, "2021-2022", pages=2, continuation_header=False)]
                        + [student("19M042", "KAMGA", "Josiane", "M", 4, "2021-2022", pages=2)]
                        + fillers(2, "M", 4, "2021-2022", "18")))
    write_pdf(root / "2022-2023/Medecine/N5_SN_Releves.pdf",
              pages_for([T(5, "2022-2023")] + fillers(3, "M", 5, "2022-2023", "18")
                        + [student("17M090", "N'DOUKOU", "Arlette", "M", 5, "2022-2023", mat_display="17/M/090")]))
    write_pdf(root / "2024-2025/Medecine/N7_SN_Releves.pdf",
              pages_for(fillers(2, "M", 7, "2024-2025", "18") + [T(7, "2024-2025")]))

    # ---- Corrélation : même personne, ancien matricule biomédical
    write_pdf(root / "2015-2016/Biomedical/B1_SN_Releves.pdf",
              pages_for([student("15B077", "TCHOUA", "Alain", "B", 1, "2015-2016", dob=tchoua_dob)]
                        + fillers(3, "B", 1, "2015-2016", "15")))
    write_docx(root / "2019-2020/Biomedical/B3_SN_Releves.docx",
               pages_for([student("17B105", "TCHOUA NGUEMO", "Brice", "B", 3, "2019-2020")]
                         + fillers(3, "B", 3, "2019-2020", "17")))

    # ---- Pharmacie scannée (OCR) et homonymes
    write_scanned_pdf(root / "2020-2021/Pharmacie/P2_SN_scan.pdf",
                      pages_for([student("19P017", "TCHOUA", "Marie-Claire", "P", 2, "2020-2021")]
                                + fillers(1, "P", 2, "2020-2021", "19")))
    write_pdf(root / "2021-2022/Pharmacie/P4_SN_Releves.pdf",
              pages_for([student("18P203", "KOUAME", "Estelle", "P", 4, "2021-2022", mat_display="18-P-203")]
                        + fillers(4, "P", 4, "2021-2022", "18")))
    write_pdf(root / "2021-2022/Medecine/N1_SN_Releves.pdf",
              pages_for([student("21M310", "TCHOUWA", "Serge", "M", 1, "2021-2022")]
                        + fillers(3, "M", 1, "2021-2022", "21")))
    write_pdf(root / "2017-2018/Masters/M2_SantePublique.pdf",
              pages_for([student("16MS008", "TCHOUA", "Fabrice", "MS", 2, "2017-2018")]
                        + fillers(2, "MS", 2, "2017-2018", "16")))
    write_pdf(root / "2020-2021/Biomedical/B1_SN_Releves.pdf",
              pages_for([student("20B044", "NGO BIKAI", "Ruth", "B", 1, "2020-2021", statut="EXCLUSION")]
                        + fillers(2, "B", 1, "2020-2021", "20")))

    # ---- Fichier corrompu (doit apparaître dans le journal d'anomalies, sans arrêter l'indexation)
    bad = root / "2011-2012/Medecine/N2_Releves.doc"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(RNG.getrandbits(8) for _ in range(4096)))

    count = sum(1 for _ in root.rglob("*.*"))
    print(f"{count} fichiers générés dans {root.resolve()}")


# ---------------------------------------------------------------- études médicales (format récent)
EM_ETUDIANTS = {   # tout est inventé
    "19MM901": ("AURORE BELLE NGO ESSAMA", "14/02/2001"),
    "19MM902": ("FICTIF PATRICE ONDOA", "03/09/2000"),
    "19MM903": ("FICTIVE LINDA ABEGA", "27/06/2001"),
}
ROMAINS = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII", 8: "VIII", 9: "IX", 10: "X"}


def em_releve(mat, annee_etudes, sem_ecrit, annee="2022 - 2023", nom=None, cycle="MASTER", matricule=True,
              seed=None, moyenne=None):
    """Lignes d'un relevé « ÉTUDES MÉDICALES » au format récent. ``sem_ecrit`` : le semestre tel qu'imprimé
    (dans l'année : I/II, ou dans le cycle Master : III/IV pour la 5e année)."""
    nom_reel, dob = EM_ETUDIANTS[mat]
    seed = seed if seed is not None else int(mat[-3:]) * 7 + annee_etudes * 3 + sem_ecrit
    vals = [10 + ((seed * 31 + i * 17) % 80) / 10 for i in range(6)]
    moy = moyenne or f"{sum(vals) / len(vals):.2f}".replace(".", ",")
    ues = ["CARDIOLOGIE", "PNEUMOLOGIE", "GASTRO-ENTEROLOGIE", "NEPHROLOGIE", "PEDIATRIE", "GYNECOLOGIE"]
    lines = [f"ETUDES MEDICALES {annee_etudes}ème ANNEE",
             "REPUBLIQUE DU CAMEROUN", "UNIVERSITE DE DOUALA", "FACULTE DE MEDECINE ET DES SCIENCES PHARMACEUTIQUES",
             "RELEVE DE NOTES /TRANSCRIPT",
             f"NOM ET PRENOM: {nom or nom_reel}" + (f" MATRICULE: {mat}" if matricule else " MATRICULE:"),
             "surname and name: Registration N°:",
             f"NÉ(E) LE : {dob} A : VILLE FICTIVE",
             f"CYCLE: {cycle} ANNÉE ACADÉMIQUE: {annee} FILIÈRE: MEDECINE",
             f"NIVEAU: {ROMAINS[annee_etudes]} SEMESTRE: {ROMAINS[sem_ecrit]}",
             "CODE UNITE D’ENSEIGNEMENT ELEMENT CONSTITUTIF NOTE/20 MOYENNE CREDIT"]
    for i, (ue, v) in enumerate(zip(ues, vals)):
        lines.append(f"UE M{annee_etudes}{sem_ecrit}{i} {ue} {ue} {v:.2f} {v:.2f} 5".replace(".", ","))
    lines += ["RELEVE NIVEAU SEMESTRE TOTAL CREDIT / 30 MOYENNE SEMESTRIELLE / 20 MGP GRADE DECISION DU JURY",
              f"{annee_etudes} {sem_ecrit} 30 {moy} 2,7 B- SEMESTRE VALIDE",
              "Grade Note/4 Appréciation Moy /20", "A + 4.0 Excellent [18-20]"]
    return lines


def generate_medecine_em(root: Path) -> dict:
    """Le cas réel reproduit avec des données inventées. Renvoie {nom logique: chemin}."""
    base = root / "DOCUMENTS ACADEMIQUE FMSP 2022-" / "RN NON TRAITES"
    f = {
        "em4s1": base / "EM4 S1.docx", "em4s2": base / "EM4 S2.docx",
        "em5s1": base / "EM5 S1.docx", "em5s2": base / "EM5 S2.docx",
        "indiv": base / "Relevé em10901.docx", "update": base / "Relevé em10901 UPDATE.docx",
        "autre": root / "AUTRES" / "Compilation fictive.docx",
    }
    # Compilations de promotion (le relevé de 19MM901 est au milieu)
    write_docx(f["em4s1"], [em_releve("19MM902", 4, 1), em_releve("19MM901", 4, 1), em_releve("19MM903", 4, 1)])
    write_docx(f["em4s2"], [em_releve("19MM903", 4, 2), em_releve("19MM901", 4, 2), em_releve("19MM902", 4, 2)])
    # 5e année : semestre numéroté dans le cycle Master (III, IV) ; niveau écrit « I/II » du cycle sur EM5 S2
    write_docx(f["em5s1"], [em_releve("19MM901", 5, 3), em_releve("19MM902", 5, 3)])
    em5s2 = em_releve("19MM901", 5, 4)
    write_docx(f["em5s2"], [em_releve("19MM902", 5, 4), em5s2,
                            # en-tête sans matricule lisible, même personne (nom collé)
                            em_releve("19MM901", 5, 4, nom="AURORE BELLENGO ESSAMA", matricule=False, seed=77)])
    # Le même relevé extrait dans un fichier individuel (nom aux espaces manquants), puis sa copie « UPDATE »
    indiv = [em_releve("19MM901", 4, 1, nom="AURORE BELLENGO ESSAMA"), em_releve("19MM901", 4, 2)]
    write_docx(f["indiv"], indiv)
    write_docx(f["update"], indiv)
    # Ailleurs : un AUTRE étudiant dont le matricule est illisible à la même page (ne doit jamais être mêlé)
    write_docx(f["autre"], [em_releve("19MM902", 4, 1, annee="2021 - 2022", seed=5),
                            em_releve("19MM903", 4, 1, matricule=False, seed=9),
                            em_releve("19MM903", 4, 2, matricule=False, seed=11)])
    return f


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dest = Path(args[0] if args else "archives_demo")
    generate(dest)
    if "--medecine-em" in sys.argv:
        generate_medecine_em(dest)
