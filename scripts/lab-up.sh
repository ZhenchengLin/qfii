#!/usr/bin/env bash
# Update and (re)start the Qfii debate viewer in the Outshift AGNTCY lab.
# Open it at https://<your-lab>.labs.outshift.com:6100 (the lab proxies :6100 -> :3000).
# Usage (inside the lab, from the qfii folder):  bash scripts/lab-up.sh
set -euo pipefail
cd "$(dirname "$0")/.."
pkill -f "configs/outshift-lab.toml ui" 2>/dev/null || true
git pull -q
[ -d .venv ] || { uv python install 3.12 -q && uv venv -q -p 3.12 .venv; }
uv pip install -q -p .venv -e .
[ -f data/prices/SPY.csv ] || .venv/bin/qfii data fetch
nohup .venv/bin/qfii --config configs/outshift-lab.toml ui --host 127.0.0.1 --port 3000 \
  > data/ui.log 2>&1 &
sleep 3
curl -fsS -o /dev/null localhost:3000/ && echo "Qfii UI running ($(git log --oneline -1))"
