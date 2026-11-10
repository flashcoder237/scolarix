"""Anomalies FICTIVES en nombre (mesures de performance de l'écran Indexation › Anomalies).

    python tools/anomalies_fictives.py <base index.db> [nombre=10000] [racine fictive]

Chemins, dossiers et messages entièrement inventés (aucune donnée réelle) ; les messages reprennent les
libellés du robot pour que le classement par cause soit réaliste. Les fichiers n'existent pas.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# (niveau, message, action, poids) — poids proches d'un poste réel (9 412 anomalies, 13 causes)
MODELES = (
    ("AVERTISSEMENT", "PDF scanné non lu : OCR désactivé ou Tesseract introuvable.", "Activer l'OCR", 3104),
    ("AVERTISSEMENT", "Aucun relevé détecté dans ce fichier.", "Revoir le fichier", 1486),
    ("AVERTISSEMENT", "Page {p} : aucun matricule détecté.", "Revoir la page", 1122),
    ("AVERTISSEMENT", "OCR : confiance faible (< 70 %) sur la/les page(s) {p}. À vérifier manuellement.", "Revoir la page", 846),
    ("AVERTISSEMENT", "99X{n:03d} (page {p}) : décision du jury non détectée.", "Revoir la page", 702),
    ("ERREUR", "Fichier endommagé (en-tête OLE illisible). Aucune écriture tentée sur l'original.", "Ouvrir le dossier", 618),
    ("AVERTISSEMENT", "Page {p} : document collectif (procès-verbal / liste) ignoré : ce n'est pas un relevé individuel.", "Revoir la page", 455),
    ("AVERTISSEMENT", "99X{n:03d} (page {p}) : année académique incohérente (2004-2007).", "Revoir la page", 391),
    ("AVERTISSEMENT", "99X{n:03d} (page {p}) : date de naissance factice (01/01/1900).", "Revoir la page", 288),
    ("AVERTISSEMENT", "Lien orphelin : fichier déplacé, renommé ou supprimé.", "Relocaliser", 174),
    ("INFO", "2 relevé(s) sur plusieurs pages jumelé(s) (ex. 99X{n:03d} : pages {p}–{q}).", None, 162),
    ("ERREUR", "Fichier Word 97-2003 (.doc) : Microsoft Word ou LibreOffice est nécessaire.", "Ouvrir le dossier", 41),
    ("ERREUR", "Fichier protégé par mot de passe : ignoré, indexation poursuivie.", "Ouvrir le dossier", 23),
)
FILIERES = ("Medecine", "Pharmacie", "Biomedical", "Masters")


def generate(db_path: str | Path, n: int = 10000, root: str = "/archives_fictives", seed: int = 7) -> int:
    from core.database import Database
    rnd = random.Random(seed)
    db = Database(db_path)
    weights = [m[3] for m in MODELES]
    rows = []
    files_per_folder = 60
    for i in range(n):
        niveau, msg, action, _ = rnd.choices(MODELES, weights)[0]
        folder_no = (i // files_per_folder) % 90
        y = 1996 + folder_no % 29
        ext = ".doc" if "Word" in msg else ".pdf"
        chemin = (f"{root}/{y}-{y + 1}/{FILIERES[folder_no % 4]}/Lot_{folder_no:02d}/"
                  f"Scan_fictif_{i // 2:05d}{ext}")
        p = rnd.randint(1, 9)
        rows.append((niveau, chemin, msg.format(p=p, q=p + 1, n=i % 1000), action))
    from core.database import _ANOMALY_INSERT, _anomaly_row
    with db._lock:
        db.conn.executemany(_ANOMALY_INSERT, [_anomaly_row(*r) for r in rows])
        db.conn.commit()
    db.close()
    return len(rows)


if __name__ == "__main__":
    target = sys.argv[1]
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
    racine = sys.argv[3] if len(sys.argv) > 3 else "/archives_fictives"
    print(generate(target, count, racine))
