"""Moteur Scolarix : processus séparé, piloté par l'interface Electron.

Protocole : une ligne JSON par message, en UTF-8.
    entrée  {"id": 7, "method": "search", "params": {"q": "18M042"}}
    sortie  {"id": 7, "result": {...}}            ou  {"id": 7, "error": {"message": "...", "kind": "user"}}
    sortie  {"event": "index", "data": {...}}      (progression de l'indexation, sans id)
    sortie  {"event": "progress", "data": {"op": "...", "phase": "conversion", "i": 3, "n": 7, "message": "..."}}
                                                   (progression d'un cursus / d'une demande lancé avec « op »)

Pourquoi un processus séparé : l'interface ne fait jamais de travail lourd, donc ne se fige jamais ; et si le
moteur plante ou se bloque, l'interface le relance au lieu de tomber avec lui.

Chaque demande s'exécute dans un fil de travail (un aperçu Word de 30 s ne retarde pas une recherche).
« ping » est traité tout de suite par la boucle de lecture : il prouve que le moteur est vivant. « cancel »
aussi : l'annulation d'un cursus ne doit jamais attendre qu'un fil de travail se libère.
Fin de l'entrée standard (Electron fermé ou planté) : arrêt propre, indexation et Word compris.

    python -m engine.server             (développement)
    scolarix-engine.exe                 (installé)
    scolarix-engine.exe --index ...     (robot d'indexation, relancé par le moteur lui-même)
"""
from __future__ import annotations

import faulthandler
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("scolarix.engine")


class Engine:
    def __init__(self, out, workers: int = 8):
        from core.config import data_dir, load_settings
        from core.database import Database
        from core.preview import PreviewService
        from core.service import Service

        self._out = out
        self._out_lock = threading.Lock()
        self.settings = load_settings()
        db = Database(self.settings.resolved_db_path(), cache_dir=self.settings.resolved_cache_dir())
        self.previews = PreviewService(db, self.settings)
        self.service = Service(self.settings, self.previews)
        self.service.emit = lambda event, data: self.send({"event": event, "data": data})
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="travail")
        self.indexer: subprocess.Popen | None = None
        self._indexer_lock = threading.Lock()
        self.data_dir = data_dir()
        self.methods = self._methods()

    # ------------------------------------------------------------------ envoi
    def send(self, msg: dict) -> None:
        line = json.dumps(msg, ensure_ascii=False, default=_json_default) + "\n"
        with self._out_lock:
            self._out.write(line.encode("utf-8"))
            self._out.flush()

    # ------------------------------------------------------------------ table des méthodes
    def _methods(self) -> dict:
        s = self.service
        names = ["info", "stats", "search", "groups", "releve", "preview", "check", "print_prepare", "print_done",
                 "print_failed", "export", "cursus_info", "cursus_build", "cursus_printed", "demandes_list",
                 "demande_get", "demande_create", "demande_update", "demande_add", "demande_remove",
                 "demande_reorder", "demande_prepare", "demande_files_to_print", "demande_printed", "demande_mark",
                 "years", "extraction", "extraction_export", "journal", "anomalies", "verify_identify",
                 "verify_compare", "verify_report", "settings_get", "settings_save", "rules_defaults", "rules_test"]
        table = {n: getattr(s, n) for n in names}
        table.update({"index_start": self.index_start, "index_stop": self.index_stop,
                      "index_reset": self.index_reset, "index_impact": s.index_impact,
                      "index_status": self.index_status, "logs_dir": lambda: str(self.data_dir / "logs")})
        return table

    # ------------------------------------------------------------------ traitement
    def handle(self, msg: dict) -> None:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "ping":
            self.send({"id": mid, "result": "pong"})
            return
        if method == "cancel":
            try:
                self.send({"id": mid, "result": self.service.cancel(str(params.get("op", "")))})
            except Exception as exc:
                self.send({"id": mid, "error": {"message": f"Annulation impossible : {exc}", "kind": "internal"}})
            return
        fn = self.methods.get(method)
        if fn is None:
            self.send({"id": mid, "error": {"message": f"Méthode inconnue : {method}", "kind": "internal"}})
            return
        self.pool.submit(self._run, mid, method, fn, params)

    def _run(self, mid, method, fn, params) -> None:
        from core.extractors import ExtractionError
        from core.guard import PrintBlocked
        from core.service import ServiceError
        try:
            result = fn(**params) if isinstance(params, dict) else fn(*params)
            self.send({"id": mid, "result": result})
        except (ServiceError, ExtractionError, PrintBlocked) as exc:     # messages écrits pour l'agent
            log.info("%s : %s", method, exc)
            self.send({"id": mid, "error": {"message": str(exc), "kind": "user"}})
        except Exception as exc:
            log.exception("échec de %s(%s)", method, _short(params))
            self.send({"id": mid, "error": {"message": f"{exc.__class__.__name__} : {exc}", "kind": "internal",
                                            "trace": traceback.format_exc(limit=8)}})

    # ------------------------------------------------------------------ indexation (processus robot)
    def index_start(self, source: str, full: bool = False) -> dict:
        from core.config import save_settings
        with self._indexer_lock:
            if self.indexer and self.indexer.poll() is None:
                return {"running": True, "already": True}
            if not source or not Path(source).exists():
                from core.service import ServiceError
                raise ServiceError("Dossier source introuvable : choisissez le dossier racine des archives.")
            self.settings.source_root = source
            save_settings(self.settings)
            program = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(ROOT / "main.py")]
            args = program + ["--index", "--json", "--stdin-control", "--source", source,
                              "--db", str(self.settings.resolved_db_path()),
                              "--full" if (full or not self.settings.differential) else "--only-new"]
            if not self.settings.ocr_enabled:
                args.append("--no-ocr")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            # erreurs du robot conservées (logs/indexation.log) : jamais d'échec silencieux
            errlog = open(self.data_dir / "logs" / "indexation.log", "a", encoding="utf-8")
            errlog.write(f"\n===== {__import__('time').strftime('%Y-%m-%d %H:%M:%S')} {' '.join(args)}\n")
            errlog.flush()
            log.info("indexation lancée : %s", " ".join(args))
            self.indexer = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=errlog, env=env,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            errlog.close()
            threading.Thread(target=self._relay_index, args=(self.indexer,), daemon=True).start()
        return {"running": True}

    def index_reset(self, confirm: str = "") -> dict:
        from core.service import ServiceError
        if self.indexer and self.indexer.poll() is None:
            raise ServiceError("Une indexation est en cours : arrêtez-la avant de vider l'index.")
        return self.service.index_reset(confirm)

    def _relay_index(self, proc: subprocess.Popen) -> None:
        for raw in proc.stdout:
            try:
                ev = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            self.send({"event": "index", "data": ev})
        code = proc.wait()
        log.info("indexation terminée (code %s)", code)
        self.send({"event": "index", "data": {"type": "exit", "code": code}})

    def index_stop(self, force: bool = False) -> dict:
        proc = self.indexer
        if not proc or proc.poll() is not None:
            return {"running": False}
        if force:
            proc.kill()
            from core.extractors import kill_orphan_words
            kill_orphan_words()
        else:
            try:
                proc.stdin.write(b"stop\n")
                proc.stdin.flush()
            except OSError:
                proc.kill()
        return {"running": True, "stopping": True}

    def index_status(self) -> dict:
        return {"running": bool(self.indexer and self.indexer.poll() is None)}

    # ------------------------------------------------------------------ arrêt
    def shutdown(self) -> None:
        try:
            if self.indexer and self.indexer.poll() is None:
                self.index_stop()
                try:
                    self.indexer.wait(20)
                except subprocess.TimeoutExpired:
                    self.indexer.kill()
        finally:
            self.pool.shutdown(wait=False, cancel_futures=True)
            try:
                self.previews.close()
            except Exception:
                pass


