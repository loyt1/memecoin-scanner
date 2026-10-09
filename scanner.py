"""Scanner memecoins Solana : paper trading des stratégies B et G (voir ../strategie.md).

Tourne toutes les 15 minutes (GitHub Actions). À chaque passage :
1. Découverte : nouveaux tokens via Dexscreener (boosts, profils, takeovers) et,
   une fois par heure, les classements GeckoTerminal.
2. Relevé : prix et market cap de tous les tokens suivis (Dexscreener, 30 par requête).
   Le bot construit ainsi son propre historique (un point toutes les 15 min).
3. Détection : pump récent (x2 minimum en 48 h, sommet entre 1 et 15 M$), puis
   retracement qui touche -60 % (B) ou -70 % (G) → position fictive au prix limite,
   avec les signaux du moment (baleines, KOLs, holders, achats/ventes).
4. Suivi des positions : paliers de vente, stop, sortie après 72 h.
5. Rapport : RAPPORT.md.

Aucun argent réel, aucun wallet : uniquement de l'observation.
"""
import csv
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEX = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
LLAMA = "https://api.llama.fi"
LLAMA_COINS = "https://coins.llama.fi"
ICI = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ICI, "data")
os.makedirs(DATA, exist_ok=True)

STRATEGIES = {
    # entrée sous le sommet, pump maximum, heures minimum entre le sommet et l'entrée
    "B": {"dd": 0.60, "pump_max": 5, "h_min": 0},
    "G": {"dd": 0.70, "pump_max": 10, "h_min": 2},
}
# Variantes de sortie suivies en parallèle pour chaque position
SORTIES = {
    "50_x2_sl60": {"sl": 0.60, "tps": [(0.5, 0.5), (1.0, 0.5)]},
    "30_60_sl60": {"sl": 0.60, "tps": [(0.3, 0.5), (0.6, 0.5)]},
    "50_x2_sl30": {"sl": 0.30, "tps": [(0.5, 0.5), (1.0, 0.5)]},
}
SOMMET_MIN, SOMMET_MAX = 1e6, 15e6
DUREE_MAX_H = 72
FRAIS = 0.03
GARDE_H = 96                 # durée de vie d'un token dans l'univers sans activité
STABLES = {"USDC", "USDT", "SOL", "WSOL", "USDG", "PYUSD", "USD1", "JUP", "JITOSOL", "MSOL"}

stats = {"dex": 0, "gt": 0, "gt_echecs": 0, "llama": 0}


def _get(url, pause, essais):
    for _ in range(essais):
        time.sleep(pause)
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "memecoin-scanner"})
            return json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 404):
                return None
            time.sleep(20 if e.code == 429 else 3)
        except Exception:
            time.sleep(3)
    return None


def dex(path):
    stats["dex"] += 1
    return _get(DEX + path, 0.25, 3)


def gt(path):
    stats["gt"] += 1
    r = _get(GT + path, 4, 3)
    if r is None:
        stats["gt_echecs"] += 1
    return r


def llama(url):
    stats["llama"] += 1
    return _get(url, 0.3, 3)


def historique_llama(univ, nouveaux, maintenant):
    """Historique horaire des 48 dernières heures pour les tokens qu'on vient de découvrir (DefiLlama, gratuit).
    Permet de voir tout de suite le pump et le sommet, au lieu de 3 points reconstitués."""
    # DefiLlama ne connaît qu'une partie des memecoins (~15 %) : on vérifie d'abord lesquels
    connus = []
    for k in range(0, len(nouveaux), 50):
        d = llama(f"{LLAMA_COINS}/prices/current/" + ",".join("solana:" + t for t in nouveaux[k:k + 50]))
        connus += [c.split(":", 1)[1] for c in ((d or {}).get("coins") or {})]
    for k in range(0, len(connus), 10):   # au-delà de 10 tokens par requête, l'API refuse
        lot = connus[k:k + 10]
        d = llama(f"{LLAMA_COINS}/chart/" + ",".join("solana:" + t for t in lot) + "?span=48&period=1h&searchWidth=600")
        for cle, v in ((d or {}).get("coins") or {}).items():
            t = cle.split(":", 1)[1]
            u = univ.get(t)
            if not u or not u.get("ratio"):
                continue
            pts = [[int(x["timestamp"]), round(x["price"] * u["ratio"]), 2] for x in v.get("prices", [])
                   if x.get("price") and int(x["timestamp"]) < maintenant - 600]
            if len(pts) >= 6:
                # remplace les 3 points reconstitués par le vrai historique horaire
                u["pts"] = sorted(pts + [x for x in u["pts"] if x[2] == 0])
                u["histo"] = "llama"


