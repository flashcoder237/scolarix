# Archives Relevés FMSP

Application de bureau Windows pour la Cellule Informatique de la FMSP (Université de Douala).
Elle **indexe les relevés de notes** accumulés au fil des années (Word .doc/.docx, PDF texte, PDF scannés),
puis permet au guichet de **retrouver, prévisualiser, imprimer et exporter un relevé en moins de 10 secondes**,
sans ouvrir Word ni Adobe et **sans jamais modifier les originaux**.

Python 3.11+ · PySide6 (Qt 6, QtPdf) · SQLite WAL + FTS5 · pdfplumber · Tesseract · pypdf · reportlab

![Guichet](docs/captures/guichet.png)

## Ce que fait l'application

| Écran | Fonctions | Cas des documents couverts |
|---|---|---|
| **Guichet** | Omnibox avec recherche au fil de la frappe (délai de 120 ms après la dernière touche) et normalisation `19-m/042 → 19M042`. Tolérance aux fautes, filtres par filière. Tableau avec surlignage de la frappe et pastilles (ADMIS, AJOURNÉ, RATTRAPAGE, 2 PAGES LIÉES, PROCHE À 92 %, DOUBLON NIVEAU, SCAN OCR). Aperçu PDF haute définition dans un split-view 55/45. Bandeaux d'alerte (échec/rattrapage, pages liées, OCR, résultat approché). Impression directe, export PDF unitaire, timbre « Duplicata conforme », vue groupée par étudiant. | S1.1–S1.4, S2.1, S2.4, S3.3, S4.2 |
| **Cursus** | Fusion chronologique N1 → N7 en un seul PDF avec page de garde. Les échecs sont exclus quand un rattrapage ADMIS existe. Les niveaux manquants sont signalés. Un ancien matricule peut être rattaché (même nom + date de naissance). | S1.4, S2.1, S2.2 |
| **Indexation** | Robot dans un processus séparé qui ne gèle jamais l'interface. Le guichet reste utilisable pendant qu'il travaille. Conversion Word → PDF (COM Word ou LibreOffice), OCR avec redressement et seuillage Otsu. Mode différentiel, vérification d'intégrité (liens orphelins), journal d'anomalies, statistiques du fonds. | S4.1, S4.3, S4.4 |
| **Extraction & journal** | Listes pour le Décanat (filière, plage d'années, statut, dernier niveau) exportées en Excel ou CSV. Journal non modifiable des consultations, impressions, exports, duplicatas et vérifications. | S3.2, traçabilité |
| **Vérification** | Document soumis et original affichés côte à côte, comparaison champ par champ (notes, moyenne, décision), rapport PDF et journalisation. | S3.1 |

Raccourcis :

| Raccourci | Action |
|---|---|
| `Échap` / `Ctrl+F` | Nouvelle recherche |
| `↑` `↓` | Parcourir les résultats avec mise à jour de l'aperçu |
| `Ctrl+P` | Imprimer le relevé affiché |
| `Ctrl+E` | Exporter le relevé affiché |
| `Ctrl+Maj+E` | Cursus complet |
| `Ctrl+1…4` | Changer d'écran |

## Installation (poste de la Cellule Informatique, Windows 10/11)

1. **Python 3.11 ou plus récent** — https://www.python.org (cocher « Add to PATH »).
2. **Tesseract OCR** avec la langue française — https://github.com/UB-Mannheim/tesseract/wiki
   (installer dans `C:\Program Files\Tesseract-OCR`, cocher *French* dans les langues).
3. **Pour les vieux `.doc` 97-2003** : Microsoft Word installé sur le poste, **ou** LibreOffice (gratuit).
4. *(Facultatif, recommandé)* **SumatraPDF** portable : déposer `SumatraPDF.exe` dans le dossier `tools\`
   pour une impression vectorielle silencieuse. Sans lui, l'impression passe par Qt sur l'imprimante par défaut.
5. Dans le dossier du projet :

```bat
run_dev.bat
```

(crée l'environnement virtuel, installe les dépendances et lance l'application).

### Construire l'exécutable autonome

```bat
build_windows.bat
```

Le script lance les tests, puis produit `dist\FMSP_Archives\FMSP_Archives.exe`. Copiez ce dossier sur les postes du guichet : Python n'y est plus nécessaire. Tesseract et Word/LibreOffice restent nécessaires **uniquement sur le poste qui indexe**.

## Tester tout de suite, sans les vraies archives

```bat
python tools\generate_samples.py archives_demo
python main.py
```

Puis, dans l'application : **Indexation** → choisir `archives_demo` → ▶. Revenez ensuite au **Guichet** et essayez les recherches suivantes :

| Recherche | Ce qu'elle démontre |
|---|---|
| `tchoua` | Homonymes, plus `TCHOUWA` en résultat approché |
| `18-m/042` | Matricule normalisé ; cursus avec N3 en échec + rattrapage, N4 sur 2 pages, N6 manquant, corrélation avec `15B077` |
| `kouam` | Tolérance aux fautes |
| `ndoukou` | Apostrophes ignorées |
| `marie claire` | PDF scanné lu par OCR |

Le fichier `.doc` corrompu du jeu d'essai apparaît dans le journal d'anomalies, sans arrêter l'indexation.

En ligne de commande :

```bat
python main.py --index --source "\\SRV-FMSP\Archives_Releves" --only-new
python main.py --check-integrity
python -m pytest
```

## ⚠️ À faire sur les vraies archives : ajuster le parsing

Le format des relevés a changé au fil des années. Toutes les règles de lecture sont regroupées dans
**`core/parsing.py` → `PATTERNS`** : libellés « Matricule », « Noms et Prénoms », « Niveau », « Décision du jury », etc.

Méthode conseillée :

1. Indexer **un échantillon** (2–3 fichiers par année et par format).
2. Consulter le journal d'anomalies : « nom non détecté », « année non détectée », « aucun relevé détecté ».
3. Adapter les expressions de `PATTERNS` et ajouter un test dans `tests/test_core.py` avec un extrait réel.
4. Lancer une réindexation complète.

L'arborescence aide aussi : année (`2020-2021`), filière (`Medecine`, `Pharmacie`…), session (`SN`/`SR`)
et niveau (`N3`) sont lus dans le chemin quand le document ne les mentionne pas (voir `hints_from_path`).

## Architecture

```
fmsp_archive_app/
├── main.py                    # interface, ou robot d'indexation avec --index
├── core/                      # métier, sans dépendance à l'interface
│   ├── config.py              # réglages (%APPDATA%\FMSP_Archives\config.json)
│   ├── normalize.py           # matricules 18M042 / 18-M-042 / 18/M/042, noms sans accents
│   ├── database.py            # SQLite WAL, FTS5 trigram, recherche floue rapidfuzz, journal
│   ├── extractors.py          # PDF, OCR (deskew + Otsu), Word → PDF (COM / LibreOffice), .docx
│   ├── parsing.py             # blocs étudiant, relevés sur plusieurs pages, métadonnées
│   ├── indexer.py             # robot (processus séparé, JSON lines, différentiel, intégrité)
│   ├── pdf_tools.py           # découpe, fusion du cursus, timbre Duplicata
│   ├── printer_service.py     # impression silencieuse (SumatraPDF / PDFtoPrinter / Qt)
│   ├── exporter.py            # Excel / CSV
│   └── verification.py        # comparaison document soumis / original, rapport PDF
├── ui/                        # PySide6
│   ├── theme_fmsp.qss         # Design System v3.1
│   ├── main_window.py         # rail de navigation, raccourcis globaux
│   ├── guichet_page.py        # Zones A/B/C (Omnibox, tableau, aperçu)
│   ├── omnibox_widget.py · results_table_model.py · pdf_preview_panel.py
│   ├── cursus_dialog.py · indexing_page.py · extraction_page.py · verification_page.py
│   └── widgets.py · icons.py · settings_dialog.py
├── tools/generate_samples.py  # faux fonds d'archives pour tester
├── tools/screenshots.py       # captures automatiques
└── tests/test_core.py         # 18 tests (normalisation, parsing, indexation de bout en bout)
```

**Règles métier respectées**

- **Non-destruction.** Le dossier des archives est seulement lu. Les conversions Word passent par une copie temporaire ou par Word en lecture seule, et les PDF générés vont dans le cache ou sur le Bureau.
- **Données locales.** La base et le cache sont dans `%APPDATA%\FMSP_Archives` (modifiable dans Réglages). Rien n'est envoyé sur un cloud.
- **Traçabilité.** Chaque consultation, impression, export, duplicata, cursus, extraction et vérification est inscrit dans la table `journal`.

**Écarts corrigés par rapport au plan directeur**

- **Schéma SQL.** `VARCHAR INDEXED` n'existe pas en SQLite : les index sont créés séparément. La table FTS5 est synchronisée par des triggers.
- **Numéros de page Word.** Un document Word n'a pas de pages fixes. Chaque Word est donc converti en PDF à l'indexation, ce qui donne à la fois les **vrais** numéros de page et un aperçu identique.
- **Levenshtein.** SQLite ne le propose pas : la recherche combine FTS5/`LIKE` et `rapidfuzz`.

## Limites connues / suite possible

- Testé ici sous Linux sur le jeu d'essai, avec LibreOffice et Tesseract. **Ce qui doit être validé sur un poste Windows réel :**
  - la passerelle COM Word ;
  - l'impression (spouleur Windows) ;
  - l'exécutable PyInstaller.
- La vérification d'authenticité compare le **texte**. Une falsification de la mise en page seule se voit sur l'aperçu côte à côte, pas dans le tableau.
- Phase 2 prévue par le plan : version web interne (FastAPI + React) réutilisant `core/` tel quel.
