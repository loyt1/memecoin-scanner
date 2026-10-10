"""Détecteur anti-rug : une « bubblemap » simplifiée, gratuite.

1. RugCheck (api.rugcheck.xyz, gratuit) : top holders, alertes, réseaux d'initiés détectés.
2. Pour chaque gros holder, on retrouve le wallet qui lui a envoyé ses premiers SOL (réseau public Solana).
   Plusieurs gros holders financés par le même wallet = un groupe relié (les bulles jaunes de Bubblemaps).
3. On mesure : la part du supply tenue par le plus gros groupe relié, la part tenue par des wallets neufs,
   la concentration du top 10 (hors pools de liquidité).

Les seuils sont des premières valeurs, à recalibrer au bilan avec les vrais trades.
"""
import json
import time
import urllib.error
import urllib.request

RPC = "https://api.mainnet-beta.solana.com"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{}/report"
NEUF_MAX_TX = 50          # un wallet avec moins de 50 transactions est considéré comme neuf
SEUILS = {"groupe_max_pct": 10, "groupe_max_wallets": 5, "neufs_pct": 25, "top10_pct": 40}
# Portefeuilles d'exchanges (Binance, Coinbase, OKX, Bybit, Kraken) : ils financent des milliers de wallets normaux,
# donc un financeur commun qui est un exchange ne relie pas les holders entre eux.
EXCHANGES = {"5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9", "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
             "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS", "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm",
             "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE", "5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD",
             "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2", "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5"}


def _post(url, data, essais=3):
    for _ in range(essais):
        try:
            req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "memecoin-scanner"})
            return json.load(urllib.request.urlopen(req, timeout=30))
        except Exception:
            time.sleep(2)
    return None


def _get(url, essais=3):
    for _ in range(essais):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "memecoin-scanner", "Accept": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            time.sleep(3)
        except Exception:
            time.sleep(3)
    return None


def rpc(methode, params):
    time.sleep(0.25)
    r = _post(RPC, {"jsonrpc": "2.0", "id": 1, "method": methode, "params": params})
    return (r or {}).get("result")


def financeur(wallet):
    """Renvoie (wallet qui a financé ce wallet, nombre de transactions). None si wallet ancien (>= 1000 tx)."""
    sigs = rpc("getSignaturesForAddress", [wallet, {"limit": 1000}])
    if not sigs:
        return None, 0
    n = len(sigs)
    if n >= 1000:
        return None, n
    for s in reversed(sigs[-3:]):          # les 3 plus anciennes transactions
        tx = rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        if not tx:
            continue
        cles = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
        meta = tx.get("meta") or {}
        if wallet not in cles:
            continue
        i = cles.index(wallet)
        pre, post = meta.get("preBalances", []), meta.get("postBalances", [])
        if i < len(pre) and pre[i] == 0 and post[i] > 0:
            # le wallet reçoit ses premiers SOL : le financeur est celui qui paie la transaction
            if cles[0] != wallet:
                return cles[0], n
            # sinon, chercher un transfert SOL vers le wallet
            for ins in tx["transaction"]["message"]["instructions"] + [
                    x for g in (meta.get("innerInstructions") or []) for x in g.get("instructions", [])]:
                info = (ins.get("parsed") or {}).get("info", {}) if isinstance(ins.get("parsed"), dict) else {}
                if info.get("destination") == wallet and info.get("source"):
                    return info["source"], n
    return None, n


def analyser(mint):
    rc = _get(RUGCHECK.format(mint)) or {}
    connus = set((rc.get("knownAccounts") or {}).keys())
    marches = {m.get("pubkey") for m in rc.get("markets") or []} | {
        (m.get("liquidityA") or "") for m in rc.get("markets") or []} | {(m.get("liquidityB") or "") for m in rc.get("markets") or []}
    holders = [h for h in rc.get("topHolders") or []
               if h.get("owner") not in connus and h.get("address") not in marches and h.get("owner") not in marches
               and (h.get("pct") or 0) >= 0.1]
    holders = holders[:20]
    res = {"rugcheck_score": rc.get("score_normalised"), "rugcheck_initiés": rc.get("graphInsidersDetected"),
           "rugcheck_alertes": " | ".join(f"{r.get('name')}" for r in rc.get("risks") or [])[:200],
           "mint_autorité": bool(rc.get("mintAuthority")), "gel_autorité": bool(rc.get("freezeAuthority")),
           "top10_pct": round(sum(h.get("pct", 0) for h in holders[:10]), 1), "holders_analysés": len(holders)}
    # groupes reliés par un financeur commun (ou un holder qui en finance un autre)
    parent = {}

    def racine(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    def unir(a, b):
        parent.setdefault(a, a); parent.setdefault(b, b)
        parent[racine(a)] = racine(b)

    neufs_pct, finance = 0.0, {}
    for h in holders:
        f, n = financeur(h["owner"])
        if n and n < NEUF_MAX_TX:
            neufs_pct += h.get("pct", 0)
        if f and f not in EXCHANGES:
            finance[h["owner"]] = f
            unir(h["owner"], f)
    pct = {h["owner"]: h.get("pct", 0) for h in holders}
    groupes = {}
    for w in pct:
        groupes.setdefault(racine(w), []).append(w)
    groupes = [g for g in groupes.values() if len(g) >= 2]
    compte = {}
    for f in finance.values():
        compte[f] = compte.get(f, 0) + 1
    res["financeur_principal"] = max(compte, key=compte.get) if compte else ""
    res.update({"groupes_reliés": len(groupes),
                "groupe_max_pct": round(max((sum(pct[w] for w in g) for g in groupes), default=0), 1),
                "groupe_max_wallets": max((len(g) for g in groupes), default=0),
                "neufs_pct": round(neufs_pct, 1)})
    raisons = []
    if res["groupe_max_pct"] >= SEUILS["groupe_max_pct"] or res["groupe_max_wallets"] >= SEUILS["groupe_max_wallets"]:
        raisons.append(f"{res['groupe_max_wallets']} gros holders reliés (même financeur) = {res['groupe_max_pct']} % du supply")
    if res["neufs_pct"] >= SEUILS["neufs_pct"]:
        raisons.append(f"wallets neufs = {res['neufs_pct']} % du supply")
    if res["top10_pct"] >= SEUILS["top10_pct"]:
        raisons.append(f"top 10 = {res['top10_pct']} % du supply")
    if res["mint_autorité"] or res["gel_autorité"]:
        raisons.append("le créateur peut encore créer ou geler des tokens")
    res["suspect"] = bool(raisons)
    res["raisons"] = " ; ".join(raisons)
    return res


if __name__ == "__main__":
    import sys
    for m in sys.argv[1:]:
        t = time.time()
        print(m[:8], json.dumps(analyser(m), ensure_ascii=False), f"({round(time.time() - t)} s)")
