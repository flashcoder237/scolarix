"""CI : pilote le moteur compilé exactement comme Electron (tubes, sans console, chemin accentué) :
vider l'index puis réindexation complète, en un temps limité."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

exe, work = sys.argv[1], Path(sys.argv[2])
env = dict(os.environ, FMSP_DATA_DIR=str(work / "data"), PYTHONIOENCODING="utf-8")
p = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
threading.Thread(target=lambda: [sys.stderr.write("[moteur] " + l.decode("utf-8", "replace")) for l in p.stderr],
                 daemon=True).start()


def send(i, m, **params):
    p.stdin.write((json.dumps({"id": i, "method": m, "params": params}) + "\n").encode())
    p.stdin.flush()


ok = False
t0 = time.time()
timer = threading.Timer(150, lambda: (print("BLOQUÉ : délai dépassé"), p.kill()))
timer.start()
for raw in p.stdout:
    m = json.loads(raw)
    if m.get("event") == "ready":
        send(1, "index_reset", confirm="VIDER")
        send(2, "index_start", source=str(work / "archives"), full=True)
    elif m.get("event") == "index":
        d = m["data"]
        print(f"{time.time() - t0:5.1f} s  {d.get('type')} {d.get('done', '')}/{d.get('total', '')}")
        if d.get("type") == "exit":
            ok = d.get("code") == 0
            send(3, "stats")
    elif m.get("id") == 3:
        print("relevés après réindexation :", m["result"]["releves"])
        ok = ok and m["result"]["releves"] > 0
        break
    elif "error" in m:
        print("erreur :", m["error"]["message"])
timer.cancel()
p.stdin.close()
p.wait(30)
sys.exit(0 if ok else 1)
