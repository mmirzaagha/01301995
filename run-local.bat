@echo off
REM Windows: evdəki kompüterdən işlət (Python və Git quraşdırılmış olmalıdır)
cd /d "%~dp0"
git pull --rebase -q
pip install -q requests
python scripts\checker.py
git add -A
git diff --cached --quiet && (echo Deyisiklik yoxdur) || (git commit -qm "auto(local): playlist yenilendi" && git push)
pause
