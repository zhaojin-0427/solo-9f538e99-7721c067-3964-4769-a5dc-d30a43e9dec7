"""业务服务：所有写操作均在单个 IMMEDIATE 事务内完成，失败整体回滚。"""
from __future__ import annotations

import json
import sqlite3

from .database import immediate_tx
from .models import BatchCreate, ConfirmationCreate, ResultCreate, TaxonomyCreate
from .resolver import (
    REASON_CYCLE,
    REASON_DIVERGENT,
    REASON_MANUAL,
    confirmable_endpoints,
    resolve_name,
)


class ConflictError(Exception):
    """409 冲突：唯一编号重复、状态冲突、异内容重试等。"""


class NotFoundError(Exception):
    """404：引用的批次、修订、结果或核定不存在。"""


# ---------- 观测批次 ----------

def create_batch(conn: sqlite3.Connection, payload: BatchCreate) -> int:
    seen: set[str] = set()
    for obs in payload.observations:
        if obs.code in seen:
            raise ConflictError(f"批次内观测编号重复：{obs.code}")
        seen.add(obs.code)

    with immediate_tx(conn):
        cur = conn.execute(
            "INSERT INTO observation_batch(note) VALUES (?)", (payload.note,)
        )
        batch_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO observation(batch_id, code, name, line_no) VALUES (?, ?, ?, ?)",
            [
                (batch_id, obs.code, obs.name, idx)
                for idx, obs in enumerate(payload.observations, start=1)
            ],
        )
    return batch_id


def get_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM observation_batch WHERE id = ?", (batch_id,)
    ).fetchone()


