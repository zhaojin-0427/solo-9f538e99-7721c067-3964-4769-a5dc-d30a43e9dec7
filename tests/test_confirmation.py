"""人工核定端到端测试：可核定范围、幂等/409、失效规则、供数套用与交错一致性。"""
from app.database import connect, init_db


def _batch(client, observations, note=None):
    return client.post(
        "/batches", json={"observations": observations, "note": note}
    )


def _taxonomy(client, accepted, edges=None, note=None):
    return client.post(
        "/taxonomies",
        json={"accepted_names": accepted, "synonym_edges": edges or [], "note": note},
    )


OBS = [
    {"code": "o1", "name": "旧名甲"},   # 链 -> A（included）
    {"code": "o2", "name": "分叉名"},   # 分叉到 A、B（可核定）
    {"code": "o3", "name": "环名x"},    # 环（不可核定）
    {"code": "o4", "name": "无人名"},   # 无接受名（不可核定）
    {"code": "o5", "name": "菱形名"},   # 分叉合流到 A（included）
    {"code": "o6", "name": "接受名A"},  # 直达（included）
    {"code": "o7", "name": "半截名"},   # 一条到 A、一条断在非接受名（不可核定）
]

EDGES_V1 = [
    {"synonym": "旧名甲", "target": "中间名"},
    {"synonym": "中间名", "target": "接受名A"},
    {"synonym": "分叉名", "target": "接受名A"},
    {"synonym": "分叉名", "target": "接受名B"},
    {"synonym": "环名x", "target": "环名y"},
    {"synonym": "环名y", "target": "环名x"},
    {"synonym": "菱形名", "target": "s1"},
    {"synonym": "菱形名", "target": "s2"},
    {"synonym": "s1", "target": "接受名A"},
    {"synonym": "s2", "target": "接受名A"},
    {"synonym": "半截名", "target": "接受名A"},
    {"synonym": "半截名", "target": "孤儿"},
]


def _setup(client):
    bid = _batch(client, OBS).json()["id"]
    rev = _taxonomy(client, ["接受名A", "接受名B"], EDGES_V1, note="v1").json()["revision_id"]
    result = client.post("/results", json={"batch_id": bid}).json()
    return bid, rev, result


def test_confirmation_happy_path_and_applied_in_later_supply(client):
    bid, rev, result = _setup(client)
    rid = result["result_id"]

    # 生成后：分叉项待核
    by_code = {i["observation_code"]: i for i in result["items"]}
    assert by_code["o2"]["item_status"] == "pending"
    assert by_code["o2"]["reason_code"] == "divergent_accepted"
    assert by_code["o2"]["confirmation_id"] is None

    # 提交核定
    r = client.post(
        "/confirmations",
        json={
            "result_id": rid,
            "observation_code": "o2",
            "accepted_name": "接受名B",
            "basis": "标本复核：形态与模式标本一致，采纳接受名B",
        },
    )
    assert r.status_code == 201
    conf = r.json()
    cid = conf["confirmation_id"]
    assert conf["status"] == "valid"
    assert conf["terminal_set"] == ["接受名A", "接受名B"]
    assert conf["revision_id"] == rev
    assert conf["invalidated_reason_code"] is None

    # 同结果同观测的同内容重试 -> 200 返回原核定，不新建
    again = client.post(
        "/confirmations",
        json={
            "result_id": rid,
            "observation_code": "o2",
            "accepted_name": "接受名B",
            "basis": "标本复核：形态与模式标本一致，采纳接受名B",
        },
    )
    assert again.status_code == 200
    assert again.json()["confirmation_id"] == cid
    assert len(client.get("/confirmations").json()) == 1

    # 旧结果不改写：原结果中 o2 仍为 pending
    frozen = client.get(f"/results/{rid}").json()
    old_item = next(i for i in frozen["items"] if i["observation_code"] == "o2")
    assert old_item["item_status"] == "pending"
    assert old_item["confirmation_id"] is None

    # 后续供数采用有效核定：保存核定号与依据
    new_result = client.post("/results", json={"batch_id": bid}).json()
    assert new_result["status"] == "active"
    assert new_result["included_count"] == result["included_count"] + 1
    assert new_result["pending_count"] == result["pending_count"] - 1
    new_item = next(i for i in new_result["items"] if i["observation_code"] == "o2")
    assert new_item["item_status"] == "included"
    assert new_item["accepted_name"] == "接受名B"
    assert new_item["reason_code"] == "manual_confirmation"
    assert new_item["confirmation_id"] == cid
    assert "标本复核" in new_item["confirmation_basis"]

    # 同批次只有一个 active，旧结果已过期
    actives = client.get("/results", params={"status": "active"}).json()
    assert [x["result_id"] for x in actives] == [new_result["result_id"]]

    # 按核定号可读取依据
    detail = client.get(f"/confirmations/{cid}").json()
    assert detail["basis"].startswith("标本复核")
    assert client.get("/confirmations/999").status_code == 404


