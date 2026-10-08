# Scanner memecoins (paper trading)

Bot qui tourne toutes les 15 minutes sur GitHub Actions et teste **en direct, sans argent réel**, les stratégies B et G trouvées dans le backtest du 2026-10-08.

- **B** : achat fictif à -60 % sous le sommet, pump ≤ x5.
- **G** : achat fictif à -70 % sous le sommet, pump ≤ x10, pas de krach en moins de 2 h.
- Conditions communes : Solana, pump d'au moins x2 en 48 h, sommet entre 1 et 15 M$, retracement dans les 48 h.
- Trois façons de sortir suivies en parallèle : `50_x2_sl60` (moitié à +50 %, moitié à x2, stop -60 %), `30_60_sl60`, `50_x2_sl30`. Sortie forcée après 72 h. Frais de 3 % déduits.
- Au moment de l'entrée, le bot note : achats/ventes des baleines (> 2000 $) depuis le sommet, trades des KOLs (liste KOLscan dans `kols.csv`), nombre de holders, compte X.

## Fichiers
- `RAPPORT.md` : le tableau de bord, mis à jour à chaque passage.
- `data/entrees.csv` : chaque entrée avec ses signaux.
- `data/sorties.csv` : le résultat de chaque trade clôturé.
- `data/positions.json`, `data/fermees.json` : l'état du bot.

## Fonctionnement
- Découverte : Dexscreener (boosts, profils, takeovers) à chaque passage, classements GeckoTerminal une fois par heure.
- Relevé : Dexscreener (gratuit, sans clé) donne la market cap de tous les tokens suivis. Le bot construit son propre historique (un point toutes les 15 min). Au premier relevé d'un token, il reconstitue 3 points passés à partir des variations sur 1 h, 6 h et 24 h.
- Entrée : si le bot avait déjà vu le token au-dessus du seuil, il compte l'entrée au prix limite (-60 % ou -70 %). Si le token est découvert déjà sous le seuil (10 points maximum en dessous), il compte un achat au prix du moment.
- Les paliers et le stop sont vérifiés sur les relevés toutes les 15 min (les mèches plus courtes ne sont pas vues : résultat plutôt prudent).
- Signaux à l'entrée : trades de plus de 2000 $ depuis le sommet, dernières transactions croisées avec la liste KOLscan, et nombre de holders à -40 % puis à l'entrée (GeckoTerminal).
