"""Backtest honnête sur bougies de 15 min (GeckoTerminal), univers = tous les tokens vus par le bot (morts compris).

Entrée : ordre limite posé à sommet × (1 - dd) dès que le pattern est confirmé (pump, sommet 1-15 M$) ; rempli si le
plus bas d'une bougie touche le prix limite (au prix d'ouverture si le prix a sauté par-dessus).
Sortie : stop testé avant les paliers dans une même bougie (prudent). Stop rempli au min(stop, ouverture) ;
mode pessimiste : si la bougie clôture sous la moitié du stop (rug), vente au prix de clôture.
"""
import json, os, statistics

D = os.path.dirname(os.path.abspath(__file__))
FRAIS = 0.03


def charger():
    U = json.load(open(f'{D}/univers_total.json'))
    R = json.load(open(f'{D}/ratios.json')) if os.path.exists(f'{D}/ratios.json') else {}
    PV = json.load(open(f'{D}/premier_vu.json'))
    LQ = json.load(open(f'{D}/liq_now.json')) if os.path.exists(f'{D}/liq_now.json') else {'t': 0, 'liq': {}}
    toks = {}
    for t in U:
        f = f'{D}/ohlcv/{t}.json'
        if not os.path.exists(f):
            continue
        c = json.load(open(f))
        if len(c) < 20:
            continue
        ratio = R.get(t) or (1e9 if t.endswith('pump') or t.endswith('bonk') else None)
        if not ratio:
            continue
        c = sorted(c)  # [ts, o, h, l, c, v]
        toks[t] = {'sym': U[t].get('sym'), 'c': [[x[0], x[1] * ratio, x[2] * ratio, x[3] * ratio, x[4] * ratio, x[5]] for x in c],
                   'vu': PV.get(t), 'debut': c[0][0], 'liq_now': LQ['liq'].get(t, 0), 'fin_donnees': LQ['t']}
    return toks


def precalc(tok):
    """Pour chaque bougie i : sommet des 48 h (clôtures), pump qui l'a précédé, volume 24 h. Calculé une fois."""
    c = tok['c']
    from collections import deque
    dq, info, vol, v24 = deque(), [], 0.0, deque()
    for i, b in enumerate(c):
        while dq and c[dq[0]][0] < b[0] - 48 * 3600:
            dq.popleft()
        while dq and c[dq[-1]][4] <= b[4]:
            dq.pop()
        dq.append(i)
        v24.append(b[5]); vol += b[5]
        while len(v24) > 96:
            vol -= v24.popleft()
        info.append([dq[0], vol])
    # pump : min des clôtures dans les 48 h avant le sommet k (mémo par k)
    mins = {}
    for i in range(len(c)):
        k = info[i][0]
        if k not in mins:
            a = k
            while a > 0 and c[a - 1][0] >= c[k][0] - 48 * 3600:
                a -= 1
            b = k
            while b > 0 and c[b - 1][0] >= c[k][0] - 7 * 86400:
                b -= 1
            mins[k] = (min(c[j][4] for j in range(a, k + 1)), max([c[j][4] for j in range(b, k)] or [0]))
        info[i] += list(mins[k])
    tok['info'] = info


def signaux_entree(tok, s):
    """Liste des entrées (une par sommet) pour une règle d'entrée s."""
    c, info = tok['c'], tok['info']
    out, deja = [], set()
    for i in range(1, len(c) - 1):
        k, vol24, pre, ath7 = info[i]
        pk, pk_t = c[k][4], c[k][0]
        if pk_t in deja or not (s['sommet_min'] <= pk <= s['sommet_max']) or pre <= 0:
            continue
        if s.get('sommet_frais') and pk < ath7:
            continue     # le « sommet » n'est qu'un rebond d'un token qui descend depuis des jours
        pump = pk / pre
        if not (s['pump_min'] <= pump <= s['pump_max']):
            continue
        T = c[i][0] + 900
        if (T - pk_t) / 3600 > 48 or (T - tok['debut']) / 3600 < s['age_min']:
            continue
        lim = pk * (1 - s['dd'])
        if c[i][4] <= lim:
            continue
        n = c[i + 1]
        if (n[0] - pk_t) / 3600 < s['h_min'] or n[3] > lim:
            continue
        if vol24 < s.get('vol_min', 10_000):
            continue
        out.append({'i': i + 1, 't': n[0], 'prix': min(lim, n[1]), 'pk': pk, 'pk_t': pk_t, 'pump': pump,
                    'age': (T - tok['debut']) / 3600, 'h': (n[0] - pk_t) / 3600, 'vol24': vol24,
                    'apres_vu': tok['vu'] is not None and n[0] >= tok['vu']})
        deja.add(pk_t)
    return out


