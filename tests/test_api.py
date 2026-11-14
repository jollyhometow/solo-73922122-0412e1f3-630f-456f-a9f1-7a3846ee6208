"""汇交 API 行为测试：乱序、冲突、裁决、幂等、整包拒绝。"""
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


def get(client, obs):
    r = client.get(f"/observations/{obs}")
    assert r.status_code == 200
    return r.json()


# ------------------------------------------------------------ 基本与乱序

def test_single_revision_valid(client):
    r = client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=1)))
    assert r.status_code == 200
    st = r.json()["observations"][0]
    assert st["status"] == "valid"
    assert st["tips"] == ["r1"]
    assert st["effective"] == {"revision_id": "r1", "species": "A",
                               "count": 1, "deleted": False}


def test_out_of_order_pending_then_recompute(client):
    r = client.post("/packages", json=pkg(rev("O1", "r2", ["r1"], species="B", count=2)))
    st = r.json()["observations"][0]
    assert st["status"] == "pending"
    assert st["missing_ancestors"] == ["r1"]
    assert st["effective"] is None
    assert get(client, "O1")["status"] == "pending"

    r = client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=1)))
    st = r.json()["observations"][0]
    assert st["status"] == "valid"
    assert st["tips"] == ["r2"]
    assert st["effective"]["species"] == "B"  # 顶端修订的值
    assert st["missing_ancestors"] == []


def test_deep_chain_pending_until_all_ancestors_arrive(client):
    client.post("/packages", json=pkg(rev("O1", "r3", ["r2"], species="C", count=3)))
    r = client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=1)))
    # r2 仍缺：继续待补齐
    assert r.json()["observations"][0]["status"] == "pending"
    assert get(client, "O1")["missing_ancestors"] == ["r2"]
    r = client.post("/packages", json=pkg(rev("O1", "r2", ["r1"], species="B", count=2)))
    st = r.json()["observations"][0]
    assert st["status"] == "valid"
    assert st["tips"] == ["r3"]


def test_delete_tip_is_valid_deleted(client):
    client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=1)))
    r = client.post("/packages", json=pkg(rev("O1", "r2", ["r1"], delete=True)))
    st = r.json()["observations"][0]
    assert st["status"] == "valid"
    assert st["effective"] == {"revision_id": "r2", "species": None,
                               "count": None, "deleted": True}


# ------------------------------------------------------------ 冲突与裁决

def test_concurrent_edit_delete_conflict_and_resolution(client):
    client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=1)))
    client.post("/packages", json=pkg(rev("O1", "r2", ["r1"], species="A", count=5)))
    r = client.post("/packages", json=pkg(rev("O1", "r3", ["r1"], delete=True)))
    st = r.json()["observations"][0]
    assert st["status"] == "conflict"
    assert st["tips"] == ["r2", "r3"]
    assert st["effective"] is None

    conf = client.get("/conflicts").json()
    assert [c["observation_id"] for c in conf] == ["O1"]

    # 过期分支（缺少 r3）→ 409 并返回当前分支
    r = client.post("/observations/O1/resolutions",
                    json={"revision_id": "rx", "parents": ["r2"],
                          "species": "A", "count": 9})
    assert r.status_code == 409
    assert r.json()["detail"]["current_tips"] == ["r2", "r3"]

    # 以所见全部分支为父裁决
    r = client.post("/observations/O1/resolutions",
                    json={"revision_id": "r4", "parents": ["r2", "r3"],
                          "species": "A", "count": 5})
    assert r.status_code == 201
    st = r.json()
    assert st["status"] == "valid"
    assert st["tips"] == ["r4"]
    assert st["effective"]["count"] == 5
    assert client.get("/conflicts").json() == []

    # 裁决后迟到的并发分支仍须保留冲突
    r = client.post("/packages", json=pkg(rev("O1", "r5", ["r2"], species="A", count=7)))
    st = r.json()["observations"][0]
    assert st["status"] == "conflict"
    assert st["tips"] == ["r4", "r5"]


def test_concurrent_edits_conflict(client):
    client.post("/packages", json=pkg(
        rev("O1", "r1", [], species="A", count=1),
        rev("O1", "r2", [], species="A", count=2),
    ))
    st = get(client, "O1")
    assert st["status"] == "conflict"
    assert st["tips"] == ["r1", "r2"]


