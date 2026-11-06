"""Accès à la base d'index SQLite (mode WAL + FTS5).

Corrections par rapport au schéma du plan directeur :
- SQLite n'accepte pas ``VARCHAR(30) INDEXED`` : les index sont créés séparément ;
- la table FTS5 ``content=`` est synchronisée par des triggers ;
- le tokenizer ``trigram`` (SQLite >= 3.34) permet la recherche partielle ;
  la tolérance aux fautes (distance d'édition) est faite en Python avec rapidfuzz.

Règle de non-destruction : ce module n'écrit JAMAIS dans le dossier des archives.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from rapidfuzz import fuzz, process

from .normalize import looks_like_matricule, norm_matricule, norm_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    cle    TEXT PRIMARY KEY,
    valeur TEXT
);

CREATE TABLE IF NOT EXISTS fichiers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chemin          TEXT NOT NULL UNIQUE,      -- chemin absolu du fichier source
    type_fichier    TEXT NOT NULL,             -- DOCX, DOC, PDF_TEXT, PDF_SCAN
    taille          INTEGER,
    mtime           REAL,
    nb_pages        INTEGER DEFAULT 0,
    cache_pdf       TEXT,                      -- PDF d'aperçu (Word converti) ; NULL si la source est un PDF
    statut          TEXT DEFAULT 'OK',         -- OK, ERREUR
    message         TEXT,
    date_indexation TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS etudiants_releves (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fichier_id       INTEGER NOT NULL REFERENCES fichiers(id) ON DELETE CASCADE,
    matricule        VARCHAR(30) NOT NULL,     -- normalisé : 18M042
    matricule_brut   VARCHAR(30),              -- tel qu'écrit dans le document
    nom              VARCHAR(255),
    prenom           VARCHAR(255),
    nom_complet      VARCHAR(255) NOT NULL,
    nom_norm         VARCHAR(255) NOT NULL,    -- majuscules sans accents ni apostrophes
    date_naissance   VARCHAR(20),
    filiere          VARCHAR(100),
    filiere_code     VARCHAR(5),
    niveau           VARCHAR(20),
    niveau_num       INTEGER DEFAULT 0,
    annee_academique VARCHAR(20),
    semestre         INTEGER,                  -- 1..14 (LMD : un relevé = un semestre)
    semestre_fin     INTEGER,                  -- = semestre ; semestre + 1 pour un relevé annuel
    cycle            VARCHAR(20),              -- LICENCE, MASTER, DOCTORAT
    option_filiere   VARCHAR(60),
    session          VARCHAR(20),              -- Normale / Rattrapage (si indiqué)
    statut           VARCHAR(30),              -- ADMIS, AJOURNÉ, EXCLUSION, ABANDON, REPORT...
    decision         VARCHAR(80),              -- texte exact : « SEMESTRE VALIDE »...
    moyenne          VARCHAR(10),
    credits          INTEGER,
    grade            VARCHAR(4),
    mgp              VARCHAR(6),
    lieu_naissance   VARCHAR(80),
    numero_page      INTEGER NOT NULL DEFAULT 1,
    page_fin         INTEGER NOT NULL DEFAULT 1,
    ocr_confiance    REAL,
    extrait          TEXT,
    date_indexation  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_releves_matricule ON etudiants_releves(matricule);
CREATE INDEX IF NOT EXISTS idx_releves_nom       ON etudiants_releves(nom_norm);
CREATE INDEX IF NOT EXISTS idx_releves_annee     ON etudiants_releves(annee_academique);
CREATE INDEX IF NOT EXISTS idx_releves_fichier   ON etudiants_releves(fichier_id);

CREATE TABLE IF NOT EXISTS anomalies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    horodatage TEXT DEFAULT (datetime('now','localtime')),
    niveau     TEXT NOT NULL,                  -- ERREUR, AVERTISSEMENT, INFO
    chemin     TEXT,
    message    TEXT NOT NULL,
    action     TEXT
);

CREATE TABLE IF NOT EXISTS journal (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    horodatage TEXT DEFAULT (datetime('now','localtime')),
    operateur  TEXT,
    poste      TEXT,
    action     TEXT NOT NULL,                  -- CONSULTATION, IMPRESSION, EXPORT PDF, DUPLICATA, CURSUS, EXTRACTION, VERIFICATION
    releve_id  INTEGER,
    matricule  TEXT,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_date ON journal(horodatage);
CREATE INDEX IF NOT EXISTS idx_journal_mat  ON journal(matricule);
"""

FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS releves_ai AFTER INSERT ON etudiants_releves BEGIN
  INSERT INTO etudiants_fts(rowid, matricule, nom_norm) VALUES (new.id, new.matricule, new.nom_norm);
END;
CREATE TRIGGER IF NOT EXISTS releves_ad AFTER DELETE ON etudiants_releves BEGIN
  INSERT INTO etudiants_fts(etudiants_fts, rowid, matricule, nom_norm) VALUES ('delete', old.id, old.matricule, old.nom_norm);
END;
CREATE TRIGGER IF NOT EXISTS releves_au AFTER UPDATE ON etudiants_releves BEGIN
  INSERT INTO etudiants_fts(etudiants_fts, rowid, matricule, nom_norm) VALUES ('delete', old.id, old.matricule, old.nom_norm);
  INSERT INTO etudiants_fts(rowid, matricule, nom_norm) VALUES (new.id, new.matricule, new.nom_norm);
END;
"""

RELEVE_COLUMNS = (
    "r.id, r.fichier_id, r.matricule, r.matricule_brut, r.nom, r.prenom, r.nom_complet, r.nom_norm, "
    "r.date_naissance, r.filiere, r.filiere_code, r.niveau, r.niveau_num, r.annee_academique, r.session, "
    "r.statut, r.moyenne, r.numero_page, r.page_fin, r.ocr_confiance, r.extrait, "
    "r.semestre, r.decision, r.credits, r.grade, r.mgp, r.lieu_naissance, r.semestre_fin, r.cycle, r.option_filiere, "
    "f.chemin, f.type_fichier, f.cache_pdf, f.statut AS fichier_statut"
)


@dataclass
class Releve:
    id: int
    fichier_id: int
    matricule: str
    matricule_brut: str | None
    nom: str | None
    prenom: str | None
    nom_complet: str
    nom_norm: str
    date_naissance: str | None
    filiere: str | None
    filiere_code: str | None
    niveau: str | None
    niveau_num: int
    annee_academique: str | None
    session: str | None
    statut: str | None
    moyenne: str | None
    numero_page: int
    page_fin: int
    ocr_confiance: float | None
    extrait: str | None
    semestre: int | None
    decision: str | None
    credits: int | None
    grade: str | None
    mgp: str | None
    lieu_naissance: str | None
    semestre_fin: int | None
    cycle: str | None
    option_filiere: str | None
    chemin: str
    type_fichier: str
    cache_pdf: str | None
    fichier_statut: str | None
    score: int = 100            # 100 = correspondance exacte ; < 100 = résultat approché
    flags: list[str] = field(default_factory=list)

    @property
    def nb_pages(self) -> int:
        return max(1, self.page_fin - self.numero_page + 1)

    @property
    def pages_label(self) -> str:
        if self.nb_pages > 1:
            return f"Pages {self.numero_page}–{self.page_fin}"
        return f"Page {self.numero_page}"

    @property
    def preview_pdf(self) -> str | None:
        """PDF à afficher/imprimer : la source si c'est un PDF, sinon le cache converti."""
        if self.type_fichier.startswith("PDF"):
            return self.chemin
        return self.cache_pdf

    @property
    def is_success(self) -> bool:
        return (self.statut or "").upper().startswith("ADMIS") or (self.statut or "").upper() == "VALIDÉ"

    @property
    def periode(self) -> str:
        """« Niveau 2 · S3 » (ou « Niveau 2 » si le semestre est inconnu)."""
        base = self.niveau or "Niveau ?"
        if self.semestre and self.semestre_fin and self.semestre_fin != self.semestre:
            return f"{base} · S{self.semestre}-S{self.semestre_fin} (annuel)"
        return f"{base} · S{self.semestre}" if self.semestre else base

    @property
    def decision_deduite(self) -> bool:
        """Ancien format (2012-2014) : pas de décision semestrielle imprimée, elle est déduite de la colonne JURY."""
        return bool(self.decision and re.match(r"^\d+ UE ", self.decision))

    @property
    def date_connue(self) -> str | None:
        """Date de naissance, sauf les dates fictives des gabarits (00/01/1900…)."""
        d = self.date_naissance or ""
        if not d or d.startswith("00") or d[3:5] == "00" or d.endswith("1900"):
            return None
        return d

    @property
    def is_word(self) -> bool:
        return self.type_fichier in ("DOC", "DOCX")

    @property
    def annuel(self) -> bool:
        return bool(self.semestre and self.semestre_fin and self.semestre_fin != self.semestre)

    @property
    def periode_key(self) -> tuple:
        return (self.niveau_num, self.semestre or 0)


