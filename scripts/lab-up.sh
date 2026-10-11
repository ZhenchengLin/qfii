#!/usr/bin/env bash
# Update and (re)start the Qfii debate viewer in the Outshift AGNTCY lab.
# Open it at https://<your-lab>.labs.outshift.com:6100 (the lab proxies :6100 -> :3000).
# Usage (inside the lab, from the qfii folder):  bash scripts/lab-up.sh
# The website shows the week replay (configs/week.toml); QFII_CONFIG overrides it.
set -euo pipefail
cd "$(dirname "$0")/.."
CFG="${QFII_CONFIG:-configs/week.toml}"
pkill -f "ui --host 127.0.0.1 --port 3000" 2>/dev/null || true
git pull -q
# SEC identity and other local settings live in .env.local (gitignored)
[ -f .env.local ] && { set -a; . ./.env.local; set +a; }
[ -d .venv ] || { uv python install 3.12 -q && uv venv -q -p 3.12 .venv; }
# Docling needs torch: install the small CPU-only build first (no GPU in the lab)
.venv/bin/python -c "import torch" 2>/dev/null || \
  uv pip install -q -p .venv torch --index-url https://download.pytorch.org/whl/cpu
uv pip install -q -p .venv -e '.[sec,docling]'
[ -f data/prices/SPY.csv ] || .venv/bin/qfii data fetch
# the week dataset (10 random Nasdaq-100 stocks, 10-minute bars, SEC filings)
[ -f data/week/manifest.json ] || .venv/bin/qfii --config "$CFG" week build
nohup .venv/bin/qfii --config "$CFG" ui --host 127.0.0.1 --port 3000 \
  > data/ui.log 2>&1 &
sleep 3
curl -fsS -o /dev/null localhost:3000/ && echo "Qfii UI running ($(git log --oneline -1))"