def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT b.id, b.note, b.created_at, COUNT(o.code) AS observation_count
        FROM observation_batch b
        LEFT JOIN observation o ON o.batch_id = b.id
        GROUP BY b.id
        ORDER BY b.id
        """
    ).fetchall()


def list_observations(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT line_no, code, name FROM observation WHERE batch_id = ? ORDER BY line_no",
        (batch_id,),
    ).fetchall()


# ---------- 分类表（整版更正 -> 新修订） ----------

def create_taxonomy_revision(
    conn: sqlite3.Connection, payload: TaxonomyCreate
) -> int:
    seen_names: set[str] = set()
    for name in payload.accepted_names:
        if name in seen_names:
            raise ConflictError(f"接受名重复：{name}")
        seen_names.add(name)

    seen_edges: set[tuple[str, str]] = set()
    for edge in payload.synonym_edges:
        key = (edge.synonym, edge.target)
        if key in seen_edges:
            raise ConflictError(f"同义名指向关系重复：{edge.synonym} -> {edge.target}")
        seen_edges.add(key)

    with immediate_tx(conn):
        cur = conn.execute(
            "INSERT INTO taxonomy_revision(note) VALUES (?)", (payload.note,)
        )
        revision_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO accepted_name(revision_id, name) VALUES (?, ?)",
            [(revision_id, name) for name in payload.accepted_names],
        )
        conn.executemany(
            "INSERT INTO synonym_edge(revision_id, synonym, target) VALUES (?, ?, ?)",
            [(revision_id, e.synonym, e.target) for e in payload.synonym_edges],
        )
        # 整版更正：旧修订标记 superseded，引用旧修订的有效结果立即过期
        conn.execute(
            """
            UPDATE taxonomy_revision
               SET superseded_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE revision_id != ? AND superseded_at IS NULL
            """,
            (revision_id,),
        )
        conn.execute(
            """
            UPDATE result
               SET status = 'expired'
             WHERE status = 'active' AND revision_id != ?
            """,
            (revision_id,),
        )
        # 同一已提交状态内重算全部有效核定：终点集合改变、引入环或非接受名终点
        # 的核定永久失效；与本观测无关的更正保留核定，且之后改回也不复活。
        _invalidate_confirmations(conn, revision_id)
    return revision_id


def _invalidate_confirmations(
    conn: sqlite3.Connection, new_revision_id: int
) -> None:
    """按新修订重算每个有效核定所绑定观测的解析结论，使失效核定落库。"""
    accepted, adjacency = _load_revision_graph(conn, new_revision_id)
    confirmations = conn.execute(
        "SELECT * FROM manual_confirmation WHERE status = 'valid'"
    ).fetchall()
    for conf in confirmations:
        obs = conn.execute(
            "SELECT name FROM observation WHERE batch_id = ? AND code = ?",
            (conf["batch_id"], conf["observation_code"]),
        ).fetchone()
        stored_set = json.loads(conf["terminal_set"])
        reason_code, reason = _invalidation_reason(obs, stored_set, adjacency, accepted)
        if reason_code is None:
            continue  # 无关更正：终点集合未变且仍为无环全接受名终点，核定保留
        conn.execute(
            """
            UPDATE manual_confirmation
               SET status = 'invalidated',
                   invalidated_reason_code = ?,
                   invalidated_reason = ?,
                   invalidated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE confirmation_id = ?
            """,
            (reason_code, reason, conf["confirmation_id"]),
        )


def _invalidation_reason(
    obs: sqlite3.Row | None,
    stored_set: list[str],
    adjacency: dict[str, list[str]],
    accepted: set[str],
) -> tuple[str | None, str | None]:
    if obs is None:
        return (
            "terminal_set_changed",
            "观测已不存在，原终点集合无法维持，人工核定永久失效",
        )
    resolution = resolve_name(obs["name"], adjacency, accepted)
    if resolution.reason_code == REASON_CYCLE:
        return (
            "cycle_introduced",
            f"分类更正后观测 {obs['name']} 的指向关系中出现环，人工核定永久失效",
        )
    if resolution.non_accepted_sinks:
        return (
            "non_accepted_terminal",
            "分类更正后存在终止于非接受名（"
            + "、".join(resolution.non_accepted_sinks)
            + "）的路径，人工核定永久失效",
        )
    if resolution.terminal_set != stored_set:
        return (
            "terminal_set_changed",
            "分类更正改变了该观测名称的终点接受名集合（原："
            + "、".join(stored_set)
            + "；现："
            + "、".join(resolution.terminal_set)
            + "），人工核定永久失效",
        )
    return None, None


def get_revision(conn: sqlite3.Connection, revision_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM taxonomy_revision WHERE revision_id = ?", (revision_id,)
    ).fetchone()


def latest_revision_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT MAX(revision_id) AS rid FROM taxonomy_revision"
    ).fetchone()
    return row["rid"]


def list_revisions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.revision_id, r.note, r.created_at, r.superseded_at,
               (SELECT COUNT(*) FROM accepted_name a WHERE a.revision_id = r.revision_id) AS accepted_count,
               (SELECT COUNT(*) FROM synonym_edge e WHERE e.revision_id = r.revision_id) AS edge_count
          FROM taxonomy_revision r
         ORDER BY r.revision_id
        """
    ).fetchall()


def _load_revision_graph(
    conn: sqlite3.Connection, revision_id: int
) -> tuple[set[str], dict[str, list[str]]]:
    accepted = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM accepted_name WHERE revision_id = ?", (revision_id,)
        )
    }
    adjacency: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT synonym, target FROM synonym_edge WHERE revision_id = ?",
        (revision_id,),
    ):
        adjacency.setdefault(row["synonym"], []).append(row["target"])
    return accepted, adjacency


# ---------- 人工核定 ----------