def _row_to_releve(row: sqlite3.Row, cache_dir: Path | None = None) -> Releve:
    rel = Releve(**{k: row[k] for k in row.keys()})
    rel.cache_pdf = resolve_cache(rel.cache_pdf, cache_dir)
    return rel


def _word_rank(nom_norm: str, words: list[str]) -> int:
    """0 = chaque mot cherché est un mot entier du nom, 1 = début de mot, 2 = simple sous-chaîne."""
    toks = nom_norm.split()
    rank = 0
    for w in words:
        if w in toks:
            continue
        rank = max(rank, 1 if any(t.startswith(w) for t in toks) else 2)
    return rank


def _result_order(rels: list, words: list[str]):
    """Tri des résultats : les relevés d'un même étudiant restent groupés et chronologiques
    (S1, S2, S2 redoublé…), quel que soit l'orthographe du nom dans chaque fichier. Les étudiants
    sont classés par pertinence (score, mot entier > début de mot > sous-chaîne), puis par nom."""
    best: dict[str, tuple] = {}
    latest: dict[str, object] = {}
    for r in rels:
        key = (-r.score, _word_rank(r.nom_norm, words) if words else 0)
        best[r.matricule] = min(best.get(r.matricule, key), key)
        cur = latest.get(r.matricule)
        if cur is None or (r.annee_academique or "", r.niveau_num) > (cur.annee_academique or "", cur.niveau_num):
            latest[r.matricule] = r

    def key(r):
        return (best[r.matricule], latest[r.matricule].nom_norm, r.matricule, r.niveau_num, r.semestre or 0,
                r.annee_academique or "", 1 if (r.session or "").lower().startswith("rattrap") else 0)
    return key


def missing_periods(rels: list) -> list[str]:
    """Périodes attendues mais absentes du dossier d'un étudiant (S1 … dernier semestre trouvé ;
    un relevé annuel couvre ses deux semestres). Sert à la vue groupée, au cursus et à sa page de garde."""
    sems: set[int] = set()
    for r in rels:
        if r.semestre:
            sems.update(range(r.semestre, (r.semestre_fin or r.semestre) + 1))
    if sems:
        return [f"S{n}" for n in range(1, max(sems) + 1) if n not in sems]
    nums = {r.niveau_num for r in rels if 0 < r.niveau_num < 100}
    if not nums:
        return []
    return [f"Niveau {n}" for n in range(1, max(nums) + 1) if n not in nums]


def resolve_cache(value: str | None, cache_dir: Path | None) -> str | None:
    """Le cache d'aperçu est enregistré par son seul nom de fichier, résolu dans le dossier de cache
    du poste : la base peut ainsi être copiée sur un autre poste du guichet avec son dossier de données.
    Les anciennes bases (chemin absolu) restent lisibles."""
    if not value:
        return None
    p = Path(value)
    if not p.is_absolute():
        return str((cache_dir or Path(".")) / p)
    if not p.exists() and cache_dir is not None and (cache_dir / p.name).exists():
        return str(cache_dir / p.name)
    return value


