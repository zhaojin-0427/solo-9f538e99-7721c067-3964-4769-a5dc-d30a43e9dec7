"""pytest 配置：每个用例使用独立临时 SQLite 文件，并覆盖应用的连接依赖。"""
import os
import tempfile

import pytest

# 必须在 import app.main 之前指定测试库路径
_TMPDIR = tempfile.mkdtemp(prefix="eco_test_")
os.environ["ECO_DB_PATH"] = os.path.join(_TMPDIR, "test.db")

from app.database import connect, init_db  # noqa: E402
from app.main import app, get_conn  # noqa: E402


@pytest.fixture()
def conn():
    path = os.path.join(_TMPDIR, f"case_{os.getpid()}_{id(object())}.db")
    connection = connect(path)
    init_db(connection)
    yield connection
    connection.close()
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture()
def client(conn):
    from fastapi.testclient import TestClient

    def _override():
        yield conn

    app.dependency_overrides[get_conn] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
