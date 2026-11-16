"""生成与分类表更正交错的并发测试。

多个线程各自持有独立连接，交替执行 generate / create_taxonomy_revision，
验证：
1. 无数据库死锁/部分结果（每条结果的 item 数与批次观测数一致）；
2. 任一时刻每个批次至多一条 active 结果；
3. active 结果的 revision_id 必为当前最新修订（旧修订永不覆盖当前供数）；
4. 最新修订下的 active 结果（若存在）与串行重算结果一致。
"""
import os
import tempfile
import threading

from app import services
from app.database import connect, init_db
from app.models import BatchCreate, ObservationIn, TaxonomyCreate, EdgeIn, ResultCreate


def _make_db():
    path = os.path.join(tempfile.mkdtemp(prefix="eco_conc_"), "conc.db")
    conn = connect(path)
    init_db(conn)
    return conn, path

def _build_taxonomy(rev_seed: int) -> TaxonomyCreate:
    # 让“分叉名”在奇偶修订间于纳入/待核之间切换，制造有差异的结果
    edges = [
        EdgeIn(synonym="旧名甲", target="接受名A"),
        EdgeIn(synonym="菱形名", target="s1"),
        EdgeIn(synonym="菱形名", target="s2"),
        EdgeIn(synonym="s1", target="接受名A"),
        EdgeIn(synonym="s2", target="接受名A"),
        EdgeIn(synonym="环名x", target="环名y"),
        EdgeIn(synonym="环名y", target="环名x"),
    ]
    if rev_seed % 2 == 0:
        edges.append(EdgeIn(synonym="分叉名", target="接受名A"))
        edges.append(EdgeIn(synonym="分叉名", target="接受名B"))
    else:
        edges.append(EdgeIn(synonym="分叉名", target="接受名A"))
    return TaxonomyCreate(
        accepted_names=["接受名A", "接受名B"],
        synonym_edges=edges,
        note=f"rev-{rev_seed}",
    )


OBSERVATIONS = [
    ObservationIn(code="o1", name="旧名甲"),
    ObservationIn(code="o2", name="分叉名"),
    ObservationIn(code="o3", name="环名x"),
    ObservationIn(code="o4", name="无人名"),
    ObservationIn(code="o5", name="菱形名"),
    ObservationIn(code="o6", name="接受名A"),
]


def test_interleaved_generation_and_correction():
    admin_conn, db_file = _make_db()
    batch_id = services.create_batch(
        admin_conn, BatchCreate(observations=OBSERVATIONS, note="并发批次")
    )
    services.create_taxonomy_revision(admin_conn, _build_taxonomy(0))
    n_batches_obs = len(OBSERVATIONS)
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def worker(worker_id: int, iterations: int, do_corrections: bool):
        conn = connect(db_file)
        try:
            barrier.wait()
            for i in range(iterations):
                if do_corrections and i % 2 == 0:
                    services.create_taxonomy_revision(conn, _build_taxonomy(worker_id * 100 + i))
                else:
                    services.generate_result(conn, ResultCreate(batch_id=batch_id))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, args=(1, 25, True)),
        threading.Thread(target=worker, args=(2, 25, True)),
        threading.Thread(target=worker, args=(3, 25, False)),
        threading.Thread(target=worker, args=(4, 25, False)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发期间出现异常: {errors[:3]}"

    # 不变量 1：每条结果完整（无部分结果）
    results = services.list_results(admin_conn)
    assert len(results) >= 50
    for row in results:
        items = services.list_result_items(admin_conn, row["result_id"])
        assert len(items) == n_batches_obs
        assert row["included_count"] + row["pending_count"] == n_batches_obs

    # 不变量 2：任一时刻的终态——每批次至多一条 active
    actives = [r for r in results if r["status"] == "active"]
    assert len(actives) <= 1

    # 不变量 3：active 结果必属当前最新修订
    latest = services.latest_revision_id(admin_conn)
    for row in actives:
        assert row["revision_id"] == latest

    # 不变量 4：用当前修订串行重算，与 active 结果（若存在）逐项一致
    expected_id = services.generate_result(
        admin_conn, ResultCreate(batch_id=batch_id)
    )
    expected = services.list_result_items(admin_conn, expected_id)
    current_actives = [
        r for r in services.list_results(admin_conn) if r["status"] == "active"
    ]
    assert len(current_actives) == 1
    actual = services.list_result_items(admin_conn, current_actives[0]["result_id"])
    assert [(i["observation_code"], i["item_status"], i["accepted_name"], i["reason_code"])
            for i in actual] == [
               (i["observation_code"], i["item_status"], i["accepted_name"], i["reason_code"])
               for i in expected
            ]

    # 历史结果仍可重读且冻结在各自修订上
    first = results[0]
    detail = services.list_result_items(admin_conn, first["result_id"])
    assert len(detail) == n_batches_obs
    admin_conn.close()


def test_failed_generation_leaves_no_partial_result():
    conn, _ = _make_db()
    bid = services.create_batch(conn, BatchCreate(observations=OBSERVATIONS))
    services.create_taxonomy_revision(conn, _build_taxonomy(0))
    before = len(services.list_results(conn))

    # 引用不存在的批次 -> 404，结果表无新增
    try:
        services.generate_result(conn, ResultCreate(batch_id=9999))
        assert False, "应抛出 NotFoundError"
    except services.NotFoundError:
        pass
    assert len(services.list_results(conn)) == before
    conn.close()
