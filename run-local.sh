#!/usr/bin/env bash
# Evdəki kompüterdən / Raspberry Pi / Termux-dan işlət: AZ/TR linkləri də düzgün yoxlanır.
# İlk dəfə: repo-nu klonla (git clone ...), sonra bu faylı çalışdır. Cron ilə gündə 1-2 dəfə də qura bilərsən.
set -e
cd "$(dirname "$0")"
git pull --rebase -q
pip install -q requests
IPTV_COUNTRY="${IPTV_COUNTRY:-}" python3 scripts/checker.py
git add -A
if git diff --cached --quiet; then echo "Dəyişiklik yoxdur"; else
  git commit -qm "auto(local): playlist yeniləndi $(date -u +'%Y-%m-%d %H:%M')" && git push
fi
