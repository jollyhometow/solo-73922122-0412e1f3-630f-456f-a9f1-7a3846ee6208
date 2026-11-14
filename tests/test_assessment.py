"""判读 API 行为测试：可行选择穷举、见证分支、阻断原因、一致性快照。"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def rev(obs, rid, parents, species=None, count=None, delete=False):
    body = {"observation_id": obs, "revision_id": rid, "parents": parents,
            "delete": delete}
    if species is not None:
        body["species"] = species
    if count is not None:
        body["count"] = count
    return body


def pkg(*revs):
    return {"revisions": list(revs)}


def assess(client, ids, species="A", threshold=50):
    r = client.post("/assessments", json={
        "observation_ids": ids, "species": species, "threshold_percent": threshold})
    assert r.status_code == 200, r.text
    return r.json()


def setup_branch(client, obs, base_count, left, right):
    """基底 + 两条并发分支，形成 conflict：left/right 为 (species, count) 或 delete。"""
    client.post("/packages", json=pkg(
        rev(obs, f"{obs}-r0", [], species="A", count=base_count)))
    for suffix, value in (("a", left), ("b", right)):
        if value == "delete":
            client.post("/packages", json=pkg(
                rev(obs, f"{obs}-r{suffix}", [f"{obs}-r0"], delete=True)))
        else:
            sp, cnt = value
            client.post("/packages", json=pkg(
                rev(obs, f"{obs}-r{suffix}", [f"{obs}-r0"], species=sp, count=cnt)))


# ------------------------------------------------------------ 确定结论

def test_valid_single_branch_exceeds(client):
    client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=6)))
    client.post("/packages", json=pkg(rev("O2", "r2", [], species="B", count=4)))
    out = assess(client, ["O1", "O2"], species="A", threshold=50)
    assert out["code"] == "exceeds"
    assert out["verdict"] == "确定超标"
    assert out["reasons"] == []
    assert out["witnesses"] is None
    # 6/10 = 60% > 50%
    assert out["observations"][0]["status"] == "valid"


def test_equal_threshold_is_not_exceed(client):
    client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=5)))
    client.post("/packages", json=pkg(rev("O2", "r2", [], species="B", count=5)))
    out = assess(client, ["O1", "O2"], species="A", threshold=50)
    assert out["code"] == "not_exceeds"
    assert out["verdict"] == "确定未超"


def test_zero_threshold_any_target_exceeds(client):
    client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=1)))
    assert assess(client, ["O1"], species="A", threshold=0)["code"] == "exceeds"
    # 目标物种完全缺席：0% 未超过 0%？
    out = assess(client, ["O1"], species="Z", threshold=0)
    assert out["code"] == "not_exceeds"


# ------------------------------------------------------------ 分支穷举

def test_all_selections_exceed_single_conflict(client):
    # 分支为 6 或 8，另一观测固定 B=2 → 6/8、8/10 均 > 50%
    setup_branch(client, "O1", 0, ("A", 6), ("A", 8))
    client.post("/packages", json=pkg(rev("O2", "r2", [], species="B", count=2)))
    out = assess(client, ["O1", "O2"], species="A", threshold=50)
    assert out["code"] == "exceeds"
    assert out["witnesses"] is None
    assert out["observations"][0]["status"] == "conflict"


def test_all_selections_not_exceed(client):
    # 分支为 1 或 2，另一观测固定 B=8 → 最多 2/10 = 20%
    setup_branch(client, "O1", 0, ("A", 1), ("A", 2))
    client.post("/packages", json=pkg(rev("O2", "r2", [], species="B", count=8)))
    out = assess(client, ["O1", "O2"], species="A", threshold=50)
    assert out["code"] == "not_exceeds"


def test_undecided_with_witnesses(client):
    # O1 分支 8 或 1；O2 固定 B=2 → 8/10 超标、1/3 未超
    setup_branch(client, "O1", 0, ("A", 8), ("A", 1))
    client.post("/packages", json=pkg(rev("O2", "r2", [], species="B", count=2)))
    out = assess(client, ["O1", "O2"], species="A", threshold=50)
    assert out["code"] == "undecided"
    assert out["verdict"] == "待裁决"
    w = out["witnesses"]
    assert w["exceeding"]["revision_ids"] == ["O1-ra", "r2"]
    assert w["exceeding"]["species_count"] == 8
    assert w["exceeding"]["total_count"] == 10
    assert w["exceeding"]["exceeds"] is True
    assert w["not_exceeding"]["revision_ids"] == ["O1-rb", "r2"]
    assert w["not_exceeding"]["total_count"] == 3
    assert w["not_exceeding"]["exceeds"] is False
    # 两个见证都真实存在且是当前分支
    assert {s["observation_id"] for s in w["exceeding"]["selections"]} == {"O1", "O2"}


def test_non_target_species_only_counts_total(client):
    # O1 分支：A=7（目标，计两项）或 B=7（非目标，只计总数）
    setup_branch(client, "O1", 0, ("A", 7), ("B", 7))
    out = assess(client, ["O1"], species="A", threshold=50)
    # A 分支 7/7 超标；B 分支 0/7 未超 → 待裁决
    assert out["code"] == "undecided"
    w = out["witnesses"]
    assert w["exceeding"]["selections"][0]["species_count"] == 7
    assert w["not_exceeding"]["selections"][0]["species_count"] == 0
    assert w["not_exceeding"]["selections"][0]["total_count"] == 7


def test_delete_branch_counts_zero_for_both(client):
    # A 分支 6/10=60% 超标；删除分支 0/4=0% 未超
    setup_branch(client, "O1", 0, ("A", 6), "delete")
    client.post("/packages", json=pkg(rev("O2", "r2", [], species="B", count=4)))
    out = assess(client, ["O1", "O2"], species="A", threshold=50)
    w = out["witnesses"]
    assert out["code"] == "undecided"
    assert w["exceeding"]["selections"][0]["revision_id"] == "O1-ra"
    # 删除分支：该观测两项均为 0，总数只剩 O2 的 4
    assert w["not_exceeding"]["selections"][0]["deleted"] is True
    assert w["not_exceeding"]["selections"][0]["total_count"] == 0
    assert w["not_exceeding"]["selections"][0]["species_count"] == 0
    assert w["not_exceeding"]["total_count"] == 4
    assert w["not_exceeding"]["ratio_percent"] == 0.0


def test_delete_everywhere_makes_total_zero(client):
    setup_branch(client, "O1", 0, ("A", 5), "delete")
    setup_branch(client, "O2", 0, ("B", 5), "delete")
    out = assess(client, ["O1", "O2"], species="A", threshold=50)
    assert out["code"] == "blocked"
    assert out["verdict"] is None
    codes = [r["code"] for r in out["reasons"]]
    assert "zero_batch_total" in codes


def test_selection_combination_zero_not_individual(client):
    # 单独看每个观测都能非零，但“全选删除”这一组合使总数为零 → 不判定
    client.post("/packages", json=pkg(
        rev("O1", "o1-r1", [], species="A", count=3),
        rev("O1", "o1-r2", ["o1-r1"], delete=True),
    ))
    setup_branch(client, "O2", 0, ("B", 4), "delete")
    out = assess(client, ["O1", "O2"], species="A", threshold=50)
    assert out["code"] == "blocked"
    assert [r["code"] for r in out["reasons"]] == ["zero_batch_total"]


# ------------------------------------------------------------ 阻断条件

def test_missing_observation_reported(client):
    client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=9)))
    out = assess(client, ["O1", "NOPE"], species="A", threshold=50)
    assert out["code"] == "blocked"
    assert out["verdict"] is None
    assert out["reasons"] == [{
        "code": "observation_not_found",
        "observation_id": "NOPE",
        "message": "观测 NOPE 不存在",
    }]
    # 已存在观测的状态仍然返回，便于核对
    assert [o["observation_id"] for o in out["observations"]] == ["O1"]


def test_pending_observation_blocks_with_reason(client):
    client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=9)))
    client.post("/packages", json=pkg(rev("O2", "r2", ["o2-r0"], species="A", count=1)))
    out = assess(client, ["O1", "O2"], species="A", threshold=50)
    assert out["code"] == "blocked"
    codes = {r["code"]: r for r in out["reasons"]}
    assert codes["observation_pending"]["observation_id"] == "O2"
    assert "o2-r0" in codes["observation_pending"]["message"]
    # 补齐祖先（同观测号）后恢复判读
    client.post("/packages", json=pkg(
        rev("O2", "o2-r0", [], species="A", count=1)))
    out = assess(client, ["O1", "O2"], species="A", threshold=50)
    assert out["code"] == "exceeds"


def test_multiple_reasons_all_listed(client):
    client.post("/packages", json=pkg(rev("O2", "r2", ["r1"], species="A", count=1)))
    out = assess(client, ["O1", "O2", "O3"], species="A", threshold=50)
    assert out["code"] == "blocked"
    listed = {(r["code"], r["observation_id"]) for r in out["reasons"]}
    assert ("observation_not_found", "O1") in listed
    assert ("observation_pending", "O2") in listed
    assert ("observation_not_found", "O3") in listed


# ------------------------------------------------------------ 请求校验

def test_request_validation(client):
    post = lambda body: client.post("/assessments", json=body)  # noqa: E731
    assert post({"observation_ids": [], "species": "A",
                 "threshold_percent": 50}).status_code == 422
    assert post({"observation_ids": ["O1", "O1"], "species": "A",
                 "threshold_percent": 50}).status_code == 422
    assert post({"observation_ids": [f"O{i}" for i in range(51)], "species": "A",
                 "threshold_percent": 50}).status_code == 422
    assert post({"observation_ids": ["O1"], "species": "A",
                 "threshold_percent": 101}).status_code == 422
    assert post({"observation_ids": ["O1"], "species": "A",
                 "threshold_percent": -1}).status_code == 422
    assert post({"observation_ids": ["O1"], "species": "A",
                 "threshold_percent": 50.5}).status_code == 422
    assert post({"observation_ids": ["O1"], "species": "",
                 "threshold_percent": 50}).status_code == 422


def test_fifty_observations_accepted(client):
    revs = [rev(f"O{i}", f"r{i}", [], species="A", count=1) for i in range(50)]
    client.post("/packages", json=pkg(*revs))
    out = assess(client, [f"O{i}" for i in range(50)], species="A", threshold=50)
    assert out["code"] == "exceeds"
    assert len(out["observations"]) == 50


def test_duplicate_ids_rejected_even_when_only_50_unique(client):
    # 51 项但只有 50 个不重复：仍超出上限
    post = client.post("/assessments", json={
        "observation_ids": [f"O{i}" for i in range(50)] + ["O0"],
        "species": "A", "threshold_percent": 50})
    assert post.status_code == 422


def test_many_conflicting_observations_scale_linearly(client):
    """20 个各含两条分支的观测：组合 2^20，判读仍应一次给出正确极值。"""
    revs = []
    for i in range(20):
        revs.append(rev(f"C{i}", f"t{i}", [], species="A", count=5))
        revs.append(rev(f"C{i}", f"o{i}", [], species="B", count=5))
    client.post("/packages", json=pkg(*revs))
    out = assess(client, [f"C{i}" for i in range(20)], species="A", threshold=50)
    assert out["code"] == "undecided"
    # 全目标物种：100/100 = 100%；全其他物种：0/100 = 0%
    assert out["witnesses"]["exceeding"]["species_count"] == 100
    assert out["witnesses"]["exceeding"]["total_count"] == 100
    assert out["witnesses"]["not_exceeding"]["species_count"] == 0
    assert out["witnesses"]["not_exceeding"]["total_count"] == 100
    # 两条见证都只含各自 20 个当前分支
    assert len(out["witnesses"]["exceeding"]["revision_ids"]) == 20
    assert all(rid.startswith("t") for rid in
               out["witnesses"]["exceeding"]["revision_ids"])
    assert all(rid.startswith("o") for rid in
               out["witnesses"]["not_exceeding"]["revision_ids"])


# ------------------------------------------------------------ 一致性与重算

def test_resolution_changes_verdict(client):
    setup_branch(client, "O1", 0, ("A", 9), "delete")
    client.post("/packages", json=pkg(rev("O2", "r2", [], species="B", count=1)))
    assert assess(client, ["O1", "O2"], species="A", threshold=50)["code"] == "undecided"
    client.post("/observations/O1/resolutions", json={
        "revision_id": "O1-fix", "parents": ["O1-ra", "O1-rb"],
        "species": "A", "count": 9})
    out = assess(client, ["O1", "O2"], species="A", threshold=50)
    assert out["code"] == "exceeds"
    assert out["observations"][0]["status"] == "valid"


def test_late_branch_reopens_undecided(client):
    setup_branch(client, "O1", 0, ("A", 9), ("A", 1))
    client.post("/observations/O1/resolutions", json={
        "revision_id": "O1-fix", "parents": ["O1-ra", "O1-rb"],
        "species": "A", "count": 9})
    assert assess(client, ["O1"], species="A", threshold=50)["code"] == "exceeds"
    # 迟到的并发分支重新形成冲突，判读必须看见
    client.post("/packages", json=pkg(
        rev("O1", "O1-late", ["O1-r0"], species="A", count=1)))
    # 分支 9 与裁决值 9 都超标，迟到分支只影响分支数而不影响结论
    assert assess(client, ["O1"], species="A", threshold=50)["code"] == "exceeds"
    client.post("/packages", json=pkg(
        rev("O1", "O1-late2", ["O1-r0"], species="B", count=1)))
    out = assess(client, ["O1"], species="A", threshold=50)
    assert out["observations"][0]["status"] == "conflict"
    # 未超标分支可被选中：0/1 = 0%
    assert out["code"] == "undecided"


def test_verdict_and_witnesses_are_internally_consistent(client):
    """非待裁决时，见证阈值与判定一致；待裁决时两组见证一超一未超。"""
    setup_branch(client, "O1", 0, ("A", 8), ("A", 1))
    client.post("/packages", json=pkg(rev("O2", "r2", [], species="B", count=2)))
    out = assess(client, ["O1", "O2"], species="A", threshold=50)
    for key in ("exceeding", "not_exceeding"):
        group = out["witnesses"][key]
        assert group["exceeds"] == (
            group["species_count"] * 100 > 50 * group["total_count"])
        assert group["total_count"] > 0
        for sel in group["selections"]:  # 每观测恰选一条当前分支
            st = next(o for o in out["observations"]
                      if o["observation_id"] == sel["observation_id"])
            assert sel["revision_id"] in st["tips"]
    assert out["witnesses"]["exceeding_revision_ids"] == \
        out["witnesses"]["exceeding"]["revision_ids"]
    assert out["witnesses"]["not_exceeding_revision_ids"] == \
        out["witnesses"]["not_exceeding"]["revision_ids"]


# ------------------------------------------------------------ 快照一致性

def test_snapshot_reads_one_committed_state(client, monkeypatch):
    """判读事务内先读观测、后读分支；期间另一端提交写入不得被看见。"""
    from app import assessment
    from app.db import snapshot_tx

    client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=1)))
    with snapshot_tx() as conn:
        first = assessment.assess(conn, ["O1"], "A", 50)  # 固定快照
        assert first["observations"][0]["tips"] == ["r1"]
        # 另一连接（模拟另一进程）提交新修订：O1 变成冲突
        client.post("/packages", json=pkg(rev("O1", "r2", ["r1"], delete=True)))
        # 同一快照内重复判读仍看到旧状态，判定与分支同源
        second = assessment.assess(conn, ["O1"], "A", 50)
        assert second["observations"][0]["tips"] == ["r1"]
        assert second["observations"][0]["version"] == 1
        assert second["code"] == first["code"]
    # 快照结束后新状态可见
    out = assess(client, ["O1"], species="A", threshold=50)
    assert out["observations"][0]["tips"] == ["r2"]
    assert out["observations"][0]["version"] == 2