def contexte_marche(maintenant):
    """Une fois par heure : volume des DEX sur Solana et sur PumpSwap (DefiLlama). Sert à savoir si
    la stratégie marche mieux quand le marché des memecoins est chaud ou froid."""
    p = os.path.join(DATA, "contexte.json")
    ctx = json.load(open(p)) if os.path.exists(p) else {}
    if maintenant - ctx.get("t", 0) < 3600:
        return ctx
    d = llama(f"{LLAMA}/overview/dexs/solana?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true")
    if not d:
        return ctx
    pump = next((x for x in d.get("protocols", []) if x.get("name") == "PumpSwap"), {})
    ctx = {"t": maintenant, "sol_vol24": d.get("total24h"), "sol_var7j": d.get("change_7d"),
           "pumpswap_vol24": pump.get("total24h"), "pumpswap_var7j": pump.get("change_7d")}
    json.dump(ctx, open(p, "w"))
    ajouter_csv("contexte.csv", {"date": iso(maintenant), **{k: v for k, v in ctx.items() if k != "t"}})
    return ctx


def charger(nom, defaut):
    p = os.path.join(DATA, nom)
    return json.load(open(p)) if os.path.exists(p) else defaut


def sauver(nom, obj):
    json.dump(obj, open(os.path.join(DATA, nom), "w"), separators=(",", ":"))


def ajouter_csv(nom, ligne):
    p = os.path.join(DATA, nom)
    neuf = not os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(ligne.keys()))
        if neuf:
            w.writeheader()
        w.writerow(ligne)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


# ---------- 1. Découverte ----------
def decouvrir(univ, maintenant):
    nouveaux = set()
    for path in ("/token-boosts/top/v1", "/token-boosts/latest/v1", "/token-profiles/latest/v1",
                 "/community-takeovers/latest/v1"):
        for x in dex(path) or []:
            if x.get("chainId") == "solana" and x.get("tokenAddress"):
                nouveaux.add(x["tokenAddress"])
    # une fois par heure : classements GeckoTerminal (limite de requêtes stricte)
    if (maintenant // 900) % 4 == 0 or not univ:
        listes = [f"/networks/solana/dexes/pumpswap/pools?page={p}&sort=h24_volume_usd_desc" for p in range(1, 6)]
        listes += [f"/networks/solana/dexes/pumpswap/pools?page={p}&sort=h24_tx_count_desc" for p in (1, 2)]
        listes += [f"/networks/solana/dexes/{d}/pools?page=1&sort=h24_volume_usd_desc"
                   for d in ("meteora-damm-v2", "raydium", "letsbonk-fun", "raydium-launchlab", "meteora")]
        listes += [f"/networks/solana/trending_pools?page=1&duration={d}" for d in ("1h", "6h", "24h")]
        for path in listes:
            d = gt(path)
            for p in (d or {}).get("data", []):
                a = p["attributes"]
                fdv = float(a.get("fdv_usd") or 0)
                if 500_000 <= fdv <= 20_000_000:
                    nouveaux.add(p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1])
    for t in nouveaux:
        univ.setdefault(t, {"vu": maintenant, "pts": []})
    return len(nouveaux)