def create_confirmation(
    conn: sqlite3.Connection, payload: ConfirmationCreate
) -> tuple[int, bool]:
    """创建人工核定。

    :returns: (confirmation_id, idempotent_replay)；同内容重试返回原核定并置 True。
    """
    with immediate_tx(conn):
        result = conn.execute(
            "SELECT * FROM result WHERE result_id = ?", (payload.result_id,)
        ).fetchone()
        if result is None:
            raise NotFoundError(f"供数结果不存在：result_id={payload.result_id}")

        item = conn.execute(
            "SELECT * FROM result_item WHERE result_id = ? AND observation_code = ?",
            (payload.result_id, payload.observation_code),
        ).fetchone()
        if item is None:
            raise NotFoundError(
                f"结果 {payload.result_id} 中不存在观测：observation_code={payload.observation_code}"
            )

        existing = conn.execute(
            "SELECT * FROM manual_confirmation WHERE result_id = ? AND observation_code = ?",
            (payload.result_id, payload.observation_code),
        ).fetchone()
        if existing is not None:
            # 同结果、同观测：同内容重试幂等返回原核定；异内容一律 409
            if (
                existing["accepted_name"] == payload.accepted_name
                and existing["basis"] == payload.basis
            ):
                return existing["confirmation_id"], True
            raise ConflictError(
                "该结果与观测已存在不同内容的人工核定，核定不可改写，返回 409"
            )

        # 新核定只收当前结果：写锁内确认状态，杜绝与更正/生成交错
        if result["status"] != "active":
            raise ConflictError(
                f"结果 {payload.result_id} 不是当前供数（status={result['status']}），"
                "人工核定只允许提交给当前结果"
            )

        # 按结果固定的修订重算：只允许核定无环且所有路径均终止于接受名的分叉
        accepted, adjacency = _load_revision_graph(conn, result["revision_id"])
        resolution = resolve_name(item["input_name"], adjacency, accepted)
        endpoints = confirmable_endpoints(resolution)
        if endpoints is None:
            raise ConflictError(
                "该观测不是可人工核定的分叉记录：仅无环且所有路径均终止于接受名的"
                "分叉待核记录可核定（环、无接受名或已纳入记录不受理）"
            )
        if payload.accepted_name not in endpoints:
            raise ConflictError(
                "候选接受名必须是分叉终点之一："
                + "、".join(endpoints)
                + f"，收到 {payload.accepted_name}"
            )

        cur = conn.execute(
            """
            INSERT INTO manual_confirmation(
                result_id, batch_id, observation_code, accepted_name, basis,
                terminal_set, revision_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'valid')
            """,
            (
                payload.result_id,
                result["batch_id"],
                payload.observation_code,
                payload.accepted_name,
                payload.basis,
                json.dumps(endpoints, ensure_ascii=False),
                result["revision_id"],
            ),
        )
        return cur.lastrowid, False


def get_confirmation(
    conn: sqlite3.Connection, confirmation_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM manual_confirmation WHERE confirmation_id = ?",
        (confirmation_id,),
    ).fetchone()


