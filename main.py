"""Point d'entrée : interface graphique, ou robot d'indexation en ligne de commande.

    python main.py                         -> guichet (interface graphique)
    python main.py --index --source DOSSIER [--only-new] [--no-ocr]
    python main.py --check-integrity
"""
from __future__ import annotations

import sys


def main() -> int:
    if any(a in sys.argv[1:] for a in ("--index", "--check-integrity")):
        from core.indexer import cli
        return cli(sys.argv[1:])
    from ui.app import run_gui
    return run_gui(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