def test_resolution_idempotent_replay(client):
    client.post("/packages", json=pkg(
        rev("O1", "r1", [], species="A", count=1),
        rev("O1", "r2", [], species="A", count=2),
    ))
    body = {"revision_id": "r3", "parents": ["r1", "r2"], "species": "A", "count": 3}
    r1 = client.post("/observations/O1/resolutions", json=body)
    assert r1.status_code == 201
    r2 = client.post("/observations/O1/resolutions", json=body)
    assert r2.status_code == 200  # 同号同内容：幂等
    assert r2.json()["tips"] == ["r3"]
    # 同号异内容 → 422
    bad = dict(body, count=4)
    r3 = client.post("/observations/O1/resolutions", json=bad)
    assert r3.status_code == 422


def test_resolution_unknown_observation_404(client):
    r = client.post("/observations/NOPE/resolutions",
                    json={"revision_id": "r9", "parents": [], "species": "A", "count": 1})
    assert r.status_code == 404


# ------------------------------------------------------------ 幂等与整包拒绝

def test_idempotent_resubmit(client):
    p = pkg(rev("O1", "r1", [], species="A", count=1))
    client.post("/packages", json=p)
    r = client.post("/packages", json=p)
    assert r.status_code == 200
    assert r.json()["duplicates"] == ["r1"]
    assert r.json()["accepted"] == []
    st = get(client, "O1")
    assert st["status"] == "valid" and st["version"] == 1


def test_same_id_different_content_rejected_and_atomic(client):
    client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=1)))
    r = client.post("/packages", json=pkg(
        rev("O1", "r1", [], species="A", count=2),      # 异内容
        rev("O1", "r2", ["r1"], species="A", count=3),  # 本身合法，但整包拒绝
    ))
    assert r.status_code == 422
    st = get(client, "O1")
    assert st["tips"] == ["r1"]  # r2 未入库，不留部分记录


def test_duplicate_in_package_different_content_rejected(client):
    r = client.post("/packages", json=pkg(
        rev("O1", "r1", [], species="A", count=1),
        rev("O1", "r1", [], species="A", count=2),
    ))
    assert r.status_code == 422
    assert client.get("/observations/O1").status_code == 404


def test_cycle_rejected(client):
    r = client.post("/packages", json=pkg(
        rev("O1", "r1", ["r2"], species="A", count=1),
        rev("O1", "r2", ["r1"], species="A", count=1),
    ))
    assert r.status_code == 422
    assert client.get("/observations/O1").status_code == 404


def test_self_parent_rejected(client):
    r = client.post("/packages", json=pkg(rev("O1", "r1", ["r1"], species="A", count=1)))
    assert r.status_code == 422
    assert client.get("/observations/O1").status_code == 404


def test_cross_observation_in_package_rejected(client):
    r = client.post("/packages", json=pkg(
        rev("O1", "r1", [], species="A", count=1),
        rev("O2", "r2", ["r1"], species="B", count=2),
    ))
    assert r.status_code == 422
    assert client.get("/observations/O1").status_code == 404
    assert client.get("/observations/O2").status_code == 404


def test_cross_observation_existing_parent_rejected(client):
    client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=1)))
    r = client.post("/packages", json=pkg(rev("O2", "r2", ["r1"], species="B", count=2)))
    assert r.status_code == 422
    assert client.get("/observations/O2").status_code == 404


def test_cross_observation_late_parent_rejected(client):
    # 子先父后：父到达时观测号不符 → 整包拒绝，子仍待补齐
    client.post("/packages", json=pkg(rev("O1", "r2", ["r1"], species="A", count=2)))
    r = client.post("/packages", json=pkg(rev("O2", "r1", [], species="B", count=1)))
    assert r.status_code == 422
    assert get(client, "O1")["status"] == "pending"
    assert client.get("/observations/O2").status_code == 404


def test_payload_validation(client):
    # 删除与物种/数量互斥
    r = client.post("/packages", json=pkg(
        rev("O1", "r1", [], species="A", count=1, delete=True)))
    assert r.status_code == 422
    # 非删除必须携带 species 与 count
    r = client.post("/packages", json=pkg(rev("O1", "r1", [])))
    assert r.status_code == 422


def test_revisions_listing(client):
    client.post("/packages", json=pkg(
        rev("O1", "r1", [], species="A", count=1),
        rev("O1", "r2", ["r1"], delete=True),
    ))
    r = client.get("/observations/O1/revisions")
    assert r.status_code == 200
    body = {x["revision_id"]: x for x in r.json()}
    assert body["r1"]["parents"] == []
    assert body["r2"]["parents"] == ["r1"]
    assert body["r2"]["delete"] is True
    assert client.get("/observations/NOPE/revisions").status_code == 404
