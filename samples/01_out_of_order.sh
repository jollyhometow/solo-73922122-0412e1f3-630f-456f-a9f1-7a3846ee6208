#!/usr/bin/env bash
# 样例 1：乱序到达（子先父后）→ 观测标待补齐、不出有效值 → 父到达后自动重算。
# 同时演示：写 api-a、读 api-b，两个共库进程结论一致。
set -euo pipefail

API_A="${API_A:-http://localhost:8001}"
API_B="${API_B:-http://localhost:8002}"

pretty() {
  if command -v jq >/dev/null 2>&1; then jq .;
  elif command -v python3 >/dev/null 2>&1; then python3 -m json.tool;
  else cat; fi
}

echo "== 1) 子修订 rev-202 先到达（父 rev-201 尚未到达），经 api-a 上报 =="
curl -sS -X POST "$API_A/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-202", "revision_id": "rev-202", "parents": ["rev-201"],
     "species": "Lynx lynx", "count": 2}
  ]
}' | pretty

echo
echo "== 2) 从 api-b 查询：status=pending（待补齐），effective=null，missing_ancestors=[rev-201] =="
curl -sS "$API_B/observations/OBS-202" | pretty

echo
echo "== 3) 父修订 rev-201 随后到达，自动重算 =="
curl -sS -X POST "$API_A/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-202", "revision_id": "rev-201", "parents": [],
     "species": "Lynx lynx", "count": 1}
  ]
}' | pretty

echo
echo "== 4) 再从 api-b 查询：status=valid，有效值取自当前分支（顶端修订 rev-202） =="
curl -sS "$API_B/observations/OBS-202" | pretty
