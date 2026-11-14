#!/usr/bin/env bash
# 样例 2：改值与删除并发 → 冲突（不能输出单一有效观测）→ 人工裁决 →
#         迟到的并发分支重新形成冲突 → 用过期分支裁决返回 409。
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

echo "== 1) 基底修订 rev-101（两台设备共同的起点） =="
curl -sS -X POST "$API_A/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-101", "revision_id": "rev-101", "parents": [],
     "species": "Cervus elaphus", "count": 3}
  ]
}' | pretty

echo
echo "== 2) 设备甲改数量为 5（rev-102a），设备乙标记删除（rev-102b），并发到达 =="
curl -sS -X POST "$API_A/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-101", "revision_id": "rev-102a", "parents": ["rev-101"],
     "species": "Cervus elaphus", "count": 5}
  ]
}' | pretty
curl -sS -X POST "$API_B/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-101", "revision_id": "rev-102b", "parents": ["rev-101"],
     "delete": true}
  ]
}' | pretty

echo
echo "== 3) 从 api-b 查冲突：改值与删除并存，无单一有效观测 =="
curl -sS "$API_B/conflicts" | pretty

echo
echo "== 4) 人工裁决：以所见全部分支 [rev-102a, rev-102b] 为父，保留数量 5 =="
curl -sS -X POST "$API_A/observations/OBS-101/resolutions" -H 'Content-Type: application/json' -d '{
  "revision_id": "rev-103", "parents": ["rev-102a", "rev-102b"],
  "species": "Cervus elaphus", "count": 5
}' | pretty

echo
echo "== 5) 裁决后从 api-b 查询：恢复 valid，有效值即裁决值 =="
curl -sS "$API_B/observations/OBS-101" | pretty

echo
echo "== 6) 迟到的并发分支 rev-102c（第三台设备，仍基于 rev-101）到达：冲突重新出现 =="
curl -sS -X POST "$API_B/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-101", "revision_id": "rev-102c", "parents": ["rev-101"],
     "species": "Cervus elaphus", "count": 4}
  ]
}' | pretty

echo
echo "== 7) 用过期分支集合 [rev-102a, rev-102b] 提交裁决 → 409，并返回当前分支 =="
post -X POST "$API_A/observations/OBS-101/resolutions" \
  -H 'Content-Type: application/json' -d '{
  "revision_id": "rev-104", "parents": ["rev-102a", "rev-102b"],
  "species": "Cervus elaphus", "count": 5
}'

echo
echo "== 8) 按当前分支 [rev-102c, rev-103] 重新裁决 → 成功 =="
post -X POST "$API_A/observations/OBS-101/resolutions" \
  -H 'Content-Type: application/json' -d '{
  "revision_id": "rev-105", "parents": ["rev-102c", "rev-103"],
  "species": "Cervus elaphus", "count": 4
}'
