"""API 端到端关键场景。"""
def _batch(client, observations, note=None):
    return client.post("/batches", json={"observations": observations, "note": note})


def _taxonomy(client, accepted, edges=None, note=None):
    return client.post(
        "/taxonomies",
        json={"accepted_names": accepted, "synonym_edges": edges or [], "note": note},
    )


OBS = [
    {"code": "o1", "name": "旧名甲"},   # 链 -> 接受名A
    {"code": "o2", "name": "分叉名"},   # 分叉到 A、B
    {"code": "o3", "name": "环名x"},    # 环
    {"code": "o4", "name": "无人名"},   # 无接受名
    {"code": "o5", "name": "菱形名"},   # 分叉后合流到 A
    {"code": "o6", "name": "接受名A"},  # 本身即接受名
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
]


def test_full_workflow(client):
    r = _batch(client, OBS, note="第一批")
    assert r.status_code == 201
    batch_id = r.json()["id"]
    assert r.json()["observation_count"] == 6

    r = _taxonomy(client, ["接受名A", "接受名B"], EDGES_V1, note="v1")
    assert r.status_code == 201
    rev1 = r.json()["revision_id"]
    assert r.json()["edge_count"] == 10

    r = client.post("/results", json={"batch_id": batch_id})
    assert r.status_code == 201
    result = r.json()
    assert result["status"] == "active"
    assert result["revision_id"] == rev1
    assert result["included_count"] == 3   # o1, o5, o6
    assert result["pending_count"] == 3    # o2 分叉, o3 环, o4 无接受名

    by_code = {i["observation_code"]: i for i in result["items"]}
    assert by_code["o1"]["item_status"] == "included"
    assert by_code["o1"]["accepted_name"] == "接受名A"
    assert by_code["o5"]["accepted_name"] == "接受名A"
    assert by_code["o6"]["accepted_name"] == "接受名A"
    assert by_code["o2"]["reason_code"] == "divergent_accepted"
    assert by_code["o3"]["reason_code"] == "cycle"
    assert by_code["o4"]["reason_code"] == "no_accepted_name"
    # 固定结果可按结果号重读
    rid = result["result_id"]
    again = client.get(f"/results/{rid}").json()
    assert again["items"] == result["items"]


def test_taxonomy_correction_expires_old_results(client):
    bid = _batch(client, OBS).json()["id"]
    _taxonomy(client, ["接受名A", "接受名B"], EDGES_V1)
    r1 = client.post("/results", json={"batch_id": bid}).json()
    assert r1["status"] == "active"

    # 整版更正：分叉名在 v2 只指向 A
    edges_v2 = [e for e in EDGES_V1 if not (
        e["synonym"] == "分叉名" and e["target"] == "接受名B"
    )]
    r = _taxonomy(client, ["接受名A", "接受名B"], edges_v2, note="v2 更正")
    rev2 = r.json()["revision_id"]
    assert r.json()["superseded_at"] is None

    # 旧结果标过期，但仍可重读
    old = client.get(f"/results/{r1['result_id']}").json()
    assert old["status"] == "expired"
    assert old["included_count"] == 3  # 内容冻结在 rev1

    # 新结果基于新修订，分叉名现在可以纳入
    r2 = client.post("/results", json={"batch_id": bid}).json()
    assert r2["status"] == "active"
    assert r2["revision_id"] == rev2
    assert r2["included_count"] == 4
    assert r2["pending_count"] == 2
    by_code = {i["observation_code"]: i for i in r2["items"]}
    assert by_code["o2"]["item_status"] == "included"

    # 列表可按状态过滤
    actives = client.get("/results", params={"status": "active"}).json()
    assert [x["result_id"] for x in actives] == [r2["result_id"]]