# ---------- 2. Relevé des prix ----------
def relever(univ, maintenant):
    toks = list(univ)
    infos = {}
    for k in range(0, len(toks), 30):
        for p in dex("/tokens/v1/solana/" + ",".join(toks[k:k + 30])) or []:
            t = p["baseToken"]["address"]
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if t in univ and (t not in infos or liq > infos[t]["liq"]):
                infos[t] = {"liq": liq, "p": p}
    for t, inf in infos.items():
        p = inf["p"]
        mc = float(p.get("marketCap") or p.get("fdv") or 0)
        if mc <= 0 or p["baseToken"]["symbol"].upper() in STABLES:
            continue
        u = univ[t]
        prix = float(p.get("priceUsd") or 0)
        if prix > 0:
            u["ratio"] = mc / prix
        ch = p.get("priceChange") or {}
        if not u["pts"]:
            # premier relevé : on reconstitue quelques points passés grâce aux variations
            for h, k in ((24, "h24"), (6, "h6"), (1, "h1")):
                if ch.get(k) is not None and ch[k] > -100:
                    u["pts"].append([maintenant - h * 3600, round(mc / (1 + ch[k] / 100)), 1])
        u["pts"].append([maintenant, round(mc), 0])
        u["pts"] = [x for x in u["pts"] if x[0] >= maintenant - 96 * 3600]
        tx = p.get("txns") or {}
        u.update({"sym": p["baseToken"]["symbol"], "pair": p["pairAddress"], "dex": p.get("dexId"),
                  "liq": round(inf["liq"]), "cree": (p.get("pairCreatedAt") or 0) // 1000,
                  "vol_h1": (p.get("volume") or {}).get("h1"), "vol_h24": (p.get("volume") or {}).get("h24"),
                  "tx_h1": tx.get("h1"), "tx_h6": tx.get("h6"),
                  "x": any(s.get("type") == "twitter" for s in (p.get("info") or {}).get("socials", [])),
                  "maj": maintenant})
        if mc > 100_000:
            u["vu"] = maintenant
    # ménage : tokens morts ou plus suivis depuis longtemps
    for t in [t for t, u in univ.items() if maintenant - u["vu"] > GARDE_H * 3600]:
        del univ[t]
    return len(infos)


# ---------- 3. Détection ----------
def etat_retracement(u, maintenant):
    """Sommet des 48 dernières heures, pump qui l'a précédé, baisse actuelle."""
    pts = u["pts"]
    recents = [x for x in pts if x[0] >= maintenant - 48 * 3600]
    if len(recents) < 2 or pts[-1][0] != maintenant:
        return None
    pk = max(recents, key=lambda x: x[1])
    avant = [x for x in pts if pk[0] - 48 * 3600 <= x[0] <= pk[0]]
    pre = min(avant, key=lambda x: x[1])
    if pre[1] <= 0:
        return None
    return {"peak_t": pk[0], "peak": pk[1], "pre": pre[1], "pre_t": pre[0], "pump_x": pk[1] / pre[1],
            "dd": 1 - pts[-1][1] / pk[1], "mc": pts[-1][1]}


def holders(token):
    d = gt(f"/networks/solana/tokens/{token}/info")
    if d and d.get("data"):
        h = d["data"]["attributes"].get("holders") or {}
        return h.get("count") if isinstance(h, dict) else None
    return None


def kols():
    p = os.path.join(ICI, "kols.csv")
    return {r["wallet"]: r["nom"] for r in csv.DictReader(open(p))} if os.path.exists(p) else {}