def list_confirmations(
    conn: sqlite3.Connection,
    result_id: int | None = None,
    status: str | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM manual_confirmation WHERE 1=1"
    params: list = []
    if result_id is not None:
        sql += " AND result_id = ?"
        params.append(result_id)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY confirmation_id"
    return conn.execute(sql, params).fetchall()


def _valid_confirmation_index(
    conn: sqlite3.Connection, batch_id: int
) -> dict[str, sqlite3.Row]:
    """取批次当前全部有效核定，按观测编号索引；同观测多条时取最新一条。"""
    rows = conn.execute(
        """
        SELECT * FROM manual_confirmation
         WHERE batch_id = ? AND status = 'valid'
         ORDER BY confirmation_id
        """,
        (batch_id,),
    ).fetchall()
    return {row["observation_code"]: row for row in rows}


# ---------- 供数结果 ----------

def generate_result(conn: sqlite3.Connection, payload: ResultCreate) -> int:
    if get_batch(conn, payload.batch_id) is None:
        raise NotFoundError(f"观测批次不存在：batch_id={payload.batch_id}")

    revision_id = payload.revision_id
    if revision_id is None:
        revision_id = latest_revision_id(conn)
        if revision_id is None:
            raise NotFoundError("尚无分类表修订，无法生成供数结果")
    elif get_revision(conn, revision_id) is None:
        raise NotFoundError(f"分类表修订不存在：revision_id={revision_id}")

    observations = list_observations(conn, payload.batch_id)

    with immediate_tx(conn):
        # 事务内重新确认修订状态：旧修订不得作为当前供数
        revision = conn.execute(
            "SELECT revision_id, superseded_at FROM taxonomy_revision WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        current = conn.execute(
            "SELECT MAX(revision_id) AS rid FROM taxonomy_revision"
        ).fetchone()["rid"]
        is_current = current == revision_id

        accepted, adjacency = _load_revision_graph(conn, revision_id)
        # 后续供数采用有效核定（同写事务内读取，归属对应同一已提交状态）
        valid_confirmations = _valid_confirmation_index(conn, payload.batch_id)

        cur = conn.execute(
            """
            INSERT INTO result(batch_id, revision_id, status, included_count, pending_count)
            VALUES (?, ?, 'active', 0, 0)
            """,
            (payload.batch_id, revision_id),
        )
        result_id = cur.lastrowid

        items = []
        included = pending = 0
        for obs in observations:
            r = resolve_name(obs["name"], adjacency, accepted)
            if r.included:
                included += 1
                items.append(
                    (result_id, obs["line_no"], obs["code"], obs["name"],
                     "included", r.accepted_name, None, None, None, None)
                )
                continue

            endpoints = confirmable_endpoints(r)
            conf = valid_confirmations.get(obs["code"])
            if (
                endpoints is not None
                and conf is not None
                and json.loads(conf["terminal_set"]) == r.terminal_set
                and conf["accepted_name"] in endpoints
            ):
                # 有效核定：保存核定号与依据，旧结果不改写
                included += 1
                items.append(
                    (result_id, obs["line_no"], obs["code"], obs["name"],
                     "included", conf["accepted_name"], REASON_MANUAL,
                     f"按人工核定 #{conf['confirmation_id']} 采纳：{conf['basis']}",
                     conf["confirmation_id"], conf["basis"])
                )
            else:
                pending += 1
                items.append(
                    (result_id, obs["line_no"], obs["code"], obs["name"],
                     "pending", None, r.reason_code, r.reason, None, None)
                )

        conn.executemany(
            """
            INSERT INTO result_item(
                result_id, line_no, observation_code, input_name,
                item_status, accepted_name, reason_code, reason,
                confirmation_id, confirmation_basis
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            items,
        )
        conn.execute(
            "UPDATE result SET included_count = ?, pending_count = ? WHERE result_id = ?",
            (included, pending, result_id),
        )

        # 若该修订已被更正（交错场景），结果直接落为 expired；
        # 只有结果本身是当前供数时，才使同批次旧的 active 结果过期，
        # 保证旧修订生成的结果绝不会覆盖当前供数，且每批次至多一份 active。
        if not is_current or revision["superseded_at"] is not None:
            conn.execute(
                "UPDATE result SET status = 'expired' WHERE result_id = ?",
                (result_id,),
            )
        else:
            conn.execute(
                """
                UPDATE result
                   SET status = 'expired'
                 WHERE status = 'active'
                   AND batch_id = ? AND result_id != ?
                """,
                (payload.batch_id, result_id),
            )
    return result_id


def get_result(conn: sqlite3.Connection, result_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM result WHERE result_id = ?", (result_id,)
    ).fetchone()


def list_result_items(conn: sqlite3.Connection, result_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM result_item WHERE result_id = ? ORDER BY line_no", (result_id,)
    ).fetchall()


def list_results(
    conn: sqlite3.Connection, batch_id: int | None = None, status: str | None = None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM result WHERE 1=1"
    params: list = []
    if batch_id is not None:
        sql += " AND batch_id = ?"
        params.append(batch_id)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY result_id"
    return conn.execute(sql, params).fetchall()
