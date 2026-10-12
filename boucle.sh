#!/bin/bash
# Fait tourner le scanner en continu pendant ~5 h 40 : un relevé toutes les 15 minutes.
# GitHub relance ce script automatiquement (voir .github/workflows/scan.yml).
#
# Le code est sur la branche main, les données sur la branche « data » (dossier data/, un « worktree » git).
# Ainsi les relevés ne touchent jamais main : Vercel (qui surveille main) ne réagit qu'aux changements de code.
set -u
# GitHub fige la version du code au moment de la réservation : on récupère la toute dernière version
# puis on relance ce script une fois, pour que la session tourne toujours avec le code à jour.
if [ -z "${BOUCLE_A_JOUR:-}" ]; then
  git pull -q --rebase --autostash origin main || git pull -q origin main || true
  BOUCLE_A_JOUR=1 exec bash boucle.sh
fi
git config user.name "scanner-bot"
git config user.email "scanner-bot@users.noreply.github.com"
# Données : branche data, récupérée dans le dossier data/
git fetch -q --depth=1 origin data
git worktree add -q -B data data origin/data || { echo "impossible de récupérer la branche data"; exit 1; }
git -C data config user.name "scanner-bot"
git -C data config user.email "scanner-bot@users.noreply.github.com"
# Dès le début, la session réserve la suivante : elle attend dans la file et démarre dès que celle-ci se termine.
# (GitHub ne garantit pas les relances programmées.)
gh workflow run scan.yml --ref main || echo "réservation impossible, la relance horaire prendra le relais"
# Écoute en continu le flux des trades de KOLs (signaux KOL), pendant toute la session
python ecoute_kols.py > /tmp/ecoute_kols.log 2>&1 &
ECOUTE=$!
trap 'kill $ECOUTE 2>/dev/null; pkill -f kolscan-api 2>/dev/null' EXIT
fin=$(( $(date +%s) + 5 * 3600 + 40 * 60 ))
n=0
while [ "$(date +%s)" -lt "$fin" ]; do
  debut=$(date +%s)
  # code à jour (--autostash : rien ne doit bloquer la mise à jour) et données à jour (si quelqu'un a corrigé des données)
  git pull -q --rebase --autostash origin main || true
  git -C data pull -q --rebase --autostash origin data || true
  python scanner.py || echo "erreur pendant le relevé"
  git -C data add -A .
  # la copie des données sur Vercel n'est qu'un secours : le site lit la branche data sur GitHub
  n=$((n + 1))
  git -C data diff --cached --quiet || git -C data commit -qm "scan $(date -u +'%Y-%m-%d %H:%M')"
  git -C data push -q origin HEAD:data || { git -C data pull -q --rebase --autostash origin data && git -C data push -q origin HEAD:data; } || echo "envoi raté, nouvel essai au prochain relevé"
  reste=$(( 900 - ($(date +%s) - debut) ))
  if [ "$reste" -gt 0 ]; then sleep "$reste"; fi
done