def signaux(u, token, peak_t, kol_map):
    s = {"baleines_achat_usd": None, "baleines_vente_usd": None, "baleines_acheteurs": None, "baleines_vendeurs": None,
         "kol_achats": None, "kol_ventes": None, "kol_noms": "", "holders": holders(token)}
    d = gt(f"/networks/solana/pools/{u['pair']}/trades?trade_volume_in_usd_greater_than=2000")
    if d is not None:
        ach, ven, ba, bv = set(), set(), 0.0, 0.0
        for t in d.get("data", []):
            a = t["attributes"]
            ts = datetime.fromisoformat(a["block_timestamp"].replace("Z", "+00:00")).timestamp()
            if ts < peak_t:
                continue
            usd = float(a.get("volume_in_usd") or 0)
            if a["kind"] == "buy":
                ba += usd; ach.add(a.get("tx_from_address"))
            else:
                bv += usd; ven.add(a.get("tx_from_address"))
        s.update({"baleines_achat_usd": round(ba), "baleines_vente_usd": round(bv),
                  "baleines_acheteurs": len(ach), "baleines_vendeurs": len(ven)})
    d = gt(f"/networks/solana/pools/{u['pair']}/trades")
    if d is not None:
        a_, v_, noms = 0, 0, set()
        for t in d.get("data", []):
            a = t["attributes"]
            w = a.get("tx_from_address")
            if w in kol_map:
                if a["kind"] == "buy":
                    a_ += 1
                else:
                    v_ += 1
                noms.add(kol_map[w])
        s.update({"kol_achats": a_, "kol_ventes": v_, "kol_noms": " ".join(sorted(noms))})
    return s


# ---------- 4. Suivi des positions ----------
def evaluer(pos, u, maintenant):
    ent = pos["prix_entree"]
    pts = [x for x in u["pts"] if x[2] == 0 and pos["entree_t"] < x[0] <= pos["entree_t"] + DUREE_MAX_H * 3600]
    fini = maintenant >= pos["entree_t"] + DUREE_MAX_H * 3600
    for nom, v in SORTIES.items():
        r = pos["res"][nom]
        if r["ferme"]:
            continue
        # evts : chaque vente partielle ou totale, avec l'heure, la part vendue et le gain
        reste, pnl, k, evts = 1.0, 0.0, 0, []
        for x in pts:
            if x[1] <= ent * (1 - v["sl"]):
                evts.append({"t": x[0], "type": "stop", "part": round(reste, 4), "gain": -v["sl"]})
                pnl += reste * -v["sl"]; reste = 0; break
            while k < len(v["tps"]) and x[1] >= ent * (1 + v["tps"][k][0]):
                f = min(reste, v["tps"][k][1]); pnl += f * v["tps"][k][0]; reste -= f
                evts.append({"t": x[0], "type": f"palier {k + 1}", "part": round(f, 4), "gain": v["tps"][k][0]})
                k += 1
            if reste <= 1e-9:
                break
        dernier = pts[-1][1] / ent - 1 if pts else 0
        if reste > 1e-9 and fini:
            evts.append({"t": pts[-1][0] if pts else maintenant, "type": "sortie 72 h", "part": round(reste, 4),
                         "gain": round(dernier, 4)})
        r["evts"], r["reste"], r["gain_latent"] = evts, round(reste, 4), round(dernier, 4)
        if reste <= 1e-9 or fini:
            r.update({"ferme": True, "pnl": round(pnl + reste * dernier - FRAIS, 4), "t_fin": evts[-1]["t"] if evts else maintenant})
        else:
            r["pnl"] = round(pnl + reste * dernier - FRAIS, 4)
    if pts:
        pos["mc_actuel"] = pts[-1][1]
        pos["haut"] = round(max(x[1] for x in pts) / ent - 1, 3)
        pos["bas"] = round(min(x[1] for x in pts) / ent - 1, 3)
        pos["nouveau_sommet"] = any(x[1] > pos["peak"] for x in pts)