def test_different_content_retry_returns_409(client):
    bid, _, result = _setup(client)
    rid = result["result_id"]
    body = {
        "result_id": rid,
        "observation_code": "o2",
        "accepted_name": "接受名B",
        "basis": "依据一",
    }
    assert client.post("/confirmations", json=body).status_code == 201

    # 异内容（候选名不同）-> 409
    conflict = client.post(
        "/confirmations",
        json={**body, "accepted_name": "接受名A"},
    )
    assert conflict.status_code == 409

    # 异内容（依据不同）-> 409
    conflict2 = client.post(
        "/confirmations",
        json={**body, "basis": "依据二"},
    )
    assert conflict2.status_code == 409

    # 冲突后没有新增核定
    confs = client.get("/confirmations").json()
    assert len(confs) == 1
    assert confs[0]["accepted_name"] == "接受名B"


def test_only_confirmable_divergent_records_accepted(client):
    _, _, result = _setup(client)
    rid = result["result_id"]

    def confirm(code, name="接受名A", basis="依据"):
        return client.post(
            "/confirmations",
            json={
                "result_id": rid,
                "observation_code": code,
                "accepted_name": name,
                "basis": basis,
            },
        )

    # 环 -> 409
    assert confirm("o3").status_code == 409
    # 无接受名 -> 409
    assert confirm("o4").status_code == 409
    # 部分路径断在非接受名 -> 409
    assert confirm("o7").status_code == 409
    # 已纳入的观测 -> 409
    assert confirm("o1").status_code == 409

    # 候选名不是分叉终点 -> 409
    bad_target = confirm("o2", name="不存在的接受名")
    assert bad_target.status_code == 409

    # 观测编号在该结果中不存在 -> 404
    assert confirm("nope").status_code == 404
    # 结果不存在 -> 404
    r = client.post(
        "/confirmations",
        json={
            "result_id": 999,
            "observation_code": "o2",
            "accepted_name": "接受名A",
            "basis": "x",
        },
    )
    assert r.status_code == 404

    # 全部失败均未留下核定
    assert client.get("/confirmations").json() == []


def test_confirmation_rejected_for_non_current_result(client):
    bid, _, result = _setup(client)
    rid = result["result_id"]

    # 生成新当前结果，旧结果过期
    newer = client.post("/results", json={"batch_id": bid}).json()
    assert newer["result_id"] != rid
    old = client.get(f"/results/{rid}").json()
    assert old["status"] == "expired"

    # 对过期结果提交新核定 -> 409（新核定只收当前结果）
    r = client.post(
        "/confirmations",
        json={
            "result_id": rid,
            "observation_code": "o2",
            "accepted_name": "接受名A",
            "basis": "迟到的核定",
        },
    )
    assert r.status_code == 409
    assert client.get("/confirmations").json() == []


