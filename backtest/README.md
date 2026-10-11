# Grand backtest sans biais

Univers : tous les tokens gradués de pump.fun sur 11 jours (morts compris), pas seulement ceux encore actifs.
Lancé par le workflow « backtest » (GitHub Actions), sur la branche `backtest` uniquement : le bot de `main` n'est pas touché.

- `pipeline.py` : télécharge les données (reprend là où il s'est arrêté, relancer le workflow si « PARTIEL »).
- `bt.py` : moteur (ordre limite sur la mèche, stop au pire prix en cas de rug, rug par retrait de liquidité = -100 %).
- `grille2.py` : ~5 000 combinaisons entrée × sortie, avec découpage apprentissage / test par date.
- `etat/` : données et résultats.
