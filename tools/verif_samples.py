"""Documents « soumis » FICTIFS pour l'écran Vérification (tests et e2e) : aucune donnée réelle.

    python tools/verif_samples.py <dossier>

Produit, à partir du relevé N5 fictif de 18M042 TCHOUA Alain (tools/generate_samples.py) :
- ``soumis_falsifie.pdf``  : PDF texte, note de MED502 et moyenne retouchées (2 divergences) ;
- ``soumis_falsifie_scan.png`` : le même document « scanné » (image 150 ppp, légèrement de travers) ;
- ``soumis_falsifie_scan.pdf`` : le même scan, en PDF image ; ``soumis_falsifie_scan.tif`` : en TIFF.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ORIG_NOTE, FAUSSE_NOTE = "14,90", "16,90"     # MED502 Pharmacologie générale


def lignes_falsifiees() -> list[str]:
    import generate_samples as g
    s = g.student("18M042", "TCHOUA", "Alain", "M", 5, "2022-2023", dob="12/03/1999", seed=3)
    out = []
    for line in g.relevé_lines(s):
        if line.startswith("MED502"):
            assert ORIG_NOTE in line, line
            line = line.replace(ORIG_NOTE, FAUSSE_NOTE)
        elif line.startswith("Moyenne annuelle"):
            line = "Moyenne annuelle : 14,15 / 20"
        out.append(line)
    return out


def generate(dest: Path) -> dict[str, Path]:
    import generate_samples as g
    import pypdfium2 as pdfium
    from PIL import Image, ImageFilter
    dest.mkdir(parents=True, exist_ok=True)
    pdf = dest / "soumis_falsifie.pdf"
    g.write_pdf(pdf, [lignes_falsifiees()])
    doc = pdfium.PdfDocument(str(pdf))
    img = doc[0].render(scale=150 / 72).to_pil().convert("L")
    doc.close()
    img = img.rotate(-1.2, expand=True, fillcolor=255, resample=Image.BICUBIC).filter(ImageFilter.GaussianBlur(0.4))
    png = dest / "soumis_falsifie_scan.png"
    img.convert("RGB").save(png)
    scan = dest / "soumis_falsifie_scan.pdf"
    img.convert("RGB").save(scan, resolution=150)
    tif = dest / "soumis_falsifie_scan.tif"          # TIFF : pas d'aperçu dans l'interface, contenu comparé quand même
    img.save(tif, compression="tiff_deflate")
    return {"pdf": pdf, "png": png, "scan": scan, "tif": tif}


if __name__ == "__main__":
    for k, v in generate(Path(sys.argv[1])).items():
        print(k, v)
