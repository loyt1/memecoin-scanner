"""Écoute en continu le flux public des trades de KOLs (celui qu'utilise kolscan.io) et les enregistre.

Un fichier par heure : data/kols/AAAA-MM-JJ-HH.jsonl, une ligne par trade :
{"t": horodatage, "w": wallet, "d": "B" (achat) ou "S" (vente), "tok": adresse du token, "sym": symbole, "usd": montant}
Les fichiers de plus de 72 h sont supprimés. Lancé en arrière-plan par boucle.sh.
"""
import json
import os
import time
import subprocess
from datetime import datetime, timezone

FLUX = "https://kolscan-api.pump.fun/api/v1/stream"
DOSSIER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "kols")
os.makedirs(DOSSIER, exist_ok=True)


def fichier(ts):
    return os.path.join(DOSSIER, datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d-%H") + ".jsonl")


def menage():
    limite = time.time() - 72 * 3600
    for f in os.listdir(DOSSIER):
        try:
            if datetime.strptime(f[:13], "%Y-%m-%d-%H").replace(tzinfo=timezone.utc).timestamp() < limite:
                os.remove(os.path.join(DOSSIER, f))
        except ValueError:
            pass


def ecouter():
    # curl plutôt qu'urllib : le flux (derrière Cloudflare) ne répond pas aux connexions Python
    proc = subprocess.Popen(["curl", "-s", "-N", "--http1.1", "-A", "Mozilla/5.0", "-H", "Origin: https://kolscan.io",
                             "-H", "Accept: text/event-stream", "--max-time", "3600", FLUX],
                            stdout=subprocess.PIPE)
    with proc.stdout as r:
        for ligne in r:
            ligne = ligne.decode("utf-8", "ignore").strip()
            if not ligne.startswith("data:"):
                continue
            try:
                x = json.loads(ligne[5:])
            except ValueError:
                continue
            achat = x.get("spl_direction") == "Buy"
            tok = x.get("out_token_address") if achat else x.get("in_token_address")
            if not tok:
                continue
            ts = int(x.get("timestamp") or time.time())
            out = {"t": ts, "w": x.get("wallet_address"), "d": "B" if achat else "S", "tok": tok,
                   "sym": x.get("out_token_symbol") if achat else x.get("in_token_symbol"),
                   "usd": round(float(x.get("usd_change") or 0), 2)}
            with open(fichier(ts), "a") as f:
                f.write(json.dumps(out, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    dernier_menage = 0
    while True:
        if time.time() - dernier_menage > 3600:
            menage()
            dernier_menage = time.time()
        try:
            ecouter()
        except Exception as e:
            print("flux KOL coupé, reconnexion :", e, flush=True)
        time.sleep(5)
