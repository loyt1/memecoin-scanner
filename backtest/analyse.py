from bt import *
import statistics, itertools
T = charger_gradues()
for t in T.values(): precalc(t)
X = {'50_x2_sl60': {'sl': 0.6, 'tps': [(0.5, 0.5), (1.0, 0.5)], 'max_h': 72},
     '50_x2_sl30': {'sl': 0.3, 'tps': [(0.5, 0.5), (1.0, 0.5)], 'max_h': 72},
     'x2_x3_sl60': {'sl': 0.6, 'tps': [(1.0, 0.5), (2.0, 0.5)], 'max_h': 168},
     'sans_stop': {'sl': None, 'tps': [(0.5, 0.5), (1.0, 0.5)], 'max_h': 72}}
def run(s, x):
    g = []
    for t, tok in T.items():
        for e in signaux_entree(tok, s):
            g.append((e['t'], sortie(tok, e, x, True), t))
    return g
print('Gain moyen par trade (ordre déclencheur, stop pessimiste). Cellule = moy [n trades / n tokens]')
for xn in ('50_x2_sl60', '50_x2_sl30', 'x2_x3_sl60'):
    print('\nSortie', xn)
    print('dd \\ h_min  ' + ''.join(f'{h:>20}' for h in (0, 1, 2, 4, 6, 12)))
    for dd in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
        row = f'{dd:<12}'
        for h in (0, 1, 2, 4, 6, 12):
            s = {'sommet_min': 1e6, 'sommet_max': 15e6, 'pump_min': 2, 'pump_max': 30, 'dd': dd, 'h_min': h, 'age_min': 0, 'declencheur': True}
            g = run(s, X[xn])
            row += f"{(sum(a for _, a, _ in g) / len(g) if g else 0):+.2f} [{len(g)}/{len({t for *_, t in g})}]".rjust(20)
        print(row)