class Database:
    """Connexion à la base d'index. Une instance par thread/processus."""

    def __init__(self, path: str | Path, cache_dir: str | Path | None = None):
        self.path = Path(path)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self._lock = threading.RLock()
        self._token_index: dict[str, set[str]] | None = None
        self._token_index_count = -1
        self.fts_mode = "none"
        self._create_schema()

    # ------------------------------------------------------------------ schéma
    def _create_schema(self) -> None:
        with self._lock:
            self.conn.executescript(SCHEMA)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(etudiants_releves)")}
            for col, decl in (("semestre", "INTEGER"), ("decision", "VARCHAR(80)"), ("credits", "INTEGER"),
                              ("grade", "VARCHAR(4)"), ("mgp", "VARCHAR(6)"), ("lieu_naissance", "VARCHAR(80)"),
                              ("semestre_fin", "INTEGER"), ("cycle", "VARCHAR(20)"), ("option_filiere", "VARCHAR(60)")):
                if col not in cols:  # base créée par une version précédente
                    self.conn.execute(f"ALTER TABLE etudiants_releves ADD COLUMN {col} {decl}")
            row = self.conn.execute("SELECT valeur FROM meta WHERE cle='fts_mode'").fetchone()
            if row:
                self.fts_mode = row[0]
            else:
                for tokenizer, mode in (("trigram", "trigram"), ("unicode61", "unicode61")):
                    try:
                        self.conn.execute(
                            "CREATE VIRTUAL TABLE IF NOT EXISTS etudiants_fts USING fts5("
                            "matricule, nom_norm, content='etudiants_releves', content_rowid='id', "
                            f"tokenize='{tokenizer}')"
                        )
                        self.fts_mode = mode
                        break
                    except sqlite3.OperationalError:
                        continue
                self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('fts_mode', ?)", (self.fts_mode,))
            if self.fts_mode != "none":
                self.conn.executescript(FTS_TRIGGERS)
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', '3')")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- fichiers
    def file_state(self, chemin: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM fichiers WHERE chemin=?", (chemin,)).fetchone()

    def file_signatures(self) -> dict[str, tuple[int, float]]:
        """{chemin: (taille, mtime)} de tous les fichiers indexés (une seule requête pour le différentiel)."""
        return {r[0]: (r[1], r[2]) for r in self.conn.execute("SELECT chemin, taille, mtime FROM fichiers")}

    def store_result(self, chemin: str, type_fichier: str, taille: int, mtime: float, nb_pages: int,
                     cache_pdf: str | None, statut: str, message: str | None, releves: Iterable[dict],
                     anomalies: Iterable[tuple]) -> int:
        """Enregistre un fichier, ses relevés et (si fournies) ses anomalies dans une seule transaction."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN")
            try:
                fid = self._save_file_tx(cur, chemin, type_fichier, taille, mtime, nb_pages, cache_pdf, statut,
                                         message, releves)
                if anomalies is not None:
                    cur.execute("DELETE FROM anomalies WHERE chemin=?", (chemin,))
                    cur.executemany("INSERT INTO anomalies(niveau, chemin, message, action) VALUES (?,?,?,?)",
                                    [(niveau, chemin, msg, action) for niveau, msg, action in anomalies])
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        self._token_index = None
        return fid

    def releves_of_file(self, fichier_id: int) -> list[Releve]:
        return self._select("r.fichier_id = ?", [fichier_id], order="r.numero_page, r.id")

    def attach_preview(self, fichier_id: int, cache_name: str, pages: dict[int, tuple[int, int]]) -> None:
        """Enregistre le PDF d'aperçu produit à la demande et les numéros de page exacts qu'il donne."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN")
            try:
                cur.execute("UPDATE fichiers SET cache_pdf=? WHERE id=?", (cache_name, fichier_id))
                for rid, (debut, fin) in pages.items():
                    cur.execute("UPDATE etudiants_releves SET numero_page=?, page_fin=? WHERE id=?", (debut, fin, rid))
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def all_files(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM fichiers ORDER BY chemin").fetchall()

    def save_file(self, chemin: str, type_fichier: str, taille: int, mtime: float, nb_pages: int,
                  cache_pdf: str | None, statut: str, message: str | None,
                  releves: Iterable[dict]) -> int:
        """Remplace (atomiquement) l'enregistrement d'un fichier et ses relevés."""
        return self.store_result(chemin, type_fichier, taille, mtime, nb_pages, cache_pdf, statut, message, releves, None)

    def _save_file_tx(self, cur, chemin, type_fichier, taille, mtime, nb_pages, cache_pdf, statut, message,
              releves) -> int:
        old = cur.execute("SELECT id FROM fichiers WHERE chemin=?", (chemin,)).fetchone()
        if old:
            cur.execute("DELETE FROM etudiants_releves WHERE fichier_id=?", (old["id"],))
            cur.execute(
                "UPDATE fichiers SET type_fichier=?, taille=?, mtime=?, nb_pages=?, cache_pdf=?, statut=?, "
                "message=?, date_indexation=datetime('now','localtime') WHERE id=?",
                (type_fichier, taille, mtime, nb_pages, cache_pdf, statut, message, old["id"]),
            )
            fid = old["id"]
        else:
            cur.execute(
                "INSERT INTO fichiers(chemin, type_fichier, taille, mtime, nb_pages, cache_pdf, statut, message) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (chemin, type_fichier, taille, mtime, nb_pages, cache_pdf, statut, message),
            )
            fid = cur.lastrowid
        for r in releves:
            cur.execute(
                "INSERT INTO etudiants_releves(fichier_id, matricule, matricule_brut, nom, prenom, nom_complet, "
                "nom_norm, date_naissance, filiere, filiere_code, niveau, niveau_num, annee_academique, session, "
                "statut, moyenne, numero_page, page_fin, ocr_confiance, extrait, semestre, decision, credits, grade, mgp, "
                "lieu_naissance, semestre_fin, cycle, option_filiere) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, r["matricule"], r.get("matricule_brut"), r.get("nom"), r.get("prenom"),
                 r["nom_complet"], norm_text(r["nom_complet"]), r.get("date_naissance"), r.get("filiere"),
                 r.get("filiere_code"), r.get("niveau"), r.get("niveau_num", 0), r.get("annee_academique"),
                 r.get("session"), r.get("statut"), r.get("moyenne"), r["numero_page"], r["page_fin"],
                 r.get("ocr_confiance"), r.get("extrait"), r.get("semestre"), r.get("decision"),
                 r.get("credits"), r.get("grade"), r.get("mgp"), r.get("lieu_naissance"),
                 r.get("semestre_fin", r.get("semestre")), r.get("cycle"), r.get("option_filiere")),
            )
        return fid

    def remove_file(self, chemin: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM fichiers WHERE chemin=?", (chemin,))
            self.conn.commit()
        self._token_index = None

    def orphan_files(self) -> list[sqlite3.Row]:
        """Fichiers indexés dont le chemin n'existe plus (dossier déplacé ou renommé)."""
        return [r for r in self.all_files() if not Path(r["chemin"]).exists()]

    # --------------------------------------------------------------- anomalies
    def add_anomaly(self, niveau: str, chemin: str | None, message: str, action: str | None = None) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO anomalies(niveau, chemin, message, action) VALUES (?,?,?,?)",
                              (niveau, chemin, message, action))
            self.conn.commit()

    def anomalies(self, niveau: str | None = None, limit: int = 500) -> list[sqlite3.Row]:
        if niveau:
            return self.conn.execute("SELECT * FROM anomalies WHERE niveau=? ORDER BY id DESC LIMIT ?",
                                     (niveau, limit)).fetchall()
        return self.conn.execute("SELECT * FROM anomalies ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def clear_anomalies_for(self, chemin: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM anomalies WHERE chemin=?", (chemin,))
            self.conn.commit()

    def clear_anomalies(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM anomalies")
            self.conn.commit()

    # ----------------------------------------------------------------- journal
    def log(self, action: str, operateur: str, poste: str, releve_id: int | None = None,
            matricule: str | None = None, detail: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO journal(operateur, poste, action, releve_id, matricule, detail) VALUES (?,?,?,?,?,?)",
                (operateur, poste, action, releve_id, matricule, detail))
            self.conn.commit()

    def journal(self, matricule: str | None = None, since: str | None = None, limit: int = 1000) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM journal WHERE 1=1", []
        if matricule:
            sql += " AND matricule LIKE ?"
            args.append(f"%{norm_matricule(matricule)}%")
        if since:
            sql += " AND horodatage >= ?"
            args.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return self.conn.execute(sql, args).fetchall()

    # ------------------------------------------------------------------- meta
    def get_meta(self, cle: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT valeur FROM meta WHERE cle=?", (cle,)).fetchone()
        return row[0] if row else default

    def set_meta(self, cle: str, valeur: str) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (cle, valeur))
            self.conn.commit()

    # -------------------------------------------------------------- recherche
    def _select(self, where: str, args: list, order: str = "", limit: int | None = None) -> list[Releve]:
        sql = f"SELECT {RELEVE_COLUMNS} FROM etudiants_releves r JOIN fichiers f ON f.id = r.fichier_id WHERE {where}"
        if order:
            sql += f" ORDER BY {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [_row_to_releve(row, self.cache_dir) for row in self.conn.execute(sql, args).fetchall()]

    def get(self, releve_id: int) -> Releve | None:
        res = self._select("r.id = ?", [releve_id])
        return res[0] if res else None

    def count_releves(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM etudiants_releves").fetchone()[0]

    def _get_token_index(self) -> dict[str, set[str]]:
        n = self.count_releves()
        if self._token_index is None or n != self._token_index_count:
            index: dict[str, set[str]] = {}
            for (name,) in self.conn.execute("SELECT DISTINCT nom_norm FROM etudiants_releves"):
                for tok in name.split():
                    if len(tok) >= 2:
                        index.setdefault(tok, set()).add(name)
            self._token_index = index
            self._token_index_count = n
        return self._token_index

    def _fuzzy_names(self, qn: str, threshold: int) -> dict[str, int]:
        """Noms dont chaque mot de la requête ressemble (distance d'édition) à un mot du nom."""
        tokens = [t for t in qn.split() if len(t) >= 3]
        if not tokens:
            return {}
        index = self._get_token_index()
        vocab = list(index)
        per_token: list[dict[str, int]] = []
        for t in tokens:
            found: dict[str, int] = {}
            for tok, score, _ in process.extract(t, vocab, scorer=fuzz.ratio, score_cutoff=threshold, limit=60):
                for name in index[tok]:
                    found[name] = max(found.get(name, 0), int(round(score)))
            # Noms collés dans certains fichiers (« PRENOMNOM ») : le mot cherché est une PARTIE du mot indexé.
            glued = [v for v in vocab if len(v) >= len(t) + 3]
            for tok, score, _ in process.extract(t, glued, scorer=fuzz.partial_ratio, score_cutoff=threshold - 5,
                                                 limit=60):
                for name in index[tok]:
                    found[name] = max(found.get(name, 0), int(round(score)) - 5)
            per_token.append(found)
        common = set(per_token[0])
        for d in per_token[1:]:
            common &= set(d)
        return {name: round(sum(d[name] for d in per_token) / len(per_token)) for name in common}

    def search(self, query: str, filiere_code: str | None = None, tolerant: bool = True,
               threshold: int = 80, limit: int = 500) -> list[Releve]:
        q = (query or "").strip()
        if not q:
            return []
        qm = norm_matricule(q)
        years = re.findall(r"\b((?:19|20)\d{2})\b", q)
        clauses, args = [], []

        def matricule_clause(t: str) -> None:
            m2 = re.fullmatch(r"(\d{2}[A-Z]{1,4})(\d{2})", t)
            if m2:   # zéro omis : « 98MP12 » -> 98MP012 (ou un préfixe de 98MP12x)
                clauses.append("(r.matricule LIKE ? OR r.matricule = ?)")
                args.extend([f"{t}%", f"{m2.group(1)}0{m2.group(2)}"])
            elif re.fullmatch(r"[A-Z]{1,4}\d{2,4}", t):   # sans l'année : « MP112 »
                clauses.append("r.matricule LIKE ?")
                args.append(f"%{t}")
            else:
                clauses.append("r.matricule LIKE ?")
                args.append(f"{t}%")

        words: list[str] = []
        if looks_like_matricule(q) or (re.fullmatch(r"\d{2}[A-Z]{1,4}\d{0,4}", qm) and not years):
            # 1) Matricule complet ou préfixe (« 18M0 », « 19-m/042 », « 19 MP 031 »)
            matricule_clause(qm)
        elif qm.isdigit() and not years:
            # Fin de matricule seule (« 042 »)
            clauses.append("r.matricule LIKE ?")
            args.append(f"%{qm}%")
        else:
            rest = []
            for tok in q.split():
                t = norm_matricule(tok)
                if re.fullmatch(r"\d{2}[A-Z]{1,4}\d{1,4}|[A-Z]{1,4}\d{3,4}", t) and not re.fullmatch(r"(?:19|20)\d{2}", t):
                    matricule_clause(t)          # saisie mixte : « 98MP112 NOM », « 98MP112 2020 »
                else:
                    rest.append(tok)
            words = [w for w in norm_text(" ".join(rest)).split() if not re.fullmatch(r"\d+", w)]
            # 2) Année(s) académique(s) ET 3) chaque mot du nom
            for y in years:
                clauses.append("r.annee_academique LIKE ?")
                args.append(f"%{y}%")
            for w in words:
                if len(w) < 3:   # mot très court : mot entier seulement (évite « A » -> 115 résultats)
                    clauses.append("(' ' || r.nom_norm || ' ') LIKE ?")
                    args.append(f"% {w} %")
                else:
                    clauses.append("r.nom_norm LIKE ?")
                    args.append(f"%{w}%")
        has_matricule = any("r.matricule" in c for c in clauses)
        fuzzy_words = [] if has_matricule else [w for w in words if len(w) >= 3]
        exact: list[Releve] = []
        if clauses:
            where = "(" + " AND ".join(clauses) + ")"
            fargs = list(args)
            if filiere_code:
                where += " AND r.filiere_code = ?"
                fargs.append(filiere_code)
            exact = self._select(where, fargs, limit=limit)

        results = {r.id: r for r in exact}
        # 4) Tolérance aux fautes (KOUAM / KOUAME, TCHOUA / TCHOUWA...)
        if tolerant and fuzzy_words:
            scores = self._fuzzy_names(" ".join(fuzzy_words), threshold)
            names = list(scores)
            if names:
                placeholders = ",".join("?" * len(names))
                where = f"r.nom_norm IN ({placeholders})"
                fargs = list(names)
                for y in years:
                    where += " AND r.annee_academique LIKE ?"
                    fargs.append(f"%{y}%")
                if filiere_code:
                    where += " AND r.filiere_code = ?"
                    fargs.append(filiere_code)
                for r in self._select(where, fargs, limit=limit):
                    if r.id not in results:
                        r.score = scores[r.nom_norm]
                        results[r.id] = r

        out = list(results.values())
        out.sort(key=_result_order(out, words))
        self._flag(out)
        return out[:limit]

    def _flag(self, rels: list[Releve]) -> None:
        """Ajoute les drapeaux visuels : rattrapage, 2 pages, doublon de niveau, scan, score flou."""
        keys: dict[tuple, int] = {}
        mats = {r.matricule for r in rels}
        if mats:
            ph = ",".join("?" * len(mats))
            for row in self.conn.execute(
                    f"SELECT matricule, niveau, IFNULL(semestre, 0) sem, COUNT(*) c FROM etudiants_releves "
                    f"WHERE matricule IN ({ph}) GROUP BY matricule, niveau, IFNULL(semestre, 0)", list(mats)):
                keys[(row["matricule"], row["niveau"], row["sem"])] = row["c"]
        for r in rels:
            r.flags = []
            if r.score < 100:
                r.flags.append(f"PROCHE À {r.score} %")
            if (r.session or "").lower().startswith("rattrap"):
                r.flags.append("RATTRAPAGE")
            if r.nb_pages > 1:
                r.flags.append(f"{r.nb_pages} PAGES LIÉES")
            n_same = keys.get((r.matricule, r.niveau, r.semestre or 0), 1)
            if n_same > 1:
                r.flags.append(f"{n_same} RELEVÉS S{r.semestre}" if r.semestre else "DOUBLON NIVEAU")
            if r.type_fichier == "PDF_SCAN":
                r.flags.append("SCAN OCR")
            if r.fichier_statut == "ERREUR" or not Path(r.chemin).exists():
                r.flags.append("FICHIER INTROUVABLE")

    def level_siblings(self, rel: Releve) -> list[Releve]:
        """Autres relevés du même matricule pour le même niveau ET le même semestre
        (session normale / rattrapage, redoublement, doublon d'archive)."""
        return self._select("r.matricule = ? AND r.niveau IS ? AND IFNULL(r.semestre, 0) = ? AND r.id <> ?",
                            [rel.matricule, rel.niveau, rel.semestre or 0, rel.id])

    # ------------------------------------------------------------ cursus
    def cursus(self, matricule: str) -> list[Releve]:
        rels = self._select("r.matricule = ?", [norm_matricule(matricule)])
        rels.sort(key=lambda r: (r.niveau_num, r.semestre or 0, r.annee_academique or "",
                                 1 if (r.session or "").lower().startswith("rattrap") else 0))
        self._flag(rels)
        return rels

    def correlated_matricules(self, matricule: str) -> list[dict]:
        """Autres matricules portés par la même personne (même nom complet, et même date de naissance si connue)."""
        base = self._select("r.matricule = ?", [norm_matricule(matricule)])
        if not base:
            return []
        name = base[0].nom_norm
        dob = next((r.date_naissance for r in base if r.date_naissance), None)
        rows = self.conn.execute(
            "SELECT matricule, MIN(filiere) filiere, COUNT(*) n, MAX(date_naissance) dob FROM etudiants_releves "
            "WHERE nom_norm = ? AND matricule <> ? GROUP BY matricule", (name, base[0].matricule)).fetchall()
        out = []
        for row in rows:
            if dob and row["dob"] and row["dob"] != dob:
                continue
            out.append({"matricule": row["matricule"], "filiere": row["filiere"], "n": row["n"],
                        "meme_naissance": bool(dob and row["dob"] == dob)})
        return out

    # --------------------------------------------------------- statistiques
    def stats(self) -> dict:
        c = self.conn
        one = lambda sql: c.execute(sql).fetchone()[0] or 0  # noqa: E731
        return {
            "releves": one("SELECT COUNT(*) FROM etudiants_releves"),
            "etudiants": one("SELECT COUNT(DISTINCT matricule) FROM etudiants_releves"),
            "pages": one("SELECT SUM(nb_pages) FROM fichiers"),
            "fichiers": one("SELECT COUNT(*) FROM fichiers"),
            "multi_pages": one("SELECT COUNT(*) FROM etudiants_releves WHERE page_fin > numero_page"),
            "echecs": one("SELECT COUNT(*) FROM fichiers WHERE statut='ERREUR'"),
            "formats": {row[0]: row[1] for row in c.execute(
                "SELECT type_fichier, COUNT(*) FROM fichiers GROUP BY type_fichier ORDER BY 2 DESC")},
            "annees": [(row[0], row[1]) for row in c.execute(
                "SELECT annee_academique, COUNT(*) FROM etudiants_releves WHERE annee_academique IS NOT NULL "
                "GROUP BY annee_academique ORDER BY annee_academique")],
        }

    # ------------------------------------------------------------ extraction
    def extraction(self, filiere_code: str | None, year_from: int | None, year_to: int | None,
                   statut: str = "tous", dernier_niveau: bool = False) -> list[Releve]:
        where, args = ["1=1"], []
        if filiere_code:
            where.append("r.filiere_code = ?")
            args.append(filiere_code)
        if year_from:
            where.append("CAST(substr(r.annee_academique, 1, 4) AS INTEGER) >= ?")
            args.append(year_from)
        if year_to:
            where.append("CAST(substr(r.annee_academique, 1, 4) AS INTEGER) <= ?")
            args.append(year_to)
        rels = self._select(" AND ".join(where), args,
                            order="r.annee_academique, r.nom_norm, r.niveau_num, r.semestre")
        if dernier_niveau:
            # D'abord le DERNIER relevé de chaque étudiant (tous statuts confondus), ensuite le filtre de statut :
            # un étudiant qui a validé au redoublement n'apparaît pas parmi les « non validés ».
            best: dict[str, Releve] = {}
            for r in rels:
                cur = best.get(r.matricule)
                if cur is None or (r.niveau_num, r.semestre or 0, r.annee_academique or "", r.is_success) > \
                        (cur.niveau_num, cur.semestre or 0, cur.annee_academique or "", cur.is_success):
                    best[r.matricule] = r
            rels = sorted(best.values(), key=lambda r: (r.annee_academique or "", r.nom_norm))
        if statut == "admis":
            rels = [r for r in rels if r.is_success]
        elif statut == "non_valides":
            rels = [r for r in rels if not r.is_success]
        return rels

    def distinct_years(self) -> list[int]:
        rows = self.conn.execute(
            "SELECT DISTINCT CAST(substr(annee_academique,1,4) AS INTEGER) y FROM etudiants_releves "
            "WHERE annee_academique IS NOT NULL ORDER BY y").fetchall()
        return [r[0] for r in rows if r[0]]
