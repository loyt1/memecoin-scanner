"""Bilan du paper trading B/G : à lancer à la main (python bilan.py), pas par le bot.

Compare les trades clôturés au backtest, puis découpe les résultats par signal (anti-rug, âge, KOLs, baleines...)
pour voir quel filtre aurait amélioré la stratégie. Écrit ../bilan-AAAA-MM-JJ.md (dans le cerveau).
Attention : un découpage sur moins de 10 trades ne prouve rien. On ne change qu'une variable à la fois.
"""
import json
import os
import statistics
from datetime import datetime, timezone

ICI = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ICI, "data")
BACKTEST = {"B": 0.17, "G": 0.31}       # gain moyen par trade du backtest (sortie 50_x2_sl60)
SORTIE_REF = "50_x2_sl60"


def charger(nom):
    p = os.path.join(DATA, nom)
    return json.load(open(p)) if os.path.exists(p) else []


def stats(gains):
    if not gains:
        return "| 0 | - | - | - | - | - |"
    n = len(gains)
    return (f"| {n} | {round(100 * sum(g > 0 for g in gains) / n)} % | {round(100 * sum(gains) / n, 1)} % | "
            f"{round(100 * statistics.median(gains), 1)} % | {round(100 * min(gains))} % | {round(100 * max(gains))} % |")


ENTETE = "| Groupe | Trades | Gagnants | Gain moyen | Médiane | Pire | Meilleur |\n|---|---|---|---|---|---|---|"


def tableau(titre, trades, groupes, sortie=SORTIE_REF):
    L = [f"### {titre}", "", ENTETE]
    for nom, f in groupes:
        g = [p["res"][sortie]["pnl"] for p in trades if f(p)]
        L.append(f"| {nom} " + stats(g))
    return L + [""]


def sig(p, k):
    return (p.get("sig") or {}).get(k)


def capital(trades, sortie, mise=0.05, depart=5000):
    """Portefeuille fictif : mise fixe de 5 % du capital de départ par trade, dans l'ordre des entrées."""
    c = depart
    for p in sorted(trades, key=lambda x: x["entree_t"]):
        c += depart * mise * p["res"][sortie]["pnl"]
    return round(c)


