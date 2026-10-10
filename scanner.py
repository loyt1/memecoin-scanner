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
import antirug
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
    # « moonbag » : 40 % à +50 %, 40 % à x2, on garde 20 % avec un stop suiveur à -40 % du plus haut, jusqu'à 7 jours
    "moonbag": {"sl": 0.60, "tps": [(0.5, 0.4), (1.0, 0.4)], "suiveur": 0.40, "max_h": 168},
}
# Stratégie C : convergence KOL (au moins KOL_MIN KOLs différents achètent le même token en moins d'une heure)
STRATEGIE_C_ACTIVE = False   # arrêtée le 2026-10-10 : micro-tokens, -29 % par trade sur 42 trades (décision de Nils)
KOL_MIN = 3
KOL_ACHAT_MIN_USD = 50
KOLS_DIR = os.path.join(DATA, "kols")
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
    # un token n'est marqué « essayé » que si la requête a abouti : sinon on réessaie au relevé suivant
    connus = []
    for k in range(0, len(nouveaux), 50):
        lot = nouveaux[k:k + 50]
        d = llama(f"{LLAMA_COINS}/prices/current/" + ",".join("solana:" + t for t in lot))
        if d is None:
            continue
        connus += [c.split(":", 1)[1] for c in (d.get("coins") or {})]
        for t in lot:
            univ[t]["histo_essai"] = True
    for k in range(0, len(connus), 10):   # au-delà de 10 tokens par requête, l'API refuse
        lot = connus[k:k + 10]
        d = llama(f"{LLAMA_COINS}/chart/" + ",".join("solana:" + t for t in lot) + "?span=48&period=1h&searchWidth=600")
        if d is None:
            for t in lot:
                univ[t]["histo_essai"] = False
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
    for x in trades_kol(maintenant - 2 * 3600):
        if x["d"] == "B" and x["usd"] >= KOL_ACHAT_MIN_USD:
            nouveaux.add(x["tok"])
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
    for t in [t for t, u in univ.items() if maintenant - u["vu"] > GARDE_H * 3600 and not u.get("en_position")]:
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


# ---------- 3 bis. Flux KOL (écrit par ecoute_kols.py) ----------
def trades_kol(depuis):
    out = []
    if not os.path.isdir(KOLS_DIR):
        return out
    for f in sorted(os.listdir(KOLS_DIR)):
        try:
            debut = datetime.strptime(f[:13], "%Y-%m-%d-%H").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if debut + 3600 < depuis:
            continue
        for ligne in open(os.path.join(KOLS_DIR, f)):
            try:
                x = json.loads(ligne)
            except ValueError:
                continue
            if x["t"] >= depuis:
                out.append(x)
    return out


def kol_resume(trades, token, depuis, kol_map):
    """Achats et ventes des KOLs sur un token depuis un instant donné."""
    a = [x for x in trades if x["tok"] == token and x["t"] >= depuis]
    ach = {x["w"] for x in a if x["d"] == "B"}
    ven = {x["w"] for x in a if x["d"] == "S"}
    return {"kols_acheteurs": len(ach), "kols_vendeurs": len(ven),
            "kols_achat_usd": round(sum(x["usd"] for x in a if x["d"] == "B")),
            "kols_vente_usd": round(sum(x["usd"] for x in a if x["d"] == "S")),
            "kols_noms": " ".join(sorted({kol_map.get(w, w[:6]) for w in ach}))[:200]}


# ---------- 3 ter. Anti-rug ----------
_budget_antirug = {"n": 0}


def analyse_antirug(token):
    """Bubblemap simplifiée (voir antirug.py). Au plus 4 analyses par relevé pour tenir dans les 15 minutes."""
    if _budget_antirug["n"] >= 4:
        return None
    _budget_antirug["n"] += 1
    try:
        return antirug.analyser(token)
    except Exception as ex:
        return {"erreur": str(ex)[:100]}


