"""Archives FICTIVES indexées pour les tests de bout en bout (aucune donnée réelle)."""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
def main():
    work = Path(sys.argv[1])
    os.environ["FMSP_DATA_DIR"] = str(work / "data")

    import generate_samples  # noqa: E402
    from core.config import Settings, save_settings  # noqa: E402
    from core.database import Database  # noqa: E402
    from core.indexer import Indexer  # noqa: E402

    if work.exists():
        shutil.rmtree(work)
    archives = work / "archives"
    generate_samples.generate(archives)
    s = Settings(cache_dir=str(work / "cache"), db_path=str(work / "index.db"), source_root=str(archives),
                 operateur="Aïcha Ndongo", poste="G1", exports_root=str(work / "Exports Scolarix"),
                 ocr_enabled=bool(shutil.which("tesseract")))
    save_settings(s)
    db = Database(s.db_path, cache_dir=s.resolved_cache_dir())
    print(Indexer(s, db).run(archives, only_new=False, workers=2))


if __name__ == "__main__":     # indispensable : l'indexation parallèle relance ce module
    main()
