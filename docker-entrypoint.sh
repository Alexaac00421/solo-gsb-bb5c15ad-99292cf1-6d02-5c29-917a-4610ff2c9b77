#!/usr/bin/env bash
# 容器入口：等待数据库就绪 -> 初始化表结构（幂等） -> 启动 HTTP 服务
set -euo pipefail

echo "[entrypoint] waiting for PostgreSQL ..."
python - <<'PY'
import os, time, sys
import psycopg

dsn = os.environ["DATABASE_URL"]
last = None
for i in range(60):
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        print("[entrypoint] PostgreSQL is ready")
        sys.exit(0)
    except Exception as exc:  # 数据库尚未就绪，重试
        last = exc
        time.sleep(1)
print(f"[entrypoint] PostgreSQL not ready after retries: {last}", file=sys.stderr)
sys.exit(1)
PY

# schema.sql 在 FastAPI 启动时通过 init_schema() 幂等执行；此处先显式初始化便于观察日志
python - <<'PY'
from app.db import init_pool, init_schema, close_pool
init_pool()
init_schema()
close_pool()
print("[entrypoint] schema initialized")
PY

exec uvicorn app.main:app --host "${APP_HOST:-0.0.0.0}" --port "${APP_PORT:-8080}"
