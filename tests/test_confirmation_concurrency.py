"""核定、更正与生成交错的并发测试。

多线程各自独立连接，交替执行「更正 / 生成 / 核定」，验证：
1. 无异常、无部分结果或部分核定（每条结果 item 完整）；
2. 核定归属始终对应一个已提交状态（绑定的修订号真实存在）；
3. 有效核定的终端集合与其绑定观测在当前最新修订下的解析结论一致，
   否则它必然已被置为 invalidated（不会出现「该失效却仍有效」的核定）；
4. 当前供数中凡引用核定号的条目，该核定必为 valid 且候选名匹配。
"""
import os
import tempfile
import threading

from app import services
from app.database import connect, init_db
from app.models import (
    BatchCreate,
    ConfirmationCreate,
    EdgeIn,
    ObservationIn,
    ResultCreate,
    TaxonomyCreate,
)
from app.resolver import resolve_name


def _make_db():
    path = os.path.join(tempfile.mkdtemp(prefix="eco_confirm_conc_"), "conc.db")
    conn = connect(path)
    init_db(conn)
    return conn, path


def _taxonomy(seed: int) -> TaxonomyCreate:
    edges = [
        EdgeIn(synonym="旧名甲", target="接受名A"),
        EdgeIn(synonym="环名x", target="环名y"),
        EdgeIn(synonym="环名y", target="环名x"),
    ]
    if seed % 2 == 0:
        edges += [
            EdgeIn(synonym="分叉名", target="接受名A"),
            EdgeIn(synonym="分叉名", target="接受名B"),
        ]
    else:
        edges.append(EdgeIn(synonym="分叉名", target="接受名A"))
    return TaxonomyCreate(
        accepted_names=["接受名A", "接受名B"],
        synonym_edges=edges,
        note=f"rev-{seed}",
    )


OBSERVATIONS = [
    ObservationIn(code="o1", name="旧名甲"),
    ObservationIn(code="o2", name="分叉名"),
    ObservationIn(code="o3", name="环名x"),
    ObservationIn(code="o4", name="接受名A"),
]


def test_interleaved_confirmation_correction_generation():
    admin, db_file = _make_db()
    batch_id = services.create_batch(
        admin, BatchCreate(observations=OBSERVATIONS, note="并发批次")
    )
    services.create_taxonomy_revision(admin, _taxonomy(0))
    first_result = services.generate_result(admin, ResultCreate(batch_id=batch_id))

    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def worker(worker_id: int, iterations: int, mode: str):
        conn = connect(db_file)
        try:
            barrier.wait()
            for i in range(iterations):
                if mode == "correct":
                    services.create_taxonomy_revision(
                        conn, _taxonomy(worker_id * 100 + i)
                    )
                elif mode == "generate":
                    services.generate_result(conn, ResultCreate(batch_id=batch_id))
                else:  # confirm：找当前 active 结果，尝试核定 o2
                    rows = services.list_results(conn, batch_id, "active")
                    if not rows:
                        continue
                    rid = rows[0]["result_id"]
                    try:
                        services.create_confirmation(
                            conn,
                            ConfirmationCreate(
                                result_id=rid,
                                observation_code="o2",
                                accepted_name="接受名A",
                                basis=f"w{worker_id}-{i} 的鉴定证据",
                            ),
                        )
                    except services.ConflictError:
                        # 异内容重试、结果已过期、该修订下 o2 不可核定等，均为预期冲突
                        pass
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, args=(1, 30, "correct")),
        threading.Thread(target=worker, args=(2, 30, "generate")),
        threading.Thread(target=worker, args=(3, 30, "confirm")),
        threading.Thread(target=worker, args=(4, 30, "confirm")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"并发期间出现异常: {errors[:3]}"

    # 不变量 1：所有结果完整、无部分写入
    for row in services.list_results(admin):
        items = services.list_result_items(admin, row["result_id"])
        assert len(items) == len(OBSERVATIONS)
        assert row["included_count"] + row["pending_count"] == len(OBSERVATIONS)

    # 不变量 2：每批次至多一个 active，且属最新修订
    latest = services.latest_revision_id(admin)
    actives = [r for r in services.list_results(admin) if r["status"] == "active"]
    assert len(actives) <= 1
    for r in actives:
        assert r["revision_id"] == latest

    # 不变量 3：核定绑定的修订/结果真实存在；同结果同观测至多一条
    confirmations = admin.execute(
        "SELECT * FROM manual_confirmation ORDER BY confirmation_id"
    ).fetchall()
    keys = [(c["result_id"], c["observation_code"]) for c in confirmations]
    assert len(keys) == len(set(keys))
    for c in confirmations:
        assert services.get_result(admin, c["result_id"]) is not None
        assert services.get_revision(admin, c["revision_id"]) is not None

    # 不变量 4：当前仍 valid 的核定，与最新修订下该观测的终点集合必须一致；
    # 不一致的一定已 invalidated（失效判定在更正同事务内完成）
    accepted, adjacency = services._load_revision_graph(admin, latest)
    import json

    for c in confirmations:
        obs = admin.execute(
            "SELECT name FROM observation WHERE batch_id = ? AND code = ?",
            (batch_id, c["observation_code"]),
        ).fetchone()
        resolution = resolve_name(obs["name"], adjacency, accepted)
        if resolution.terminal_set == json.loads(c["terminal_set"]):
            # 集合一致时，valid/invalidated 都可能出现（历史更正曾失效则不复活）
            continue
        assert c["status"] == "invalidated", (
            f"核定 {c['confirmation_id']} 终点集合已变却仍为 valid"
        )

    # 不变量 5：当前供数引用的核定号必为 valid，且候选名一致
    if actives:
        items = services.list_result_items(admin, actives[0]["result_id"])
        for item in items:
            if item["confirmation_id"] is not None:
                conf = services.get_confirmation(admin, item["confirmation_id"])
                assert conf["status"] == "valid"
                assert conf["accepted_name"] == item["accepted_name"]
                assert item["confirmation_basis"] == conf["basis"]

    admin.close()
