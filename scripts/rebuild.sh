#!/bin/bash
# 一键重建大肥鱼记忆星图：抓取 → 归一化 → 布局 → 写 web/
# 用法：  ./scripts/rebuild.sh          只重建数据
#        ./scripts/rebuild.sh --serve  重建并确认 8123 在跑
set -euo pipefail
cd "$(dirname "$0")/.."

python3 tools/dsh_to_jsonl.py --max-per-session 1500
python3 src/build.py --sources sources.json --out web/starmap.json

if [ "${1:-}" = "--serve" ]; then
  if ! lsof -nP -iTCP:8123 -sTCP:LISTEN >/dev/null 2>&1; then
    nohup python3 -m http.server 8123 --bind 127.0.0.1 -d web \
      > /tmp/memory-starmap-http.log 2>&1 &
    echo "server started: http://127.0.0.1:8123"
  else
    echo "server already on http://127.0.0.1:8123"
  fi
fi

echo "打开 http://127.0.0.1:8123 （刷新即可看到新数据）"