def test_correction_changing_terminal_set_invalidates_permanently(client):
    bid, _, result = _setup(client)
    rid = result["result_id"]
    cid = client.post(
        "/confirmations",
        json={
            "result_id": rid,
            "observation_code": "o2",
            "accepted_name": "接受名B",
            "basis": "证据B",
        },
    ).json()["confirmation_id"]

    # 更正：分叉名只指向 A（终点集合由 [A,B] 变为 [A]）
    edges_v2 = [e for e in EDGES_V1 if not (
        e["synonym"] == "分叉名" and e["target"] == "接受名B"
    )]
    _taxonomy(client, ["接受名A", "接受名B"], edges_v2, note="v2")

    conf = client.get(f"/confirmations/{cid}").json()
    assert conf["status"] == "invalidated"
    assert conf["invalidated_reason_code"] == "terminal_set_changed"
    assert conf["invalidated_at"] is not None

    # 新结果中 o2 已由算法自动纳入，不引用失效核定
    new_result = client.post("/results", json={"batch_id": bid}).json()
    item = next(i for i in new_result["items"] if i["observation_code"] == "o2")
    assert item["item_status"] == "included"
    assert item["accepted_name"] == "接受名A"
    assert item["reason_code"] is None
    assert item["confirmation_id"] is None

    # 后来改回 v1 的结构，核定不复活
    _taxonomy(client, ["接受名A", "接受名B"], EDGES_V1, note="v3 改回")
    revived = client.get(f"/confirmations/{cid}").json()
    assert revived["status"] == "invalidated"

    # 失效核定上的同内容重试仍是原核定（返回 200，不新建）
    replay = client.post(
        "/confirmations",
        json={
            "result_id": rid,
            "observation_code": "o2",
            "accepted_name": "接受名B",
            "basis": "证据B",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["confirmation_id"] == cid

    # 对新的当前结果可以重新核定（新核定号）
    current = client.post("/results", json={"batch_id": bid}).json()
    new_conf = client.post(
        "/confirmations",
        json={
            "result_id": current["result_id"],
            "observation_code": "o2",
            "accepted_name": "接受名B",
            "basis": "重新核定依据",
        },
    )
    assert new_conf.status_code == 201
    assert new_conf.json()["confirmation_id"] != cid
    assert new_conf.json()["status"] == "valid"


def test_cycle_and_non_accepted_terminal_invalidation(client):
    bid, _, result = _setup(client)
    rid = result["result_id"]
    cid = client.post(
        "/confirmations",
        json={
            "result_id": rid,
            "observation_code": "o2",
            "accepted_name": "接受名A",
            "basis": "证据A",
        },
    ).json()["confirmation_id"]

    # 更正引入环（分叉名 -> 环名x，环名x <-> 环名y）
    edges_cycle = EDGES_V1 + [{"synonym": "分叉名", "target": "环名x"}]
    _taxonomy(client, ["接受名A", "接受名B"], edges_cycle, note="v2 引入环")
    conf = client.get(f"/confirmations/{cid}").json()
    assert conf["status"] == "invalidated"
    assert conf["invalidated_reason_code"] == "cycle_introduced"

    # 再更正：去掉环，但让分叉名有一条断在非接受名
    edges_dangling = [
        e for e in EDGES_V1
        if not (e["synonym"] == "环名x" or e["synonym"] == "环名y")
    ] + [{"synonym": "分叉名", "target": "孤儿"}]
    _taxonomy(client, ["接受名A", "接受名B"], edges_dangling, note="v3 非接受名终点")
    conf = client.get(f"/confirmations/{cid}").json()
    assert conf["status"] == "invalidated"  # 已失效不复活，原因停留在环
    # 用新核定验证非接受名终点的失效原因：先给当前结果重新核定
    cur = client.post("/results", json={"batch_id": bid}).json()
    # 当前图里分叉名仍是分叉且终点含非接受名孤儿 -> 不可核定
    r = client.post(
        "/confirmations",
        json={
            "result_id": cur["result_id"],
            "observation_code": "o2",
            "accepted_name": "接受名A",
            "basis": "x",
        },
    )
    assert r.status_code == 409


def test_unrelated_correction_keeps_confirmation(client):
    bid, _, result = _setup(client)
    rid = result["result_id"]
    cid = client.post(
        "/confirmations",
        json={
            "result_id": rid,
            "observation_code": "o2",
            "accepted_name": "接受名B",
            "basis": "证据B",
        },
    ).json()["confirmation_id"]

    # 无关更正：只动「菱形名」的中间名，分叉名的终点集合不变
    unrelated = [e for e in EDGES_V1 if e["synonym"] != "s2"] + [
        {"synonym": "s2", "target": "接受名A"},  # 原本即 s2 -> 接受名A，保持等价
        {"synonym": "新名称", "target": "接受名A"},
    ]
    _taxonomy(client, ["接受名A", "接受名B"], unrelated, note="v2 无关更正")

    conf = client.get(f"/confirmations/{cid}").json()
    assert conf["status"] == "valid"
    assert conf["invalidated_reason_code"] is None

    # 后续供数继续套用该核定
    new_result = client.post("/results", json={"batch_id": bid}).json()
    item = next(i for i in new_result["items"] if i["observation_code"] == "o2")
    assert item["confirmation_id"] == cid
    assert item["accepted_name"] == "接受名B"


def test_same_named_other_observations_unaffected(client):
    # 两个批次含同名观测；只核定其中一个，另一个不受影响
    obs = [{"code": "a1", "name": "分叉名"}]
    bid1 = _batch(client, obs).json()["id"]
    bid2 = _batch(client, [{"code": "b1", "name": "分叉名"}]).json()["id"]
    _taxonomy(
        client,
        ["接受名A", "接受名B"],
        [
            {"synonym": "分叉名", "target": "接受名A"},
            {"synonym": "分叉名", "target": "接受名B"},
        ],
    )
    r1 = client.post("/results", json={"batch_id": bid1}).json()
    r2 = client.post("/results", json={"batch_id": bid2}).json()

    cid = client.post(
        "/confirmations",
        json={
            "result_id": r1["result_id"],
            "observation_code": "a1",
            "accepted_name": "接受名B",
            "basis": "只核定批次1",
        },
    ).json()["confirmation_id"]

    # 批次2 重新生成：同名观测仍为 pending，不带核定
    res2 = client.post("/results", json={"batch_id": bid2}).json()
    item = res2["items"][0]
    assert item["observation_code"] == "b1"
    assert item["item_status"] == "pending"
    assert item["confirmation_id"] is None

    # 批次1 的后续供数带核定
    res1 = client.post("/results", json={"batch_id": bid1}).json()
    assert res1["items"][0]["confirmation_id"] == cid


def test_legacy_sqlite_volume_migrates_and_supply_without_confirmation(client, conn):
    # 用旧版 schema（无 manual_confirmation、result_item 无核定列）建库后再迁移
    path = conn.execute("PRAGMA database_list").fetchone()[2]

    import os
    import sqlite3

    # client fixture 占用的连接先释放，直接用底层 sqlite3 重建旧卷
    conn.close()
    os.remove(path)
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE observation_batch (
            id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        CREATE TABLE observation (
            batch_id INTEGER NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL, line_no INTEGER NOT NULL,
            PRIMARY KEY (batch_id, code));
        CREATE TABLE taxonomy_revision (
            revision_id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT, superseded_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        CREATE TABLE accepted_name (revision_id INTEGER NOT NULL, name TEXT NOT NULL,
            PRIMARY KEY (revision_id, name));
        CREATE TABLE synonym_edge (revision_id INTEGER NOT NULL, synonym TEXT NOT NULL, target TEXT NOT NULL,
            PRIMARY KEY (revision_id, synonym, target));
        CREATE TABLE result (
            result_id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL,
            revision_id INTEGER NOT NULL, status TEXT NOT NULL,
            included_count INTEGER NOT NULL, pending_count INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        CREATE INDEX idx_result_revision_status ON result(revision_id, status);
        CREATE TABLE result_item (
            result_id INTEGER NOT NULL, line_no INTEGER NOT NULL, observation_code TEXT NOT NULL,
            input_name TEXT NOT NULL, item_status TEXT NOT NULL, accepted_name TEXT,
            reason_code TEXT, reason TEXT, PRIMARY KEY (result_id, line_no));
        """
    )
    old.execute("INSERT INTO observation_batch(id, note) VALUES (1, '旧卷批次')")
    old.execute("INSERT INTO observation(batch_id, code, name, line_no) VALUES (1, 'o1', '旧名', 1)")
    old.execute("INSERT INTO taxonomy_revision(revision_id, note) VALUES (1, '旧卷修订')")
    old.execute("INSERT INTO accepted_name(revision_id, name) VALUES (1, '接受名A')")
    old.execute("INSERT INTO synonym_edge(revision_id, synonym, target) VALUES (1, '旧名', '接受名A')")
    old.execute(
        "INSERT INTO result(result_id, batch_id, revision_id, status, included_count, pending_count) "
        "VALUES (1, 1, 1, 'active', 1, 0)"
    )
    old.execute(
        "INSERT INTO result_item(result_id, line_no, observation_code, input_name, item_status, "
        "accepted_name, reason_code, reason) VALUES (1, 1, 'o1', '旧名', 'included', '接受名A', NULL, NULL)"
    )
    old.commit()
    old.close()

    # 应用启动式迁移：新代码打开旧卷，幂等补表补列
    migrated = connect(path)
    init_db(migrated)
    cols = {r[1] for r in migrated.execute("PRAGMA table_info(result_item)")}
    assert {"confirmation_id", "confirmation_basis"} <= cols
    migrated.execute("SELECT * FROM manual_confirmation").fetchall()  # 表存在即可

    # 无核定供数仍可正常读取，核定相关字段为空
    row = migrated.execute(
        "SELECT confirmation_id, confirmation_basis, accepted_name FROM result_item WHERE result_id = 1"
    ).fetchone()
    assert row[0] is None and row[1] is None and row[2] == "接受名A"
    migrated.close()

    # 通过 API 在迁移后的旧卷上继续工作：重新连接并跑通核定全流程
    fresh = connect(path)
    init_db(fresh)
    from app.main import app, get_conn

    def _override():
        yield fresh

    app.dependency_overrides[get_conn] = _override
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        detail = c.get("/results/1").json()
        assert detail["items"][0]["confirmation_id"] is None
        # 旧结果是 active，分叉名需要先造一个；新增批次+修订+生成+核定
        c.post(
            "/batches",
            json={"observations": [{"code": "n1", "name": "分叉名"}]},
        )
        c.post(
            "/taxonomies",
            json={
                "accepted_names": ["接受名A", "接受名B"],
                "synonym_edges": [
                    {"synonym": "分叉名", "target": "接受名A"},
                    {"synonym": "分叉名", "target": "接受名B"},
                ],
            },
        )
        nr = c.post("/results", json={"batch_id": 2}).json()
        conf = c.post(
            "/confirmations",
            json={
                "result_id": nr["result_id"],
                "observation_code": "n1",
                "accepted_name": "接受名A",
                "basis": "旧卷迁移后核定",
            },
        )
        assert conf.status_code == 201
    app.dependency_overrides.clear()
    fresh.close()
