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
        self._stopping = False
        self._run_gen = 0
        self.data_dir = data_dir()
        self.methods = self._methods()
        threading.Thread(target=self._housekeeping, name="menage", daemon=True).start()
        # Word lancé à l'avance (quelques secondes après le démarrage, pour ne pas ralentir l'ouverture) :
        # le premier aperçu Word de la journée ne paie plus son lancement.
        threading.Timer(4.0, self.previews.warm_up).start()

    def _housekeeping(self) -> None:
        """Au démarrage, comme la 1.x : PDF temporaires d'une session interrompue, instances de Word orphelines,
        exports plus anciens que la durée de conservation (Réglages > Exports)."""
        from core import exports, pdf_tools
        for name, fn in (("fichiers temporaires", lambda: pdf_tools.purge_temp(max_age=3600)),
                         ("Word orphelins", _kill_orphan_words),
                         ("conservation des exports", lambda: exports.purge_old(self.settings))):
            try:
                n = fn()
                if n:
                    log.info("ménage au démarrage (%s) : %s élément(s)", name, n)
            except Exception:
                log.exception("ménage au démarrage (%s)", name)

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
                 "verify_compare", "verify_report", "settings_get", "settings_save", "rules_defaults", "rules_test",
                 "visual_refused", "demande_add_cursus", "journal_export", "suggest_path", "exports_dir",
                 "check_integrity", "verify_print_prepare", "verify_printed", "preview_original"]
        names += ["index_overview", "anomalies_summary", "anomalies_files", "anomalies_detail", "anomalies_mark_seen", "anomalies_export"]  # écran Indexation (2.1)
        table = {n: getattr(s, n) for n in names}
        table.update({"index_start": self.index_start, "index_stop": self.index_stop,
                      "index_reset": self.index_reset, "index_impact": s.index_impact,
                      "index_status": self.index_status, "logs_dir": lambda: str(self.data_dir / "logs")})
        table.update({"index_reread": self.index_reread, "index_exclude_folder": self.index_exclude_folder})

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
    def index_start(self, source: str, full: bool = False, paths: list[str] | None = None) -> dict:
        """Lance le robot. ``paths`` : relecture de ces seuls fichiers (écran Anomalies, « Relire »)."""
        from core.config import save_settings
        with self._indexer_lock:
            if self.indexer and self.indexer.poll() is None:
                return {"running": True, "already": True}
            if paths is None and (not source or not Path(source).exists()):
                from core.service import ServiceError
                raise ServiceError("Dossier source introuvable : choisissez le dossier racine des archives.")
            if paths is None:
                self.settings.source_root = source
                save_settings(self.settings)
            program = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(ROOT / "main.py")]
            args = program + ["--index", "--json", "--stdin-control", "--source", source or "",
                              "--db", str(self.service.db_path),
                              "--full" if (full or not self.settings.differential) else "--only-new"]
            if paths is not None:
                # liste dans un fichier (une ligne de commande Windows est limitée à 32 000 caractères)
                tmp = self.data_dir / "tmp"
                tmp.mkdir(parents=True, exist_ok=True)
                listing = tmp / f"relire_{os.getpid()}_{self._run_gen + 1}.txt"
                listing.write_text("\n".join(paths) + "\n", encoding="utf-8")
                args += ["--paths-file", str(listing)]
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
            self._stopping = False
            self._run_gen += 1
            threading.Thread(target=self._relay_index, args=(self.indexer,), daemon=True).start()
        return {"running": True}

    def index_reread(self, items: list[str] | None = None, filter: dict | None = None) -> dict:
        """« Relire » (écran Anomalies) : une passe sur les fichiers cochés, ou sur tous ceux d'une cause."""
        from core.service import ServiceError
        paths = self.service.anomalies_paths(items, filter)
        if not paths:
            raise ServiceError("Aucun fichier à relire dans cette sélection.")
        res = self.index_start(self.settings.source_root, paths=paths)
        return dict(res, fichiers=len(paths))

    def index_exclude_folder(self, dossier: str, apply: bool = False) -> dict:
        from core.service import ServiceError
        if apply and self.indexer and self.indexer.poll() is None:
            raise ServiceError("Une indexation est en cours : arrêtez-la avant d'exclure un dossier.")
        return self.service.index_exclude_folder(dossier, apply)

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
        data = {"type": "exit", "code": code, "stopped": bool(self._stopping)}
        if code not in (0, None) and not self._stopping:
            data["error"] = _last_error_line(self.data_dir / "logs" / "indexation.log")
        self.send({"event": "index", "data": data})

    def index_stop(self, force: bool = False) -> dict:
        proc = self.indexer
        if not proc or proc.poll() is not None:
            return {"running": False}
        self._stopping = True
        if force:
            proc.kill()
            _kill_orphan_words()
        else:
            try:
                proc.stdin.write(b"stop\n")
                proc.stdin.flush()
            except OSError:
                proc.kill()
            # comme la 1.x : sans fin propre au bout d'une minute, l'arrêt est forcé (Word compris)
            gen = self._run_gen
            timer = threading.Timer(self.FORCE_STOP_AFTER, self._force_stop, args=(proc, gen))
            timer.daemon = True
            timer.start()
        return {"running": True, "stopping": True}

    FORCE_STOP_AFTER = 60.0

    def _force_stop(self, proc: subprocess.Popen, gen: int) -> None:
        if gen != self._run_gen or proc.poll() is not None:
            return      # indexation déjà finie, ou une nouvelle a été lancée entre-temps
        log.warning("arrêt propre trop long : indexation arrêtée de force")
        proc.kill()
        _kill_orphan_words()

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
            try:
                from core.pdf_tools import purge_temp
                purge_temp()          # PDF temporaires de cette session (données personnelles)
            except Exception:
                pass


def _kill_orphan_words() -> int:
    from core.extractors import kill_orphan_words
    return kill_orphan_words()


def _last_error_line(path: Path) -> str | None:
    """Dernière ligne utile du journal du robot (cause d'un arrêt anormal, montrée à l'agent)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 8000))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if line and not line.startswith("=====") and not line.startswith(("File ", "Traceback")):
            return line[:200]
    return None


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