# ---------- 5. Rapport ----------
def rapport(positions, fermees, maintenant, n_univ, n_rel):
    L = [f"# Rapport du scanner (mis à jour le {iso(maintenant)} UTC)", "",
         "Paper trading uniquement : aucune vraie transaction.", "",
         f"Tokens suivis : {n_univ} (dont {n_rel} relevés à ce passage). Requêtes : {stats['dex']} Dexscreener, "
         f"{stats['gt']} GeckoTerminal ({stats['gt_echecs']} échecs).", "",
         "## Résultats par stratégie et par sortie", "",
         "| Stratégie | Sortie | Trades clôturés | Gagnants | Gain moyen | En cours |", "|---|---|---|---|---|---|"]
    for st in STRATEGIES:
        for so in SORTIES:
            f = [p["res"][so]["pnl"] for p in fermees if p["strat"] == st]
            o = sum(1 for p in positions if p["strat"] == st)
            if f:
                L.append(f"| {st} | {so} | {len(f)} | {round(100 * sum(1 for x in f if x > 0) / len(f))} % | "
                         f"{round(100 * sum(f) / len(f), 1)} % | {o} |")
            else:
                L.append(f"| {st} | {so} | 0 | - | - | {o} |")
    L += ["", "Référence backtest (2026-10-08, sortie 50_x2_sl60) : B +17 %, G +31 % par trade.", "",
          "## Dernières positions", "",
          "| Token | Strat | Entrée (UTC) | MC entrée | Pump | 50/x2 | Haut | Bas | Baleines achat/vente | KOLs A/V | Holders -40 % → entrée | Statut |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in sorted(fermees + positions, key=lambda x: -x["entree_t"])[:100]:
        r = p["res"]["50_x2_sl60"]
        s = p["sig"]
        statut = "clôturée" if all(v["ferme"] for v in p["res"].values()) else "ouverte"
        L.append(f"| [{p['sym']}](https://dexscreener.com/solana/{p['pair']}) | {p['strat']} | {iso(p['entree_t'])} | "
                 f"{round(p['prix_entree'] / 1e6, 2)} M$ | x{round(p['pump_x'], 1)} | {round(100 * r['pnl'])} % | "
                 f"+{round(100 * p.get('haut', 0))} % | {round(100 * p.get('bas', 0))} % | "
                 f"{s['baleines_achat_usd']}/{s['baleines_vente_usd']} $ | {s['kol_achats']}/{s['kol_ventes']} | "
                 f"{p.get('holders_40')} → {s['holders']} | {statut} |")
    open(os.path.join(ICI, "RAPPORT.md"), "w").write("\n".join(L) + "\n")


def tableau_de_bord(positions, fermees, maintenant, n_univ, n_rel):
    """Fichier léger lu par le site (tableau de bord Vercel)."""
    champs = ("token", "pair", "sym", "strat", "entree_t", "prix_entree", "ordre_limite", "mc_reel", "peak", "peak_t",
              "pump_x", "pump_h", "h_vers_entree", "liq", "x", "age_h", "holders_40", "sig", "res", "haut", "bas",
              "nouveau_sommet", "mc_actuel")
    trades = [{k: p.get(k) for k in champs} for p in fermees + positions]
    json.dump({"maj": maintenant, "tokens_suivis": n_univ, "releves": n_rel, "frais": FRAIS,
               "strategies": STRATEGIES, "sorties": {k: {"sl": v["sl"], "tps": v["tps"]} for k, v in SORTIES.items()},
               "duree_max_h": DUREE_MAX_H, "trades": trades},
              open(os.path.join(DATA, "tableau.json"), "w"), separators=(",", ":"))


def main():
    maintenant = int(time.time())
    univ = charger("univers.json", {})
    positions = charger("positions.json", [])
    fermees = charger("fermees.json", [])
    deja = {(p["token"], p["strat"], p["peak_t"]) for p in positions + fermees}
    kol_map = kols()

    decouvrir(univ, maintenant)
    n_rel = relever(univ, maintenant)
    a_completer = [t for t, u in univ.items() if not u.get("histo_essai") and u.get("ratio")]
    for t in a_completer:
        univ[t]["histo_essai"] = True
    historique_llama(univ, a_completer, maintenant)
    ctx = contexte_marche(maintenant)

    for t, u in univ.items():
        if u.get("maj") != maintenant:
            continue
        e = etat_retracement(u, maintenant)
        if not e or not (SOMMET_MIN <= e["peak"] <= SOMMET_MAX) or e["pump_x"] < 2:
            continue
        # holders au moment où le retracement passe -40 % (pour mesurer leur évolution)
        if e["dd"] >= 0.4 and u.get("h40_peak") != e["peak_t"]:
            u["h40_peak"], u["holders_40"] = e["peak_t"], holders(t)
        for strat, s in STRATEGIES.items():
            if e["pump_x"] > s["pump_max"] or e["dd"] < s["dd"] or (t, strat, e["peak_t"]) in deja:
                continue
            h = (maintenant - e["peak_t"]) / 3600
            if h < s["h_min"] or h > 48 or e["dd"] > s["dd"] + 0.10:
                continue
            # Si on avait déjà vu le token au-dessus du seuil, l'ordre limite aurait été posé :
            # entrée au prix limite. Sinon (découvert déjà sous le seuil) : achat au prix actuel.
            vus = [x for x in u["pts"][:-1] if x[2] == 0 and x[0] >= e["peak_t"]]
            limite = e["peak"] * (1 - s["dd"])
            prix = limite if vus and vus[-1][1] > limite else min(limite, e["mc"])
            sig = signaux(u, t, e["peak_t"], kol_map)
            pos = {"token": t, "pair": u["pair"], "sym": u["sym"], "strat": strat, "entree_t": maintenant,
                   "prix_entree": prix, "ordre_limite": prix == limite, "mc_reel": e["mc"], "peak": e["peak"], "peak_t": e["peak_t"],
                   "pump_x": round(e["pump_x"], 2), "pump_h": round((e["peak_t"] - e["pre_t"]) / 3600, 1),
                   "h_vers_entree": round(h, 1), "liq": u.get("liq"), "x": u.get("x"),
                   "age_h": round((maintenant - u["cree"]) / 3600) if u.get("cree") else None,
                   "holders_40": u.get("holders_40") if u.get("h40_peak") == e["peak_t"] else None,
                   "sig": sig, "contexte": {k: v for k, v in ctx.items() if k != "t"},
                   "res": {k: {"ferme": False, "pnl": -FRAIS} for k in SORTIES}}
            positions.append(pos)
            deja.add((t, strat, e["peak_t"]))
            ajouter_csv("entrees.csv", {"date": iso(maintenant), "token": t, "sym": u["sym"], "strat": strat,
                                        "mc_sommet": round(e["peak"]), "mc_entree": round(pos["prix_entree"]),
                                        "mc_reel": round(e["mc"]), "pump_x": pos["pump_x"], "pump_h": pos["pump_h"],
                                        "h_vers_entree": pos["h_vers_entree"], "age_h": pos["age_h"],
                                        "liquidite": u.get("liq"), "compte_x": u.get("x"),
                                        "tx_h1": json.dumps(u.get("tx_h1")), "holders_40": pos["holders_40"],
                                        "historique": u.get("histo", "reconstitue"), **sig,
                                        **{"marche_" + k: v for k, v in pos["contexte"].items()}})

    restantes = []
    for p in positions:
        u = univ.get(p["token"])
        if u:
            evaluer(p, u, maintenant)
        if all(v["ferme"] for v in p["res"].values()) or maintenant > p["entree_t"] + (DUREE_MAX_H + 6) * 3600:
            for v in p["res"].values():
                v["ferme"] = True
                v.setdefault("t_fin", maintenant)
            fermees.append(p)
            ajouter_csv("sorties.csv", {"date_entree": iso(p["entree_t"]), "sym": p["sym"], "token": p["token"],
                                        "strat": p["strat"], **{k: v["pnl"] for k, v in p["res"].items()},
                                        "haut_max": p.get("haut"), "bas_max": p.get("bas"),
                                        "nouveau_sommet": p.get("nouveau_sommet")})
        else:
            restantes.append(p)

    sauver("univers.json", univ)
    sauver("positions.json", restantes)
    sauver("fermees.json", fermees)
    rapport(restantes, fermees, maintenant, len(univ), n_rel)
    tableau_de_bord(restantes, fermees, maintenant, len(univ), n_rel)
    print(f"univers {len(univ)}, relevés {n_rel}, ouvertes {len(restantes)}, clôturées {len(fermees)}, "
          f"requêtes dex {stats['dex']} gt {stats['gt']} (échecs {stats['gt_echecs']}) llama {stats['llama']}, "
          f"{sum(1 for u in univ.values() if u.get('histo') == 'llama')} tokens avec historique DefiLlama")


if __name__ == "__main__":
    main()
