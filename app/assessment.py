"""物种占比判读：在“每条冲突观测只取一条当前分支”的全部可行选择上求结论。

语义
----
- 每个观测在其当前分支（tips）中恰好选一条；`valid` 观测只有一条分支，实际被强制；
  `conflict` 观测可在各分支间任选。
- 一次选择同时决定“目标物种个体数”与“整批总数”，二者必然来自同一组分支选择。
- 删除分支两项均计 0；非目标物种只计入整批总数。
- 占比 = 目标物种个体数 * 100 / 整批总数，超过阈值（严格大于）才算超标。

算法
----
每条分支对判定的贡献可写成线性打分：

    score = 100 * 目标物种数 - 阈值 * 整批总数

整批得分是各观测所选分支得分的和，因此所有可行选择的极值只需逐观测取
max/min 再求和：max > 0 说明存在超标选择，min > 0 说明所有选择都超标。
这与“每组选择同时取物种数与总数”完全等价，且能直接构造见证分支组。

阻断条件（不给判定，只列原因）
------------------------------
- 输入观测不存在；
- 观测待补齐（祖先未齐，当前没有唯一可用的分支集合）；
- 存在某种分支选择使整批总数为零（占比无定义）。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Optional


VERDICT_EXCEED = "确定超标"
VERDICT_NOT_EXCEED = "确定未超"
VERDICT_UNDECIDED = "待裁决"


@dataclass(frozen=True)
class Candidate:
    """一条可能是当前分支的修订，及其对判定的固定贡献。"""

    observation_id: str
    revision_id: str
    species: Optional[str]
    count: Optional[int]
    deleted: bool
    species_contrib: int   # 计入“目标物种个体数”
    total_contrib: int     # 计入“整批总数”
    score: int              # 100 * species_contrib - threshold * total_contrib


def _candidate(tip: sqlite3.Row, observation_id: str, species: str,
               threshold: int) -> Candidate:
    deleted = bool(tip["is_delete"])
    count = tip["individual_count"]
    # 删除分支两项均计零；非目标物种只计总数
    total = 0 if deleted or count is None else int(count)
    target = total if (not deleted and tip["species"] == species) else 0
    return Candidate(
        observation_id=observation_id,
        revision_id=tip["revision_id"],
        species=tip["species"],
        count=tip["individual_count"],
        deleted=deleted,
        species_contrib=target,
        total_contrib=total,
        score=100 * target - threshold * total,
    )


def _best(candidates: list[Candidate], maximize: bool) -> Candidate:
    """确定性地取极值分支：同分时取修订号较小者，便于复算见证。"""
    ordered = sorted(candidates, key=lambda c: c.revision_id)
    best = ordered[0]
    for cand in ordered[1:]:
        if (cand.score > best.score) if maximize else (cand.score < best.score):
            best = cand
    return best


def _reason(code: str, message: str, observation_id: Optional[str] = None) -> dict:
    return {"code": code, "observation_id": observation_id, "message": message}


def _chunks(items: list[str], size: int = 500):
    """分批查询：单个观测可有任意多条分支，避免超出 SQL 变量数上限。"""
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _blocked(reasons: list[dict], species: str, threshold: int,
             observation_ids: list[str], states: list[dict]) -> dict:
    return {
        "verdict": None,
        "code": "blocked",
        "species": species,
        "threshold": threshold,
        "threshold_percent": threshold,
        "observation_ids": observation_ids,
        "reasons": reasons,
        "observations": states,
        "witnesses": None,
    }


def _group(candidates: list[Candidate], species: str, threshold: int) -> dict:
    species_count = sum(c.species_contrib for c in candidates)
    total_count = sum(c.total_contrib for c in candidates)
    return {
        "revision_ids": [c.revision_id for c in candidates],
        "selections": [
            {
                "observation_id": c.observation_id,
                "revision_id": c.revision_id,
                "species": c.species,
                "count": c.count,
                "deleted": c.deleted,
                "species_count": c.species_contrib,
                "total_count": c.total_contrib,
            }
            for c in candidates
        ],
        "species_count": species_count,
        "total_count": total_count,
        "ratio_percent": (
            species_count * 100.0 / total_count if total_count else None
        ),
        "exceeds": species_count * 100 > threshold * total_count,
    }


def assess(
    conn: sqlite3.Connection,
    observation_ids: list[str],
    species: str,
    threshold: int,
) -> dict:
    """在一个只读快照里判读（调用方须用 snapshot_tx 保证同一已提交状态）。"""
    states: list[dict] = []
    reasons: list[dict] = []

    by_id: dict[str, sqlite3.Row] = {}
    for chunk in _chunks(observation_ids):
        rows = conn.execute(
            "SELECT * FROM observations WHERE observation_id IN ({})".format(
                ",".join("?" for _ in chunk)
            ),
            chunk,
        ).fetchall()
        by_id.update({row["observation_id"]: row for row in rows})

    # 1) 观测不存在 / 待补齐：列出原因，等待补齐后重新判读
    for obs_id in observation_ids:
        row = by_id.get(obs_id)
        if row is None:
            reasons.append(_reason(
                "observation_not_found", f"观测 {obs_id} 不存在", obs_id))
            continue
        missing = json.loads(row["missing"])
        states.append({
            "observation_id": obs_id,
            "status": row["status"],
            "tips": json.loads(row["tips"]),
            "missing_ancestors": missing,
            "version": row["version"],
        })
        if row["status"] == "pending":
            reasons.append(_reason(
                "observation_pending",
                f"观测 {obs_id} 待补齐，缺少祖先修订 {missing}",
                obs_id,
            ))

    if reasons:
        return _blocked(reasons, species, threshold, observation_ids, states)

    # 2) 取当前分支载荷：与上方观测状态来自同一个快照事务
    tip_ids = sorted({tip for st in states for tip in st["tips"]})
    tips_by_id: dict[str, sqlite3.Row] = {}
    for chunk in _chunks(tip_ids):
        for row in conn.execute(
            "SELECT revision_id, species, individual_count, is_delete "
            "FROM revisions WHERE revision_id IN ({})".format(
                ",".join("?" for _ in chunk)
            ),
            chunk,
        ).fetchall():
            tips_by_id[row["revision_id"]] = row

    per_obs: dict[str, list[Candidate]] = {}
    for st in states:
        cands = []
        for tip in st["tips"]:
            row = tips_by_id.get(tip)
            if row is None:  # 库内状态本应自洽；防御性处理，仍不给判定
                reasons.append(_reason(
                    "branch_unavailable",
                    f"观测 {st['observation_id']} 的分支修订 {tip} 不可用",
                    st["observation_id"],
                ))
                continue
            cands.append(_candidate(row, st["observation_id"], species, threshold))
        per_obs[st["observation_id"]] = cands

    if reasons:
        return _blocked(reasons, species, threshold, observation_ids, states)

    # 3) 某种选择使整批总数为零 ⇒ 占比无定义，不判定。
    #    各观测贡献非负且可独立选取，故整批可达的最小总数 = 各观测最小贡献之和。
    min_total = sum(
        min(c.total_contrib for c in per_obs[obs]) for obs in observation_ids
    )
    if min_total == 0:
        zero_picks = [
            min(per_obs[obs], key=lambda c: (c.total_contrib, c.revision_id))
            for obs in observation_ids
        ]
        reasons.append(_reason(
            "zero_batch_total",
            "存在一种分支选择使整批总数为零，占比无定义"
            f"（该选择为 {[c.revision_id for c in zero_picks]}）",
        ))
        return _blocked(reasons, species, threshold, observation_ids, states)

    # 4) 逐观测取打分极值；整批极值 = 各观测极值之和
    max_picks = [_best(per_obs[obs], maximize=True) for obs in observation_ids]
    min_picks = [_best(per_obs[obs], maximize=False) for obs in observation_ids]
    max_score = sum(c.score for c in max_picks)
    min_score = sum(c.score for c in min_picks)
    exceed_witness = _group(max_picks, species, threshold)
    not_exceed_witness = _group(min_picks, species, threshold)

    if min_score > 0:  # 所有可行选择都超标
        verdict, code, witnesses = VERDICT_EXCEED, "exceeds", None
    elif max_score <= 0:  # 所有可行选择都未超标（等号不算超标）
        verdict, code, witnesses = VERDICT_NOT_EXCEED, "not_exceeds", None
    else:  # 有的选择超标、有的未超标
        verdict, code = VERDICT_UNDECIDED, "undecided"
        witnesses = {
            "exceeding": exceed_witness,
            "not_exceeding": not_exceed_witness,
            "exceeding_revision_ids": exceed_witness["revision_ids"],
            "not_exceeding_revision_ids": not_exceed_witness["revision_ids"],
        }

    return {
        "verdict": verdict,
        "code": code,
        "species": species,
        "threshold": threshold,
        "threshold_percent": threshold,
        "observation_ids": observation_ids,
        "reasons": [],
        "observations": states,
        "witnesses": witnesses,
    }