def _json_default(o):
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def _short(params) -> str:
    text = json.dumps(params, ensure_ascii=False, default=str)
    return text if len(text) < 200 else text[:200] + "…"


def _setup_logging() -> Path:
    from core.config import data_dir
    logs = data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(logs / "moteur.log", maxBytes=2_000_000, backupCount=5,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # plantage natif (bibliothèque PDF, Word…) : la pile de chaque fil est écrite dans ce fichier
    crash = open(logs / "moteur_plantages.log", "a", encoding="utf-8")
    faulthandler.enable(file=crash, all_threads=True)
    return logs


def serve(stdin=None, stdout=None) -> int:
    real_out = stdout or sys.stdout.buffer
    sys.stdout = sys.stderr          # un print() d'une bibliothèque ne doit jamais corrompre le protocole
    _setup_logging()
    engine = Engine(real_out)
    log.info("moteur démarré (pid %s)", os.getpid())
    engine.send({"event": "ready", "data": {"pid": os.getpid()}})
    src = stdin or sys.stdin.buffer
    try:
        for raw in src:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                log.warning("ligne illisible ignorée : %r", raw[:120])
                continue
            if msg.get("method") == "shutdown":
                engine.send({"id": msg.get("id"), "result": "bye"})
                break
            engine.handle(msg)
    finally:
        log.info("arrêt du moteur")
        engine.shutdown()
    return 0


def main() -> int:
    import multiprocessing
    multiprocessing.freeze_support()     # processus d'indexation parallèles de l'exécutable figé
    if any(a in sys.argv[1:] for a in ("--index", "--check-integrity")):
        from core.indexer import cli
        return cli(sys.argv[1:])
    return serve()


if __name__ == "__main__":
    sys.exit(main())
