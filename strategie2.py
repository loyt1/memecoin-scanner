"""Stratégie 2 (2026-10-30), paper trading sur Hyperliquid : aucune vraie transaction.

Deux compartiments, capital fictif de 1 000 € (500 + 500) :
1. Tendance BTC + ETH : on détient chaque crypto quand sa clôture d'hier est au-dessus de sa moyenne des 50 jours
   précédents, sinon cash. (Backtest 2022-2026, période jamais vue : +26 %/an contre +2 % en gardant.)
2. Short des pumps : un alt ou memecoin Hyperliquid a fait plus de +75 % en 7 jours (volume ≥ 1 M$/jour), et le BTC
   est sous sa moyenne 50 jours (marché baissier) → short de 20 % du compartiment, gardé 7 jours, stop à +50 %,
   5 positions maximum. Frais 0,3 % aller-retour, funding réel compté à la clôture.
Backtest de l'ensemble (mars 2023 → oct. 2026) : +48 %/an, pire baisse -19 %. Voir crypto/rapport-2026-10-30-nouvelles-strategies.md.

Lancé toutes les 15 min par boucle.sh : contrôle des stops à chaque passage, décisions d'entrée une fois par jour.
"""
import csv
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

ICI = os.path.dirname(os.path.abspath(__file__))
D = os.environ.get("S2_DIR") or os.path.join(ICI, "data", "s2")
os.makedirs(D, exist_ok=True)
API = "https://api.hyperliquid.xyz/info"
MAJEURES = {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC", "TRX", "DOT", "BCH", "HYPE"}
P = {"ma": 50, "pump": 0.75, "jours_pump": 7, "vol_min": 1e6, "garde_j": 7, "stop": 0.50, "taille": 0.20,
     "max_pos": 5, "frais": 0.003, "capital": 1000.0}


def api(corps, essais=4):
    for i in range(essais):
        try:
            req = urllib.request.Request(API, data=json.dumps(corps).encode(), headers={"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=40))
        except Exception:
            time.sleep(3 * (i + 1))
    return None


def lire(nom, defaut):
    p = os.path.join(D, nom)
    return json.load(open(p)) if os.path.exists(p) else defaut


def ecrire(nom, obj):
    json.dump(obj, open(os.path.join(D, nom), "w"), separators=(",", ":"))


def journal(ligne):
    p = os.path.join(D, "trades.csv")
    neuf = not os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(ligne))
        if neuf:
            w.writeheader()
        w.writerow(ligne)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def bougies(coin, jours=60):
    fin = int(time.time() * 1000)
    d = api({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1d", "startTime": fin - jours * 86400000, "endTime": fin}})
    # on ne garde que les bougies terminées (la bougie du jour est encore ouverte)
    debut_jour = (int(time.time()) // 86400) * 86400
    return [[x["t"] // 1000, float(x["c"]), float(x["v"]) * float(x["c"])] for x in (d or []) if x["t"] // 1000 < debut_jour]


def au_dessus_ma(c):
    if len(c) < P["ma"] + 1:
        return None
    return c[-1][1] > sum(x[1] for x in c[-P["ma"] - 1:-1]) / P["ma"]


def main():
    now = int(time.time())
    E = lire("etat.json", {"debut": now, "tendance": {"cash": P["capital"] / 2, "pos": {}},
                           "short": {"cash": P["capital"] / 2, "pos": []}, "dernier_jour": 0, "historique": []})
    mids = {k: float(v) for k, v in (api({"type": "allMids"}) or {}).items()}
    if not mids:
        print("strategie2 : prix indisponibles, rien fait")
        return

    # 1. shorts ouverts : stop et durée, à chaque passage
    for p in list(E["short"]["pos"]):
        px = mids.get(p["coin"])
        if px is None:
            continue
        motif = "stop" if px >= p["entree"] * (1 + P["stop"]) else ("durée" if now >= p["t"] + P["garde_j"] * 86400 else None)
        if motif:
            fh = api({"type": "fundingHistory", "coin": p["coin"], "startTime": p["t"] * 1000, "endTime": now * 1000}) or []
            funding = sum(float(x["fundingRate"]) for x in fh)          # le short reçoit le funding positif
            g = 1 - px / p["entree"] - P["frais"] + funding
            E["short"]["cash"] += p["mise"] * (1 + g)
            E["short"]["pos"].remove(p)
            journal({"date": iso(now), "compartiment": "short", "action": f"clôture ({motif})", "coin": p["coin"],
                     "prix": px, "mise": round(p["mise"], 2), "gain_pct": round(100 * g, 1), "funding_pct": round(100 * funding, 2)})

    # 2. décisions quotidiennes (une fois par jour, après la clôture de 00:00 UTC)
    jour = (now // 86400) * 86400
    if E["dernier_jour"] < jour:
        btc, eth = bougies("BTC"), bougies("ETH")
        regime_baissier = au_dessus_ma(btc) is False
        # compartiment tendance
        for coin, c in (("BTC", btc), ("ETH", eth)):
            on = au_dessus_ma(c)
            if on is None:
                continue
            tient = coin in E["tendance"]["pos"]
            part = P["capital"] / 4                                       # 250 € de référence par crypto
            if on and not tient:
                mise = min(E["tendance"]["cash"], max(part, E["tendance"]["cash"] / (2 - len(E["tendance"]["pos"]))))
                E["tendance"]["cash"] -= mise
                E["tendance"]["pos"][coin] = {"qte": mise * (1 - P["frais"] / 2) / mids[coin], "entree": mids[coin], "t": now}
                journal({"date": iso(now), "compartiment": "tendance", "action": "achat", "coin": coin, "prix": mids[coin],
                         "mise": round(mise, 2), "gain_pct": "", "funding_pct": ""})
            elif not on and tient:
                p = E["tendance"]["pos"].pop(coin)
                valeur = p["qte"] * mids[coin] * (1 - P["frais"] / 2)
                E["tendance"]["cash"] += valeur
                journal({"date": iso(now), "compartiment": "tendance", "action": "vente", "coin": coin, "prix": mids[coin],
                         "mise": round(valeur, 2), "gain_pct": round(100 * (mids[coin] / p["entree"] - 1), 1), "funding_pct": ""})
        # compartiment short
        candidats = []
        if regime_baissier and len(E["short"]["pos"]) < P["max_pos"]:
            meta = (api({"type": "meta"}) or {}).get("universe", [])
            deja = {p["coin"] for p in E["short"]["pos"]}
            for u in meta:
                n = u["name"]
                if n in MAJEURES or n in deja or u.get("isDelisted") or n not in mids:
                    continue
                c = bougies(n, 12)
                time.sleep(0.15)
                if len(c) < P["jours_pump"] + 1 or c[-1][2] < P["vol_min"]:
                    continue
                hausse = c[-1][1] / c[-1 - P["jours_pump"]][1] - 1
                if hausse >= P["pump"]:
                    candidats.append((hausse, n))
            for hausse, n in sorted(candidats, reverse=True):
                if len(E["short"]["pos"]) >= P["max_pos"]:
                    break
                total = valeur_totale(E, mids)
                mise = min(E["short"]["cash"], P["taille"] * total / 2)     # 20 % du compartiment (10 % du total)
                if mise < 1:
                    break
                E["short"]["cash"] -= mise
                E["short"]["pos"].append({"coin": n, "entree": mids[n], "t": now, "mise": mise, "hausse_7j": round(hausse, 3)})
                journal({"date": iso(now), "compartiment": "short", "action": f"short (pump +{round(100 * hausse)} % en 7 j)",
                         "coin": n, "prix": mids[n], "mise": round(mise, 2), "gain_pct": "", "funding_pct": ""})
        E["dernier_jour"] = jour
        E["regime_baissier"] = regime_baissier
        E["candidats_du_jour"] = [n for _, n in candidats]

    # 3. valeur et historique
    total = valeur_totale(E, mids)
    E["valeur"] = round(total, 2)
    E["maj"] = now
    if not E["historique"] or now - E["historique"][-1][0] >= 3600:
        E["historique"].append([now, round(total, 2)])
    ecrire("etat.json", E)
    print(f"strategie2 : valeur {total:.2f} € | régime {'baissier' if E.get('regime_baissier') else 'haussier'} | "
          f"tendance {list(E['tendance']['pos'])} | shorts {[p['coin'] for p in E['short']['pos']]}")


def valeur_totale(E, mids):
    v = E["tendance"]["cash"] + sum(p["qte"] * mids.get(c, p["entree"]) for c, p in E["tendance"]["pos"].items())
    v += E["short"]["cash"] + sum(p["mise"] * (1 + 1 - mids.get(p["coin"], p["entree"]) / p["entree"]) for p in E["short"]["pos"])
    return v


if __name__ == "__main__":
    main()
