"""核心域逻辑：修订 DAG 的接收、校验与观测状态重算。

每个观测的修订构成一张有向无环图（允许子先父后乱序到达）：
- pending  待补齐：已知修订引用了尚未到达的祖先，不出有效值；补齐后自动重算；
- valid    有效：祖先齐全且只剩一个无后继修订（当前分支唯一），其载荷即有效值
           （删除标记也是合法结论，表示该观测被删除）；
- conflict 冲突：祖先齐全但存在多个无后继修订（多分支；改值与删除并存同属此类），
           不能输出单一有效观测，需人工裁决。

人工裁决：以裁决时所见全部分支（tips）为父新建修订；提交时分支已变则 409；
裁决之后迟到的并发分支会重新形成冲突，不会被裁决吞没。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Optional


class PackageRejected(Exception):
    """整包拒绝：事务回滚，不留任何部分记录。"""


class ObservationNotFound(Exception):
    pass


class BranchesChanged(Exception):
    """裁决提交时分支已变化（409）。"""

    def __init__(self, observation_id: str, current_tips: list[str], status: str):
        super().__init__(observation_id)
        self.observation_id = observation_id
        self.current_tips = current_tips
        self.status = status


@dataclass
class RevisionPayload:
    observation_id: str
    revision_id: str
    parents: list[str]
    species: Optional[str]
    count: Optional[int]
    delete: bool


def canonical_content(rev: RevisionPayload) -> str:
    """规范化内容指纹：同号同内容重传幂等的比对基准（父修订顺序无关）。"""
    return json.dumps(
        {
            "observation_id": rev.observation_id,
            "parents": sorted(set(rev.parents)),
            "species": rev.species,
            "count": rev.count,
            "delete": bool(rev.delete),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


# ---------------------------------------------------------------- 查询

def _row_to_state(row: sqlite3.Row) -> dict:
    effective = None
    if row["status"] == "valid" and row["eff_revision_id"] is not None:
        effective = {
            "revision_id": row["eff_revision_id"],
            "species": row["eff_species"],
            "count": row["eff_count"],
            "deleted": bool(row["eff_deleted"]),
        }
    return {
        "observation_id": row["observation_id"],
        "status": row["status"],
        "tips": json.loads(row["tips"]),
        "missing_ancestors": json.loads(row["missing"]),
        "effective": effective,
        "version": row["version"],
    }


def get_observation(conn: sqlite3.Connection, observation_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM observations WHERE observation_id = ?", (observation_id,)
    ).fetchone()
    return _row_to_state(row) if row else None


def list_observations(conn: sqlite3.Connection, status: Optional[str] = None) -> list[dict]:
    if status:
        rows = conn.execute(
            "SELECT * FROM observations WHERE status = ? ORDER BY observation_id", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM observations ORDER BY observation_id").fetchall()
    return [_row_to_state(r) for r in rows]


def list_revisions(conn: sqlite3.Connection, observation_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM revisions WHERE observation_id = ? ORDER BY received_at, revision_id",
        (observation_id,),
    ).fetchall()
    parents = conn.execute(
        "SELECT rp.revision_id, rp.parent_id FROM revision_parents rp "
        "JOIN revisions rv ON rv.revision_id = rp.revision_id "
        "WHERE rv.observation_id = ?",
        (observation_id,),
    ).fetchall()
    pmap: dict[str, list[str]] = {}
    for p in parents:
        pmap.setdefault(p["revision_id"], []).append(p["parent_id"])
    return [
        {
            "revision_id": r["revision_id"],
            "observation_id": r["observation_id"],
            "parents": sorted(pmap.get(r["revision_id"], [])),
            "species": r["species"],
            "count": r["individual_count"],
            "delete": bool(r["is_delete"]),
            "received_at": r["received_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------- 重算

def recompute_observation(conn: sqlite3.Connection, observation_id: str) -> dict:
    """按当前已到的全部修订重算观测状态（待补齐/有效/冲突）。"""
    rows = conn.execute(
        "SELECT revision_id, species, individual_count, is_delete "
        "FROM revisions WHERE observation_id = ?",
        (observation_id,),
    ).fetchall()
    ids = {r["revision_id"] for r in rows}

    # 当前分支 = 没有任何后继（子修订）的修订
    referenced: set[str] = set()
    if ids:
        marks = ",".join("?" for _ in ids)
        referenced = {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT parent_id FROM revision_parents WHERE parent_id IN ({marks})",
                sorted(ids),
            )
        }
    tips = sorted(ids - referenced)

    # 未齐祖先 = 本观测修订引用、但尚未到达的修订号
    needed: set[str] = set()
    if ids:
        needed = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT rp.parent_id FROM revision_parents rp "
                "JOIN revisions rv ON rv.revision_id = rp.revision_id "
                "WHERE rv.observation_id = ?",
                (observation_id,),
            )
        }
    missing = sorted(needed - ids)

    if missing:
        status = "pending"
        eff = (None, None, None, 0)
    elif len(tips) == 1:
        tip = next(r for r in rows if r["revision_id"] == tips[0])
        status = "valid"
        eff = (tip["revision_id"], tip["species"], tip["individual_count"], tip["is_delete"])
    else:
        status = "conflict"
        eff = (None, None, None, 0)

    tips_json = json.dumps(tips, ensure_ascii=False)
    missing_json = json.dumps(missing, ensure_ascii=False)
    prev = conn.execute(
        "SELECT * FROM observations WHERE observation_id = ?", (observation_id,)
    ).fetchone()
    if prev is None:
        conn.execute(
            "INSERT INTO observations (observation_id, status, tips, missing, "
            "eff_revision_id, eff_species, eff_count, eff_deleted, version) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            (observation_id, status, tips_json, missing_json, *eff),
        )
    else:
        current = (
            prev["status"], prev["tips"], prev["missing"], prev["eff_revision_id"],
            prev["eff_species"], prev["eff_count"], prev["eff_deleted"],
        )
        if current != (status, tips_json, missing_json, *eff):
            conn.execute(
                "UPDATE observations SET status=?, tips=?, missing=?, eff_revision_id=?, "
                "eff_species=?, eff_count=?, eff_deleted=?, version=version+1 "
                "WHERE observation_id=?",
                (status, tips_json, missing_json, *eff, observation_id),
            )
    return get_observation(conn, observation_id)  # type: ignore[return-value]


# ---------------------------------------------------------------- 汇交

def submit_package(conn: sqlite3.Connection, revisions: list[RevisionPayload]) -> dict:
    """接收一个 JSON 包：整体校验、整体入库、整体重算；任何一条非法则整包拒绝。"""
    # 1) 包内去重：同号同内容幂等，同号异内容整包拒绝
    unique: dict[str, tuple[str, RevisionPayload]] = {}
    for rev in revisions:
        content = canonical_content(rev)
        prev = unique.get(rev.revision_id)
        if prev is not None and prev[0] != content:
            raise PackageRejected(f"修订 {rev.revision_id} 在包内同号异内容")
        unique.setdefault(rev.revision_id, (content, rev))

    # 2) 与库中已有修订比对：同号同内容→幂等跳过；同号异内容→整包拒绝
    existing: dict[str, sqlite3.Row] = {}
    if unique:
        marks = ",".join("?" for _ in unique)
        existing = {
            r["revision_id"]: r
            for r in conn.execute(
                f"SELECT revision_id, observation_id, content FROM revisions "
                f"WHERE revision_id IN ({marks})",
                list(unique),
            )
        }
    duplicates: list[str] = []
    fresh: dict[str, tuple[str, RevisionPayload]] = {}
    for rid, (content, rev) in unique.items():
        hit = existing.get(rid)
        if hit is None:
            fresh[rid] = (content, rev)
        elif hit["content"] != content:
            raise PackageRejected(f"修订 {rid} 与库中已有修订同号异内容")
        else:
            duplicates.append(rid)

    states: list[dict] = []
    if fresh:
        _validate_new_revisions(conn, fresh, existing)
        # 3) 全部校验通过后整体入库
        for rid, (content, rev) in fresh.items():
            conn.execute(
                "INSERT INTO revisions (revision_id, observation_id, species, "
                "individual_count, is_delete, content) VALUES (?,?,?,?,?,?)",
                (rid, rev.observation_id, rev.species, rev.count,
                 1 if rev.delete else 0, content),
            )
            for parent in sorted(set(rev.parents)):
                conn.execute(
                    "INSERT INTO revision_parents (revision_id, parent_id) VALUES (?,?)",
                    (rid, parent),
                )
        # 4) 受影响观测自动重算（补齐的祖先会解除待补齐，新分支会形成冲突等）
        for obs_id in sorted({rev.observation_id for _, rev in fresh.values()}):
            states.append(recompute_observation(conn, obs_id))

    return {
        "accepted": sorted(fresh),
        "duplicates": sorted(duplicates),
        "observations": states,
    }


def _validate_new_revisions(
    conn: sqlite3.Connection,
    fresh: dict[str, tuple[str, RevisionPayload]],
    existing: dict[str, sqlite3.Row],
) -> None:
    # 取出新修订引用、且已在库中的父修订（不在包内的那些）
    ref_parents: set[str] = set()
    for _, rev in fresh.values():
        ref_parents.update(rev.parents)
    ref_parents -= set(fresh)
    ref_parents -= set(existing)
    parent_rows: dict[str, sqlite3.Row] = {}
    if ref_parents:
        marks = ",".join("?" for _ in ref_parents)
        parent_rows = {
            r["revision_id"]: r
            for r in conn.execute(
                f"SELECT revision_id, observation_id FROM revisions "
                f"WHERE revision_id IN ({marks})",
                sorted(ref_parents),
            )
        }

    # 正向跨观测引用：父修订（无论在包内还是库中）必须属于同一观测
    for rid, (_, rev) in fresh.items():
        for parent in set(rev.parents):
            if parent == rid:
                raise PackageRejected(f"修订 {rid} 以自身为父，构成循环")
            if parent in fresh:
                if fresh[parent][1].observation_id != rev.observation_id:
                    raise PackageRejected(f"修订 {rid} 跨观测引用父修订 {parent}")
            elif parent in existing:
                if existing[parent]["observation_id"] != rev.observation_id:
                    raise PackageRejected(f"修订 {rid} 跨观测引用父修订 {parent}")
            elif parent in parent_rows:
                if parent_rows[parent]["observation_id"] != rev.observation_id:
                    raise PackageRejected(f"修订 {rid} 跨观测引用父修订 {parent}")
            # 父未到达：允许（子先父后），观测标待补齐

    # 循环检测：库中 DAG 本无环，只需检查包内新修订之间的边
    indegree = {rid: 0 for rid in fresh}
    children: dict[str, list[str]] = {rid: [] for rid in fresh}
    for rid, (_, rev) in fresh.items():
        for parent in set(rev.parents):
            if parent in fresh:
                indegree[rid] += 1
                children[parent].append(rid)
    stack = [rid for rid, deg in indegree.items() if deg == 0]
    visited = 0
    while stack:
        node = stack.pop()
        visited += 1
        for child in children[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                stack.append(child)
    if visited != len(fresh):
        raise PackageRejected("修订之间存在循环引用")

    # 反向跨观测引用：新修订是库中既有子修订（先到的子）的父
    for rid, (_, rev) in fresh.items():
        rows = conn.execute(
            "SELECT rv.revision_id, rv.observation_id FROM revision_parents rp "
            "JOIN revisions rv ON rv.revision_id = rp.revision_id WHERE rp.parent_id = ?",
            (rid,),
        ).fetchall()
        for row in rows:
            if row["observation_id"] != rev.observation_id:
                raise PackageRejected(
                    f"修订 {rid} 与既有子修订 {row['revision_id']} 跨观测引用"
                )


# ---------------------------------------------------------------- 裁决

def resolve_observation(
    conn: sqlite3.Connection, observation_id: str, rev: RevisionPayload
) -> tuple[dict, bool]:
    """人工裁决：以所见全部分支为父新建修订。返回 (观测状态, 是否幂等重放)。"""
    content = canonical_content(rev)
    hit = conn.execute(
        "SELECT content FROM revisions WHERE revision_id = ?", (rev.revision_id,)
    ).fetchone()
    if hit is not None:  # 同号重传：同内容幂等返回，异内容拒绝
        if hit["content"] != content:
            raise PackageRejected(f"修订 {rev.revision_id} 与库中已有修订同号异内容")
        state = get_observation(conn, observation_id)
        if state is None:
            raise ObservationNotFound(observation_id)
        return state, True

    row = conn.execute(
        "SELECT * FROM observations WHERE observation_id = ?", (observation_id,)
    ).fetchone()
    if row is None:
        raise ObservationNotFound(observation_id)
    current_tips = json.loads(row["tips"])
    if sorted(set(rev.parents)) != current_tips:
        raise BranchesChanged(observation_id, current_tips, row["status"])

    conn.execute(
        "INSERT INTO revisions (revision_id, observation_id, species, individual_count, "
        "is_delete, content) VALUES (?,?,?,?,?,?)",
        (rev.revision_id, observation_id, rev.species, rev.count,
         1 if rev.delete else 0, content),
    )
    for parent in sorted(set(rev.parents)):
        conn.execute(
            "INSERT INTO revision_parents (revision_id, parent_id) VALUES (?,?)",
            (rev.revision_id, parent),
        )
    return recompute_observation(conn, observation_id), False