def test_explicit_old_revision_generation_is_expired(client):
    bid = _batch(client, OBS).json()["id"]
    rev1 = _taxonomy(client, ["接受名A", "接受名B"], EDGES_V1).json()["revision_id"]
    r1 = client.post("/results", json={"batch_id": bid}).json()
    assert r1["status"] == "active"
    _taxonomy(client, ["接受名A"], [], note="v2")  # 更正产生 rev2

    r = client.post("/results", json={"batch_id": bid, "revision_id": rev1})
    assert r.status_code == 201
    body = r.json()
    # 旧修订生成的结果不得成为当前供数
    assert body["status"] == "expired"
    assert body["revision_id"] == rev1
    # 更正时旧 active 已过期；对旧修订再生成也不能产生任何 active
    assert client.get("/results", params={"status": "active"}).json() == []

    # 重新用当前修订生成，成为唯一 active；之后再对旧修订生成，不得顶替它
    current = client.post("/results", json={"batch_id": bid}).json()
    assert current["status"] == "active"
    again_old = client.post(
        "/results", json={"batch_id": bid, "revision_id": rev1}
    ).json()
    assert again_old["status"] == "expired"
    actives = client.get("/results", params={"status": "active"}).json()
    assert [x["result_id"] for x in actives] == [current["result_id"]]


def test_limits_and_validation(client):
    # 超过 100 条 -> 422
    too_many = [{"code": f"c{i}", "name": "n"} for i in range(101)]
    assert client.post("/batches", json={"observations": too_many}).status_code == 422

    # 批内编号重复 -> 409，且不留部分数据
    dup = [{"code": "c1", "name": "a"}, {"code": "c1", "name": "b"}]
    assert client.post("/batches", json={"observations": dup}).status_code == 409
    assert client.get("/batches").json() == []

    # 空白名称 -> 422
    assert client.post(
        "/batches", json={"observations": [{"code": "c1", "name": "   "}]}
    ).status_code == 422

    # 同义边超过 40 条 -> 422
    edges = [{"synonym": f"s{i}", "target": "A"} for i in range(41)]
    assert client.post(
        "/taxonomies", json={"accepted_names": ["A"], "synonym_edges": edges}
    ).status_code == 422

    # 接受名重复 -> 409，不产生修订
    r = client.post(
        "/taxonomies", json={"accepted_names": ["A", "A"], "synonym_edges": []}
    )
    assert r.status_code == 409
    assert client.get("/taxonomies").json() == []


def test_missing_references_and_404(client):
    assert client.post("/results", json={"batch_id": 999}).status_code == 404
    assert client.get("/batches/999").status_code == 404
    assert client.get("/results/999").status_code == 404

    bid = _batch(client, [{"code": "o1", "name": "x"}]).json()["id"]
    # 没有任何分类表时生成 -> 404
    assert client.post("/results", json={"batch_id": bid}).status_code == 404

    _taxonomy(client, ["A"], [])
    assert client.post(
        "/results", json={"batch_id": bid, "revision_id": 999}
    ).status_code == 404


def test_empty_taxonomy_rejected_but_edges_optional(client):
    # 至少一个接受名
    r = client.post("/taxonomies", json={"accepted_names": [], "synonym_edges": []})
    assert r.status_code == 422
    # 没有同义边是合法的
    r = client.post("/taxonomies", json={"accepted_names": ["A"]})
    assert r.status_code == 201


def test_result_immutable_after_correction(client):
    bid = _batch(client, [{"code": "o1", "name": "旧名"}]).json()["id"]
    _taxonomy(client, ["A"], [{"synonym": "旧名", "target": "A"}])
    r1 = client.post("/results", json={"batch_id": bid}).json()

    # 更正为旧名不再指向 A
    _taxonomy(client, ["A"], [{"synonym": "旧名", "target": "孤儿"}])
    r2 = client.post("/results", json={"batch_id": bid}).json()

    frozen = client.get(f"/results/{r1['result_id']}").json()
    assert frozen["status"] == "expired"
    assert frozen["items"][0]["accepted_name"] == "A"
    assert r2["items"][0]["item_status"] == "pending"
