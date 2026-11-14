#!/usr/bin/env bash
# 样例 4：物种占比判读。
#   确定超标 / 确定未超 / 待裁决（给出见证分支）/ 不给判定（列原因）。
set -euo pipefail

API_A="${API_A:-http://localhost:8001}"
API_B="${API_B:-http://localhost:8002}"

pretty() {
  if command -v jq >/dev/null 2>&1; then jq .;
  elif command -v python3 >/dev/null 2>&1; then python3 -m json.tool;
  else cat; fi
}

assess() {
  curl -sS -X POST "$1/assessments" -H 'Content-Type: application/json' -d "$2" | pretty
}

echo "== 1) 准备两个观测：OBS-A 目标物种 Cervus elaphus=8；OBS-B 其他物种=2 =="
curl -sS -X POST "$API_A/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-A", "revision_id": "ra-1", "parents": [],
     "species": "Cervus elaphus", "count": 8},
    {"observation_id": "OBS-B", "revision_id": "rb-1", "parents": [],
     "species": "Sus scrofa", "count": 2}
  ]
}' | pretty

echo
echo "== 2) 确定超标：8/10 = 80% > 50% =="
assess "$API_A" '{"observation_ids": ["OBS-A", "OBS-B"],
  "species": "Cervus elaphus", "threshold_percent": 50}'

echo
echo "== 3) 给定观测制造冲突：设备甲改为 1 只，设备乙改为 8 只 =="
curl -sS -X POST "$API_A/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-A", "revision_id": "ra-2", "parents": ["ra-1"],
     "species": "Cervus elaphus", "count": 1}
  ]
}' >/dev/null
curl -sS -X POST "$API_B/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-A", "revision_id": "ra-3", "parents": ["ra-1"],
     "species": "Cervus elaphus", "count": 8}
  ]
}' >/dev/null

echo "== 4) 待裁决：选 ra-2 时 1/3 未超；选 ra-3 时 8/10 超标 =="
echo "   注意：ra-2 与 ra-3 是同一观测的两条分支，只能二选一；"
echo "   物种数与总数由同一组选择决定。"
assess "$API_B" '{"observation_ids": ["OBS-A", "OBS-B"],
  "species": "Cervus elaphus", "threshold_percent": 50}'

echo
echo "== 5) 删除分支两项均计零，其他物种只计总数 =="
echo "   OBS-D 固定为其他物种 6 只；OBS-E 在“目标物种 5 只”与“删除”之间冲突。"
echo "   选改值：5/(5+6)=45% 未超；选删除：0/6=0% 未超 → 所有选择都未超。"
curl -sS -X POST "$API_A/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-D", "revision_id": "rd-1", "parents": [],
     "species": "Sus scrofa", "count": 6},
    {"observation_id": "OBS-E", "revision_id": "re-1", "parents": [],
     "species": "Cervus elaphus", "count": 5},
    {"observation_id": "OBS-E", "revision_id": "re-2", "parents": ["re-1"],
     "delete": true}
  ]
}' >/dev/null
assess "$API_A" '{"observation_ids": ["OBS-D", "OBS-E"],
  "species": "Cervus elaphus", "threshold_percent": 50}'

echo
echo "== 6) 待补齐观测不给判定：OBS-C 的父修订 rc-0 尚未到达 =="
curl -sS -X POST "$API_B/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-C", "revision_id": "rc-1", "parents": ["rc-0"],
     "species": "Cervus elaphus", "count": 4}
  ]
}' >/dev/null
assess "$API_A" '{"observation_ids": ["OBS-A", "OBS-C"],
  "species": "Cervus elaphus", "threshold_percent": 50}'

echo
echo "== 7) 观测不存在也一并列出原因 =="
assess "$API_B" '{"observation_ids": ["OBS-A", "OBS-MISSING"],
  "species": "Cervus elaphus", "threshold_percent": 50}'

echo
echo "== 8) 某种选择使整批总数为零 → 不给判定 =="
echo "   OBS-F 在“其他物种 3 只”与“删除”之间冲突；若选删除，整批总数为零。"
curl -sS -X POST "$API_B/packages" -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-F", "revision_id": "rf-1", "parents": [],
     "species": "Sus scrofa", "count": 3},
    {"observation_id": "OBS-F", "revision_id": "rf-2", "parents": ["rf-1"],
     "delete": true}
  ]
}' >/dev/null
assess "$API_A" '{"observation_ids": ["OBS-F"],
  "species": "Cervus elaphus", "threshold_percent": 50}'
