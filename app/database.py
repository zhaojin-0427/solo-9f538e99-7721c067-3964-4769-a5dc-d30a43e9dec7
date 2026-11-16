"""SQLite 连接管理、建表脚本与写事务上下文。"""
from __future__ import annotations

import contextlib
import os
import sqlite3

DEFAULT_DB_PATH = os.path.join(os.getcwd(), "eco.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS observation_batch (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    note       TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS observation (
    batch_id INTEGER NOT NULL REFERENCES observation_batch(id) ON DELETE CASCADE,
    code     TEXT NOT NULL,
    name     TEXT NOT NULL,
    line_no  INTEGER NOT NULL,
    PRIMARY KEY (batch_id, code)
);

CREATE TABLE IF NOT EXISTS taxonomy_revision (
    revision_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    note          TEXT,
    superseded_at TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS accepted_name (
    revision_id INTEGER NOT NULL REFERENCES taxonomy_revision(revision_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    PRIMARY KEY (revision_id, name)
);

CREATE TABLE IF NOT EXISTS synonym_edge (
    revision_id INTEGER NOT NULL REFERENCES taxonomy_revision(revision_id) ON DELETE CASCADE,
    synonym     TEXT NOT NULL,
    target      TEXT NOT NULL,
    PRIMARY KEY (revision_id, synonym, target)
);

CREATE TABLE IF NOT EXISTS result (
    result_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id       INTEGER NOT NULL REFERENCES observation_batch(id),
    revision_id    INTEGER NOT NULL REFERENCES taxonomy_revision(revision_id),
    status         TEXT NOT NULL CHECK (status IN ('active', 'expired')),
    included_count INTEGER NOT NULL,
    pending_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_result_revision_status
    ON result(revision_id, status);

CREATE TABLE IF NOT EXISTS result_item (
    result_id        INTEGER NOT NULL REFERENCES result(result_id) ON DELETE CASCADE,
    line_no          INTEGER NOT NULL,
    observation_code TEXT NOT NULL,
    input_name       TEXT NOT NULL,
    item_status      TEXT NOT NULL CHECK (item_status IN ('included', 'pending')),
    accepted_name    TEXT,
    reason_code      TEXT,
    reason           TEXT,
    PRIMARY KEY (result_id, line_no)
);
"""


def db_path() -> str:
    return os.environ.get("ECO_DB_PATH", DEFAULT_DB_PATH)


def connect(path: str | None = None) -> sqlite3.Connection:
    # check_same_thread=False：FastAPI 测试客户端在独立线程发请求；
    # 写安全由 BEGIN IMMEDIATE 的库级写锁保证。
    conn = sqlite3.connect(
        path or db_path(), timeout=10.0, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextlib.contextmanager
def immediate_tx(conn: sqlite3.Connection):
    """立即写事务：同一时刻只允许一个写者，保证生成/更正串行化。

    任何异常都会回滚，保证失败时不留下部分结果。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