# ---------- 4. Suivi des positions ----------
def evaluer(pos, u, maintenant):
    ent = pos["prix_entree"]
    tous = [x for x in u["pts"] if x[2] == 0 and pos["entree_t"] < x[0]]
    pts = [x for x in tous if x[0] <= pos["entree_t"] + DUREE_MAX_H * 3600]
    for nom, v in SORTIES.items():
        r = pos["res"].setdefault(nom, {"ferme": False, "pnl": -FRAIS})
        if r["ferme"]:
            continue
        duree = v.get("max_h", DUREE_MAX_H)
        pts = [x for x in tous if x[0] <= pos["entree_t"] + duree * 3600]
        fini = maintenant >= pos["entree_t"] + duree * 3600
        # evts : chaque vente partielle ou totale, avec l'heure, la part vendue et le gain
        reste, pnl, k, evts, haut = 1.0, 0.0, 0, [], ent
        for x in pts:
            haut = max(haut, x[1])
            if v.get("suiveur") and k == len(v["tps"]) and reste > 1e-9 and x[1] <= haut * (1 - v["suiveur"]):
                g = x[1] / ent - 1
                evts.append({"t": x[0], "type": "stop suiveur", "part": round(reste, 4), "gain": round(g, 4)})
                pnl += reste * g; reste = 0; break
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
            evts.append({"t": pts[-1][0] if pts else maintenant, "type": f"sortie {duree} h", "part": round(reste, 4),
                         "gain": round(dernier, 4)})
        r["evts"], r["reste"], r["gain_latent"] = evts, round(reste, 4), round(dernier, 4)
        if reste <= 1e-9 or fini:
            r.update({"ferme": True, "pnl": round(pnl + reste * dernier - FRAIS, 4), "t_fin": evts[-1]["t"] if evts else maintenant})
        else:
            r["pnl"] = round(pnl + reste * dernier - FRAIS, 4)
    pts = [x for x in tous if x[0] <= pos["entree_t"] + DUREE_MAX_H * 3600]
    if pts:
        pos["mc_actuel"] = tous[-1][1]
        pos["haut"] = round(max(x[1] for x in pts) / ent - 1, 3)
        pos["bas"] = round(min(x[1] for x in pts) / ent - 1, 3)
        pos["nouveau_sommet"] = bool(pos.get("peak")) and any(x[1] > pos["peak"] for x in pts)


# ---------- 5. Rapport ----------
def rapport(positions, fermees, maintenant, n_univ, n_rel):
    L = [f"# Rapport du scanner (mis à jour le {iso(maintenant)} UTC)", "",
         "Paper trading uniquement : aucune vraie transaction.", "",
         f"Tokens suivis : {n_univ} (dont {n_rel} relevés à ce passage). Requêtes : {stats['dex']} Dexscreener, "
         f"{stats['gt']} GeckoTerminal ({stats['gt_echecs']} échecs).", "",
         "## Résultats par stratégie et par sortie", "",
         "| Stratégie | Sortie | Trades clôturés | Gagnants | Gain moyen | En cours |", "|---|---|---|---|---|---|"]
    for st in list(STRATEGIES) + (["C"] if STRATEGIE_C_ACTIVE else []):
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
                 f"{round(p['prix_entree'] / 1e6, 2)} M$ | {('x' + str(round(p['pump_x'], 1))) if p.get('pump_x') else '-'} | {round(100 * r['pnl'])} % | "
                 f"+{round(100 * p.get('haut', 0))} % | {round(100 * p.get('bas', 0))} % | "
                 f"{s['baleines_achat_usd']}/{s['baleines_vente_usd']} $ | {s['kol_achats']}/{s['kol_ventes']} | "
                 f"{p.get('holders_40')} → {s['holders']} | {statut} |")
    open(os.path.join(ICI, "RAPPORT.md"), "w").write("\n".join(L) + "\n")


def radar(univ, maintenant):
    """Tokens qui font le pattern (pump récent, sommet 1-15 M$) mais pas encore à -60 % : les plus proches d'abord."""
    r = []
    for t, u in univ.items():
        if u.get("maj") != maintenant:
            continue
        e = etat_retracement(u, maintenant)
        if not e or not (SOMMET_MIN <= e["peak"] <= SOMMET_MAX) or not (2 <= e["pump_x"] <= 10):
            continue
        h = (maintenant - e["peak_t"]) / 3600
        if h > 48 or e["dd"] >= 0.6:
            continue
        r.append({"sym": u["sym"], "pair": u["pair"], "token": t, "sommet": e["peak"], "mc": e["mc"], "pump_x": round(e["pump_x"], 1),
                  "baisse": round(e["dd"], 3), "h_depuis_sommet": round(h, 1), "liq": u.get("liq"),
                  "strategies": "B+G" if e["pump_x"] <= 5 else "G"})
    return sorted(r, key=lambda x: -x["baisse"])[:15]