def main():
    fermees = [p for p in charger("fermees.json") if p["strat"] in ("B", "G")]
    ouvertes = [p for p in charger("positions.json") if p["strat"] in ("B", "G")]
    invalides = charger("invalides.json")
    sorties = list(fermees[0]["res"]) if fermees else [SORTIE_REF]
    jour = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    L = [f"# Bilan du bot memecoins ({jour})", "",
         f"Trades B/G clôturés : {len(fermees)}. Ouverts : {len(ouvertes)}. Retirés (pool sans liquidité) : {len(invalides)}.",
         "Moins de 30 trades par stratégie = tendance, pas preuve.", "",
         "## 1. Résultats contre le backtest", "",
         "| Stratégie | Sortie | Trades | Gagnants | Gain moyen | Médiane | Pire | Meilleur | Backtest | 5000 $ deviennent |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for st in ("B", "G"):
        t = [p for p in fermees if p["strat"] == st]
        for so in sorties:
            g = [p["res"][so]["pnl"] for p in t]
            bt = f"+{round(100 * BACKTEST[st])} %" if so == SORTIE_REF else ""
            L.append(f"| {st} | {so} " + stats(g) + f" {bt} | {capital(t, so)} $ |")
    L += ["", "## 2. Quel filtre aurait aidé ? (B et G ensemble, sortie 50 % / x2, stop -60 %)", ""]
    T = fermees
    ar = lambda p: (p.get("antirug") or {})
    L += tableau("Anti-rug", T, [
        ("suspect", lambda p: ar(p).get("suspect") is True),
        ("sain", lambda p: ar(p).get("suspect") is False),
        ("pas analysé", lambda p: "suspect" not in ar(p))])
    L += tableau("Âge du token", T, [
        ("< 40 h", lambda p: (p.get("age_h") or 0) < 40), ("40-100 h", lambda p: 40 <= (p.get("age_h") or 0) < 100),
        ("≥ 100 h", lambda p: (p.get("age_h") or 0) >= 100)])
    L += tableau("Les deux filtres du tableau de bord (anti-rug sain ET ≥ 40 h)", T, [
        ("passe les filtres", lambda p: ar(p).get("suspect") is not True and (p.get("age_h") or 0) >= 40),
        ("bloqué par les filtres", lambda p: not (ar(p).get("suspect") is not True and (p.get("age_h") or 0) >= 40))])
    L += tableau("Type d'entrée", T, [
        ("ordre limite (vu au-dessus du seuil)", lambda p: p.get("ordre_limite")),
        ("achat au marché (découvert sous le seuil)", lambda p: not p.get("ordre_limite"))])
    L += tableau("Taille du pump", T, [
        ("x2-x3", lambda p: (p.get("pump_x") or 0) < 3), ("x3-x5", lambda p: 3 <= (p.get("pump_x") or 0) < 5),
        ("x5-x10", lambda p: (p.get("pump_x") or 0) >= 5)])
    L += tableau("Délai sommet → entrée", T, [
        ("< 6 h", lambda p: (p.get("h_vers_entree") or 0) < 6), ("6-24 h", lambda p: 6 <= (p.get("h_vers_entree") or 0) < 24),
        ("≥ 24 h", lambda p: (p.get("h_vers_entree") or 0) >= 24)])
    L += tableau("Liquidité à l'entrée", T, [
        ("< 30 k$", lambda p: (p.get("liq") or 0) < 30e3), ("30-100 k$", lambda p: 30e3 <= (p.get("liq") or 0) < 100e3),
        ("≥ 100 k$", lambda p: (p.get("liq") or 0) >= 100e3)])
    L += tableau("KOLs depuis le sommet (flux KOLscan)", T, [
        ("des KOLs ont acheté le dip", lambda p: (sig(p, "kols_acheteurs") or 0) > 0),
        ("aucun KOL acheteur", lambda p: not (sig(p, "kols_acheteurs") or 0))])
    L += tableau("Baleines (trades > 2000 $ depuis le sommet)", T, [
        ("plus d'achats que de ventes", lambda p: (sig(p, "baleines_achat_usd") or 0) > (sig(p, "baleines_vente_usd") or 0)),
        ("plus de ventes", lambda p: (sig(p, "baleines_achat_usd") or 0) <= (sig(p, "baleines_vente_usd") or 0))])
    L += tableau("Holders entre -40 % et l'entrée", T, [
        ("en hausse", lambda p: p.get("holders_40") and sig(p, "holders") and sig(p, "holders") > p["holders_40"]),
        ("stables ou en baisse", lambda p: p.get("holders_40") and sig(p, "holders") and sig(p, "holders") <= p["holders_40"]),
        ("inconnu", lambda p: not (p.get("holders_40") and sig(p, "holders")))])
    L += tableau("Compte X", T, [("oui", lambda p: p.get("x")), ("non", lambda p: not p.get("x"))])
    L += tableau("Marché (volume PumpSwap sur 7 jours)", T, [
        ("en hausse", lambda p: ((p.get("contexte") or {}).get("pumpswap_var7j") or 0) > 0),
        ("en baisse", lambda p: ((p.get("contexte") or {}).get("pumpswap_var7j") or 0) <= 0)])
    L += ["## 3. Détail des trades clôturés", "",
          "| Token | Strat | Entrée (UTC) | MC entrée | Pump | Âge | Anti-rug | Haut | Bas | " + " | ".join(sorties) + " |",
          "|---|---|---|---|---|---|---|---|---|" + "---|" * len(sorties)]
    for p in sorted(T, key=lambda x: x["entree_t"]):
        L.append(f"| {p['sym']} | {p['strat']} | {datetime.fromtimestamp(p['entree_t'], timezone.utc).strftime('%m-%d %H:%M')} | "
                 f"{round(p['prix_entree'] / 1e6, 2)} M$ | x{p.get('pump_x')} | {p.get('age_h')} h | "
                 f"{'suspect : ' + ar(p).get('raisons', '') if ar(p).get('suspect') else ('sain' if 'suspect' in ar(p) else '-')} | "
                 f"+{round(100 * (p.get('haut') or 0))} % | {round(100 * (p.get('bas') or 0))} % | "
                 + " | ".join(f"{round(100 * p['res'][s]['pnl'])} %" for s in sorties) + " |")
    if ouvertes:
        L += ["", "## 4. Positions encore ouvertes (gain latent, sortie 50 % / x2)", ""]
        for p in ouvertes:
            L.append(f"- {p['sym']} ({p['strat']}) : {round(100 * p['res'][SORTIE_REF]['pnl'])} %")
    sortie = os.path.join(ICI, "..", f"bilan-{jour}.md")
    open(sortie, "w").write("\n".join(L) + "\n")
    print("écrit :", os.path.abspath(sortie))


if __name__ == "__main__":
    main()
