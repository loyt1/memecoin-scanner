#!/bin/bash
# Fait tourner le scanner en continu pendant ~5 h 40 : un relevé toutes les 15 minutes.
# GitHub relance ce script automatiquement (voir .github/workflows/scan.yml).
set -u
# GitHub fige la version du code au moment de la réservation : on récupère la toute dernière version
# puis on relance ce script une fois, pour que la session tourne toujours avec le code à jour.
if [ -z "${BOUCLE_A_JOUR:-}" ]; then
  git pull -q --rebase origin main || git pull -q origin main || true
  BOUCLE_A_JOUR=1 exec bash boucle.sh
fi
# Dès le début, la session réserve la suivante : elle attend dans la file et démarre dès que celle-ci se termine.
# (GitHub ne garantit pas les relances programmées.)
gh workflow run scan.yml --ref main || echo "réservation impossible, la relance horaire prendra le relais"
git config user.name "scanner-bot"
git config user.email "scanner-bot@users.noreply.github.com"
# Écoute en continu le flux des trades de KOLs (stratégie C et signaux KOL), pendant toute la session
python ecoute_kols.py > /tmp/ecoute_kols.log 2>&1 &
ECOUTE=$!
trap 'kill $ECOUTE 2>/dev/null; pkill -f kolscan-api 2>/dev/null' EXIT
fin=$(( $(date +%s) + 5 * 3600 + 40 * 60 ))
n=0
while [ "$(date +%s)" -lt "$fin" ]; do
  debut=$(date +%s)
  git pull -q --rebase || true
  python scanner.py || echo "erreur pendant le relevé"
  git add -A data RAPPORT.md
  # le site Vercel se met à jour un relevé sur deux (toutes les 30 min, limite de 100 déploiements par jour)
  n=$((n + 1)); tag=""
  if [ $((n % 2)) -eq 1 ]; then tag=" [deploy]"; fi
  git diff --cached --quiet || git commit -qm "scan $(date -u +'%Y-%m-%d %H:%M')$tag"
  git push -q || { git pull -q --rebase && git push -q; } || echo "envoi raté, nouvel essai au prochain relevé"
  reste=$(( 900 - ($(date +%s) - debut) ))
  if [ "$reste" -gt 0 ]; then sleep "$reste"; fi
done
