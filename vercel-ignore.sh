#!/bin/bash
# Étape « Ignored Build Step » de Vercel : exit 0 = ne pas déployer, exit 1 = déployer.
# Limite Vercel : 100 déploiements par jour. Le site lit ses données sur GitHub, il n'a besoin d'être redéployé
# que quand le code change (et toutes les 2 h par sécurité, scans marqués [deploy]).
[ "$VERCEL_GIT_COMMIT_REF" != "main" ] && exit 0
git log -1 --pretty=%B | grep -q '\[deploy\]' && exit 1
[ "$(git log -1 --pretty=%an)" = "scanner-bot" ] && exit 0
git diff --quiet HEAD^ HEAD -- . ':(exclude)data' ':(exclude)RAPPORT.md' && exit 0 || exit 1