def tableau_de_bord(positions, fermees, maintenant, n_univ, n_rel, rad=None):
    """Fichier léger lu par le site (tableau de bord Vercel)."""
    champs = ("token", "pair", "sym", "strat", "entree_t", "prix_entree", "ordre_limite", "mc_reel", "peak", "peak_t",
              "pump_x", "pump_h", "h_vers_entree", "liq", "x", "age_h", "holders_40", "sig", "res", "haut", "bas",
              "nouveau_sommet", "mc_actuel", "retard_min", "antirug")
    trades = [{k: p.get(k) for k in champs} for p in fermees + positions if p["strat"] != "C" or STRATEGIE_C_ACTIVE]
    json.dump({"maj": maintenant, "tokens_suivis": n_univ, "releves": n_rel, "frais": FRAIS,
               "strategies": STRATEGIES, "sorties": {k: {"sl": v["sl"], "tps": v["tps"], "suiveur": v.get("suiveur"), "max_h": v.get("max_h", DUREE_MAX_H)}
                           for k, v in SORTIES.items()},
               "flux_kol": len(trades_kol(maintenant - 3600)),
               "duree_max_h": DUREE_MAX_H, "trades": trades, "radar": rad or []},
              open(os.path.join(DATA, "tableau.json"), "w"), separators=(",", ":"))


