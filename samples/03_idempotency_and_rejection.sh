#!/usr/bin/env bash
# 样例 3：同号同内容重传幂等；异内容 / 循环 / 跨观测引用整包拒绝、不留部分记录。
set -euo pipefail

API_A="${API_A:-http://localhost:8001}"
API_B="${API_B:-http://localhost:8002}"

pretty() {
  if command -v jq >/dev/null 2>&1; then jq .;
  elif command -v python3 >/dev/null 2>&1; then python3 -m json.tool;
  else cat; fi
}

# POST 并美化输出响应体，最后单独标注 HTTP 状态码
post() {
  local out code
  out=$(mktemp)
  code=$(curl -sS -o "$out" -w '%{http_code}' "$@")
  pretty < "$out"
  echo "HTTP $code"
  rm -f "$out"
}

echo "== 1) 首次上报 rev-301 =="
curl -sS -X POST "$API_A/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-301", "revision_id": "rev-301", "parents": [],
     "species": "Vulpes vulpes", "count": 1}
  ]
}' | pretty

echo
echo "== 2) 同号同内容原样重传：幂等，duplicates=[rev-301]，状态不变 =="
curl -sS -X POST "$API_B/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-301", "revision_id": "rev-301", "parents": [],
     "species": "Vulpes vulpes", "count": 1}
  ]
}' | pretty

echo
echo "== 3) 同号异内容（rev-301 数量改为 9）连同一条合法新修订一起提交 → 422 整包拒绝 =="
post -X POST "$API_A/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-301", "revision_id": "rev-301", "parents": [],
     "species": "Vulpes vulpes", "count": 9},
    {"observation_id": "OBS-301", "revision_id": "rev-302", "parents": ["rev-301"],
     "species": "Vulpes vulpes", "count": 2}
  ]
}'

echo
echo "== 4) 验证未留部分记录：rev-302 未入库，分支仍只有 rev-301 =="
curl -sS "$API_B/observations/OBS-301" | pretty

echo
echo "== 5) 循环引用（rev-401 ↔ rev-402）→ 422 整包拒绝 =="
post -X POST "$API_A/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-401", "revision_id": "rev-401", "parents": ["rev-402"],
     "species": "Aquila chrysaetos", "count": 1},
    {"observation_id": "OBS-401", "revision_id": "rev-402", "parents": ["rev-401"],
     "species": "Aquila chrysaetos", "count": 1}
  ]
}'

echo
echo "== 6) 跨观测引用（OBS-501 的修订引用 OBS-301 的 rev-301 为父）→ 422 整包拒绝 =="
post -X POST "$API_A/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-501", "revision_id": "rev-501", "parents": ["rev-301"],
     "species": "Bubo bubo", "count": 2}
  ]
}'

echo
echo "== 7) 反向跨观测：子 rev-601 先到（父 rev-600 未到、待补齐），随后父以别的观测号到达 → 422 =="
curl -sS -X POST "$API_A/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-601", "revision_id": "rev-601", "parents": ["rev-600"],
     "species": "Canis lupus", "count": 6}
  ]
}' | pretty
post -X POST "$API_A/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-602", "revision_id": "rev-600", "parents": [],
     "species": "Canis lupus", "count": 5}
  ]
}'

echo
echo "== 8) 子仍待补齐，父未入库（未留部分记录） =="
curl -sS "$API_B/observations/OBS-601" | pretty