def sortie(tok, e, x, pessimiste=False):
    """x = {'sl': 0.6 ou None, 'tps': [(gain, part)...], 'max_h': 72, 'suiveur': None}"""
    c = tok['c']
    ent = e['prix']
    reste, pnl, k, haut = 1.0, 0.0, 0, ent
    fin = e['t'] + x['max_h'] * 3600
    # la bougie d'entrée elle-même : on ne regarde que sa clôture pour le stop (on vient d'acheter dedans)
    der = c[e['i']][4]
    for b in c[e['i'] + 1:]:
        if b[0] >= fin:
            break
        ts, o, hi, lo, cl, v = b
        der = cl
        if x.get('sl') is not None:
            st = ent * (1 - x['sl'])
            if lo <= st:
                f = min(st, o)
                if pessimiste and cl < st * 0.5:
                    f = min(f, cl)
                pnl += reste * (f / ent - 1); reste = 0
                break
        if x.get('suiveur') and k == len(x['tps']) and lo <= haut * (1 - x['suiveur']):
            f = min(haut * (1 - x['suiveur']), o)
            pnl += reste * (f / ent - 1); reste = 0
            break
        while k < len(x['tps']) and hi >= ent * (1 + x['tps'][k][0]):
            p = min(reste, x['tps'][k][1]); pnl += p * x['tps'][k][0]; reste -= p; k += 1
        haut = max(haut, hi)
        if reste <= 1e-9:
            break
    # rug par retrait de liquidité : plus aucune bougie (personne ne peut vendre) et pool vide aujourd'hui
    dern_bougie = c[-1][0]
    if reste > 1e-9 and tok.get('liq_now', 0) < 5000 and min(fin, tok.get('fin_donnees', 0)) - dern_bougie > 2 * 3600:
        der = 0
    pnl += reste * (der / ent - 1)
    return pnl - FRAIS


def resume(g):
    if not g:
        return {'n': 0}
    return {'n': len(g), 'gagnants': round(sum(x > 0 for x in g) / len(g), 2), 'moy': round(sum(g) / len(g), 3),
            'med': round(statistics.median(g), 3), 'pire': round(min(g), 2)}


def charger_gradues():
    """Univers sans biais : tous les tokens gradués de pump.fun sur 11 jours (ATH >= 800 k$ selon pump.fun)."""
    import gzip
    E = f'{D}/etat'
    A = json.load(open(f'{E}/ath.json'))
    O = json.load(gzip.open(f'{E}/ohlcv.json.gz', 'rt'))
    LQ = json.load(open(f'{E}/liq_gradues.json')) if os.path.exists(f'{E}/liq_gradues.json') else {'t': 0, 'liq': {}}
    toks = {}
    for t, c in O.items():
        x = A.get(t) or {}
        if len(c) < 20:
            continue
        c = sorted(c)
        toks[t] = {'sym': x.get('symbol'), 'c': [[b[0], b[1] * 1e9, b[2] * 1e9, b[3] * 1e9, b[4] * 1e9, b[5]] for b in c],
                   'vu': c[0][0], 'debut': min(c[0][0], (x.get('created_timestamp') or 9e15) / 1000),
                   'liq_now': LQ['liq'].get(t, 0), 'fin_donnees': LQ['t']}
    return toks


def bootstrap(g, n=500):
    """Intervalle de confiance à 90 % du gain moyen (rééchantillonnage)."""
    import random
    if len(g) < 5:
        return None
    m = sorted(sum(random.choices(g, k=len(g))) / len(g) for _ in range(n))
    return round(m[int(0.05 * n)], 3), round(m[int(0.95 * n)], 3)
