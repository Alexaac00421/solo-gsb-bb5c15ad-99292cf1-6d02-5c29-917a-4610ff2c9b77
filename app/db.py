"""数据库连接池与结构初始化。"""
from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

pool: ConnectionPool | None = None


def init_pool(dsn: str | None = None) -> ConnectionPool:
    global pool
    if pool is None:
        pool = ConnectionPool(
            dsn or settings.database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return pool


def close_pool() -> None:
    global pool
    if pool is not None:
        pool.close()
        pool = None


def init_schema() -> None:
    """幂等执行 schema.sql，可重复调用（容器启动即完成初始化）。"""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    assert pool is not None
    with pool.connection() as conn:
        conn.execute(sql)
