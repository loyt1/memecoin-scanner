"""Grille sur l'univers sans biais (tokens gradués), avec découpage apprentissage / test par date."""
import itertools, json, time, sys
from bt import charger_gradues, precalc, signaux_entree, sortie, resume, bootstrap

TOK = charger_gradues()
for t in TOK.values():
    precalc(t)
tous_t = sorted(e[0] for tok in TOK.values() for e in tok['c'])
COUPE = float(sys.argv[1]) if len(sys.argv) > 1 else (tous_t[0] + tous_t[-1]) / 2   # milieu de la période
print(len(TOK), 'tokens', flush=True)
BASE = {'sommet_min': 1e6, 'sommet_max': 15e6, 'pump_min': 2, 'vol_min': 10_000}
ENTREES = {}
for dd, pm, hm, age, fr in itertools.product([0.3, 0.4, 0.5, 0.6, 0.7, 0.8], [5, 10, 30], [0, 2, 6, 12], [0, 24, 40], [0, 1]):
    ENTREES[f'dd{int(dd*100)}_p{pm}_h{hm}_a{age}' + ('_frais' if fr else '')] = {**BASE, 'dd': dd, 'pump_max': pm, 'h_min': hm, 'age_min': age, 'sommet_frais': fr}
for smin, smax in ((3e5, 1e6), (1e6, 5e6), (5e6, 15e6), (15e6, 1e9)):
    for dd in (0.5, 0.6, 0.7):
        ENTREES[f'taille{int(smin/1e5)}-{int(smax/1e5)}_dd{int(dd*100)}'] = {**BASE, 'sommet_min': smin, 'sommet_max': smax, 'dd': dd, 'pump_max': 30, 'h_min': 0, 'age_min': 0}
SORTIES = {
    '50_x2_sl60': {'sl': 0.6, 'tps': [(0.5, 0.5), (1.0, 0.5)], 'max_h': 72},
    '30_60_sl60': {'sl': 0.6, 'tps': [(0.3, 0.5), (0.6, 0.5)], 'max_h': 72},
    '50_x2_sl30': {'sl': 0.3, 'tps': [(0.5, 0.5), (1.0, 0.5)], 'max_h': 72},
    '50_x2_sl40': {'sl': 0.4, 'tps': [(0.5, 0.5), (1.0, 0.5)], 'max_h': 72},
    '50_x2_sans_stop': {'sl': None, 'tps': [(0.5, 0.5), (1.0, 0.5)], 'max_h': 72},
    '20_40_sl40': {'sl': 0.4, 'tps': [(0.2, 0.5), (0.4, 0.5)], 'max_h': 72},
    '15_30_sl30': {'sl': 0.3, 'tps': [(0.15, 0.5), (0.3, 0.5)], 'max_h': 72},
    '30_tout_sl40': {'sl': 0.4, 'tps': [(0.3, 1.0)], 'max_h': 72},
    '20_tout_sl20_24h': {'sl': 0.2, 'tps': [(0.2, 1.0)], 'max_h': 24},
    '50_x2_sl60_24h': {'sl': 0.6, 'tps': [(0.5, 0.5), (1.0, 0.5)], 'max_h': 24},
    'x2_x3_sl60': {'sl': 0.6, 'tps': [(1.0, 0.5), (2.0, 0.5)], 'max_h': 168},
    'moonbag': {'sl': 0.6, 'tps': [(0.5, 0.4), (1.0, 0.4)], 'suiveur': 0.4, 'max_h': 168},
}
for k in list(ENTREES):
    ENTREES[k + '_decl'] = {**ENTREES[k], 'declencheur': True}
res = []
for ne, s in ENTREES.items():
    ents = [(t, e) for t, tok in TOK.items() for e in signaux_entree(tok, s)]
    for ns, x in SORTIES.items():
        g = [(sortie(TOK[t], e, x, True), e, t) for t, e in ents]
        r = {'entree': ne, 'sortie': ns, 'tous': resume([a for a, _, _ in g]), 'tokens': len({t for _, _, t in g}),
             'ic90': bootstrap([a for a, _, _ in g])}
        if COUPE:
            r['avant'] = resume([a for a, e, _ in g if e['t'] < COUPE])
            r['apres'] = resume([a for a, e, _ in g if e['t'] >= COUPE])
        res.append(r)
json.dump(res, open('etat/resultats2.json', 'w'))
print('fini')
