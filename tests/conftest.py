"""测试夹具：启动便携 PostgreSQL（pgserver）+ FastAPI TestClient。

必须在导入 app.* 之前设置 DATABASE_URL。
"""
import os
import tempfile
import uuid

import pgserver
import pytest

_tmpdir = tempfile.mkdtemp(prefix="pgtest-")
_pg = pgserver.get_server(_tmpdir)
os.environ["DATABASE_URL"] = _pg.get_uri()

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402

# 预先建池建表；startup 再调 init_pool/init_schema 时为幂等空操作
db.init_pool(os.environ["DATABASE_URL"])
db.init_schema()


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_tables():
    """每个用例前清空（TRUNCATE 不受 append-only 行级触发器限制）。"""
    with db.pool.connection() as conn:
        conn.execute(
            "TRUNCATE group_members, transfer_groups, day_settlements, "
            "ledger_entries, trips, fare_rules RESTART IDENTITY CASCADE"
        )
    yield


@pytest.fixture
def account():
    return f"acc-{uuid.uuid4().hex[:8]}"
