"""Fixtures partagées : un faux fonds d'archives (données fictives) indexé une fois pour toute la session."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tools", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("SCOLARIX_SOUND", "0")

HAS_TESSERACT = bool(shutil.which("tesseract")) or Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe").exists()
HAS_CONVERTER = bool(shutil.which("soffice") or shutil.which("libreoffice")
                     or Path(r"C:\Program Files\LibreOffice\program\soffice.exe").exists())

from core.config import Settings  # noqa: E402
from core.database import Database  # noqa: E402
from core.indexer import Indexer  # noqa: E402


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    base = tmp_path_factory.mktemp("scolarix_data")
    os.environ["FMSP_DATA_DIR"] = str(base)
    return base


def make_settings(base: Path, **kw) -> Settings:
    kw.setdefault("cache_dir", str(base / "cache"))
    kw.setdefault("db_path", str(base / "index.db"))
    kw.setdefault("ocr_enabled", HAS_TESSERACT)
    return Settings(**kw)


@pytest.fixture(scope="session")
def indexed(tmp_path_factory, data_dir):
    """(db, settings, dossier d'archives, compteurs) — indexation PARALLÈLE (2 processus)."""
    import generate_samples
    base = tmp_path_factory.mktemp("fmsp")
    archives = base / "archives"
    generate_samples.generate(archives)
    settings = make_settings(base)
    db = Database(settings.db_path, cache_dir=settings.resolved_cache_dir())
    counters = Indexer(settings, db).run(archives, only_new=False, workers=2)
    return db, settings, archives, counters


@pytest.fixture()
def demo_archives(tmp_path):
    """Un petit fonds d'archives jetable (pour les tests qui le modifient)."""
    import generate_samples
    archives = tmp_path / "archives"
    generate_samples.generate(archives)
    return archives