def main():
    maintenant = int(time.time())
    univ = charger("univers.json", {})
    if not os.path.exists(os.path.join(DATA, ".llama_v2")):
        for u in univ.values():
            if u.get("histo") != "llama":
                u["histo_essai"] = False
        open(os.path.join(DATA, ".llama_v2"), "w").write("1")
    positions = charger("positions.json", [])
    fermees = charger("fermees.json", [])
    deja = {(p["token"], p["strat"], p["peak_t"]) for p in positions + fermees}
    kol_map = kols()
    for p in positions:
        if p["token"] in univ:
            univ[p["token"]]["en_position"] = True

    decouvrir(univ, maintenant)
    n_rel = relever(univ, maintenant)
    a_completer = [t for t, u in univ.items() if not u.get("histo_essai") and u.get("ratio")]
    historique_llama(univ, a_completer, maintenant)
    ctx = contexte_marche(maintenant)
    flux = trades_kol(maintenant - 48 * 3600)

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
            if h < s["h_min"] or h > 48:
                continue
            # Si on avait déjà vu le token au-dessus du seuil, l'ordre limite aurait été posé et exécuté,
            # même si le prix a ensuite plongé bien plus bas (on compte alors la perte, pour rester honnête).
            # Sinon (token découvert déjà sous le seuil) : achat au prix actuel, et seulement s'il n'est
            # pas plus de 10 points sous le seuil (sinon c'est un rug déjà fini).
            vus = [x for x in u["pts"][:-1] if x[2] == 0 and x[0] >= e["peak_t"]]
            limite = e["peak"] * (1 - s["dd"])
            if vus and vus[-1][1] > limite:
                prix = limite
            elif e["dd"] <= s["dd"] + 0.10:
                prix = min(limite, e["mc"])
            else:
                continue
            sig = signaux(u, t, e["peak_t"], kol_map)
            ar = analyse_antirug(t)
            sig.update(kol_resume(flux, t, e["peak_t"], kol_map) if flux else
                       {"kols_acheteurs": None, "kols_vendeurs": None, "kols_achat_usd": None, "kols_vente_usd": None, "kols_noms": ""})
            pos = {"token": t, "pair": u["pair"], "sym": u["sym"], "strat": strat, "entree_t": maintenant,
                   "prix_entree": prix, "ordre_limite": prix == limite, "mc_reel": e["mc"], "peak": e["peak"], "peak_t": e["peak_t"],
                   "pump_x": round(e["pump_x"], 2), "pump_h": round((e["peak_t"] - e["pre_t"]) / 3600, 1),
                   "h_vers_entree": round(h, 1), "liq": u.get("liq"), "x": u.get("x"),
                   "age_h": round((maintenant - u["cree"]) / 3600) if u.get("cree") else None,
                   "holders_40": u.get("holders_40") if u.get("h40_peak") == e["peak_t"] else None,
                   "sig": sig, "antirug": ar, "contexte": {k: v for k, v in ctx.items() if k != "t"},
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

    # Stratégie C : au moins KOL_MIN KOLs différents achètent le même token dans la dernière heure
    recents = [x for x in flux if x["t"] >= maintenant - 3600 and x["d"] == "B" and x["usd"] >= KOL_ACHAT_MIN_USD]
    par_token = {}
    for x in recents:
        par_token.setdefault(x["tok"], set()).add(x["w"])
    deja_c = {p["token"] for p in positions + fermees if p["strat"] == "C" and maintenant - p["entree_t"] < 24 * 3600}
    for t, wallets in (par_token.items() if STRATEGIE_C_ACTIVE else []):
        if len(wallets) < KOL_MIN or t in deja_c:
            continue
        u = univ.get(t)
        if not u or u.get("maj") != maintenant or not u.get("pair"):
            continue   # pas encore de prix pour ce token : il sera pris au relevé suivant s'il est toujours en convergence
        mc = u["pts"][-1][1]
        trois = sorted(x["t"] for x in recents if x["tok"] == t)
        ar = analyse_antirug(t) if mc >= 100_000 else None
        sig = {"baleines_achat_usd": None, "baleines_vente_usd": None, "baleines_acheteurs": None, "baleines_vendeurs": None,
               "kol_achats": len(wallets), "kol_ventes": None, "kol_noms": "", "holders": None,
               **kol_resume(flux, t, maintenant - 3600, kol_map)}
        pos = {"token": t, "pair": u["pair"], "sym": u.get("sym") or "?", "strat": "C", "entree_t": maintenant,
               "prix_entree": mc, "ordre_limite": False, "mc_reel": mc, "peak": None, "peak_t": None,
               "pump_x": None, "pump_h": None, "h_vers_entree": None, "liq": u.get("liq"), "x": u.get("x"),
               "age_h": round((maintenant - u["cree"]) / 3600) if u.get("cree") else None,
               "retard_min": round((maintenant - trois[KOL_MIN - 1]) / 60) if len(trois) >= KOL_MIN else None,
               "holders_40": None, "sig": sig, "antirug": ar, "contexte": {k: v for k, v in ctx.items() if k != "t"},
               "res": {k: {"ferme": False, "pnl": -FRAIS} for k in SORTIES}}
        positions.append(pos)
        u["en_position"] = True
        ajouter_csv("entrees_c.csv", {"date": iso(maintenant), "token": t, "sym": pos["sym"], "mc_entree": round(mc),
                                      "liquidite": u.get("liq"), "age_h": pos["age_h"], "retard_min": pos["retard_min"],
                                      **{k: sig[k] for k in ("kols_acheteurs", "kols_vendeurs", "kols_achat_usd", "kols_vente_usd", "kols_noms")},
                                      **{"marche_" + k: v for k, v in pos["contexte"].items()}})

    restantes = []
    for p in positions:
        u = univ.get(p["token"])
        if u:
            evaluer(p, u, maintenant)
        duree_max = max(v.get("max_h", DUREE_MAX_H) for v in SORTIES.values())
        if all(v["ferme"] for v in p["res"].values()) or maintenant > p["entree_t"] + (duree_max + 6) * 3600:
            for v in p["res"].values():
                v["ferme"] = True
                v.setdefault("t_fin", maintenant)
            fermees.append(p)
            if p["token"] in univ:
                univ[p["token"]]["en_position"] = False
            ajouter_csv("sorties.csv", {"date_entree": iso(p["entree_t"]), "sym": p["sym"], "token": p["token"],
                                        "strat": p["strat"], **{k: p["res"].get(k, {}).get("pnl") for k in SORTIES},
                                        "haut_max": p.get("haut"), "bas_max": p.get("bas"),
                                        "nouveau_sommet": p.get("nouveau_sommet")})
        else:
            restantes.append(p)

    sauver("univers.json", univ)
    sauver("positions.json", restantes)
    sauver("fermees.json", fermees)
    rapport(restantes, fermees, maintenant, len(univ), n_rel)
    tableau_de_bord(restantes, fermees, maintenant, len(univ), n_rel, radar(univ, maintenant))
    print(f"univers {len(univ)}, relevés {n_rel}, ouvertes {len(restantes)}, clôturées {len(fermees)}, "
          f"requêtes dex {stats['dex']} gt {stats['gt']} (échecs {stats['gt_echecs']}) llama {stats['llama']}, "
          f"{sum(1 for u in univ.values() if u.get('histo') == 'llama')} tokens avec historique DefiLlama")


if __name__ == "__main__":
    main()
