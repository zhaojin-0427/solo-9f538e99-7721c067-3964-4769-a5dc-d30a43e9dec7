"""业务服务：所有写操作均在单个 IMMEDIATE 事务内完成，失败整体回滚。"""
from __future__ import annotations

import sqlite3

from .database import immediate_tx
from .models import BatchCreate, ResultCreate, TaxonomyCreate
from .resolver import resolve_name


class ConflictError(Exception):
    """409 冲突：唯一编号重复等。"""


class NotFoundError(Exception):
    """404：引用的批次或修订不存在。"""


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
    return revision_id


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
                     "included", r.accepted_name, None, None)
                )
            else:
                pending += 1
                items.append(
                    (result_id, obs["line_no"], obs["code"], obs["name"],
                     "pending", None, r.reason_code, r.reason)
                )

        conn.executemany(
            """
            INSERT INTO result_item(result_id, line_no, observation_code, input_name,
                                    item_status, accepted_name, reason_code, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
