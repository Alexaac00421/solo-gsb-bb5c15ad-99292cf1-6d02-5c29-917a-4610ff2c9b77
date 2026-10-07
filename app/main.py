"""HTTP API 入口。

接口一览：
  POST   /trips                       提交乘车记录（同号同内容幂等，异内容冲突）
  POST   /trips/{trip_id}/correct     更正（需携带当前 version）
  POST   /trips/{trip_id}/cancel      撤销（需携带当前 version）
  GET    /trips/{trip_id}             查询乘次状态
  GET    /accounts/{account_id}/days/{op_day}   查询运营日换乘组与实收
  GET    /accounts/{account_id}/ledger          游标后流水（翻页不重不漏）
  POST   /rules                       发布按运营日生效的换乘时限/封顶规则
  GET    /rules/{op_day}              查询指定运营日适用规则
  GET    /rules                       列出全部已发布规则
  GET    /healthz                     健康检查
"""
from __future__ import annotations

from datetime import date, datetime

from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import service
from .db import close_pool, init_pool, init_schema
from .errors import ApiError
from .models import CorrectIn, RulePublishIn, TripIn
from .timeutil import to_beijing_iso


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_pool()
    init_schema()  # 启动即完成表结构初始化（幂等）
    try:
        yield
    finally:
        close_pool()


app = FastAPI(title="公交换乘票价清分服务", version="1.0.0", lifespan=lifespan)


# ------------------------------------------------------------ 异常处理

@app.exception_handler(ApiError)
async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


def _parse_validation_code(exc: RequestValidationError) -> tuple[str, str]:
    """把 pydantic 校验信息映射为明确的业务错误码。"""
    # 字段级类型解析错误（未进入自定义 validator 即被拦截）按字段名映射
    field_code = {
        "boarding_at": "INVALID_TIME",
        "base_fare_cents": "INVALID_FARE",
        "trip_id": "INVALID_TRIP_ID",
        "account_id": "INVALID_ACCOUNT_ID",
        "expected_version": "INVALID_VERSION",
        "effective_day": "INVALID_DATE",
        "op_day": "INVALID_DATE",
        "transfer_window_minutes": "INVALID_WINDOW",
        "daily_cap_cents": "INVALID_CAP",
    }
    errs = exc.errors()
    messages, code = [], "INVALID_REQUEST"
    for e in errs:
        msg = e.get("msg", "")
        messages.append(msg)
        # 自定义 validator 抛出的 "CODE: message" 优先
        for token in ("INVALID_TIME", "INVALID_FARE", "INVALID_TRIP_ID",
                      "INVALID_ACCOUNT_ID", "INVALID_VERSION", "INVALID_DATE",
                      "INVALID_WINDOW", "INVALID_CAP"):
            if token in msg:
                code = token
                break
        else:
            loc = e.get("loc") or ()
            field = loc[-1] if loc else None
            if field in field_code and code == "INVALID_REQUEST":
                code = field_code[field]
    return code, "; ".join(messages) or "request validation failed"


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    code, message = _parse_validation_code(exc)
    return JSONResponse(status_code=422,
                        content={"error": {"code": code, "message": message}})


@app.exception_handler(psycopg.errors.QueryCanceled)
async def _timeout(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=503,
                        content={"error": {"code": "BUSY",
                                           "message": "service busy, please retry"}})


# ------------------------------------------------------------ 响应辅助

def _trip_payload(row: dict, idempotent: bool) -> dict:
    return {
        "trip": service.trip_view(row).model_dump(),
        "idempotent": idempotent,
    }


# ------------------------------------------------------------ 路由

@app.post("/trips", status_code=201)
def submit_trip(body: TripIn):
    row, idem = service.submit_trip(
        body.trip_id, body.account_id, body.boarding_at, body.base_fare_cents)
    return _trip_payload(row, idem)


@app.post("/trips/{trip_id}/correct")
def correct_trip(trip_id: str, body: CorrectIn):
    row, idem = service.correct_trip(
        trip_id, body.account_id, body.boarding_at,
        body.base_fare_cents, body.expected_version)
    return _trip_payload(row, idem)


@app.post("/trips/{trip_id}/cancel")
def cancel_trip(
    trip_id: str,
    expected_version: int = Query(..., description="乘次当前版本，陈旧版本将被拒绝"),
):
    row, idem = service.cancel_trip(trip_id, expected_version)
    return _trip_payload(row, idem)


@app.get("/trips/{trip_id}")
def get_trip(trip_id: str):
    return {"trip": service.trip_view(service.get_trip(trip_id)).model_dump()}


@app.get("/accounts/{account_id}/days/{op_day}")
def get_day(account_id: str, op_day: date):
    return service.get_day(account_id, op_day).model_dump()


# ------------------------------------------------------------ 费率规则

@app.post("/rules")
def publish_rule(body: RulePublishIn):
    """发布按运营日生效的换乘时限/单日封顶规则；同内容同版本重发幂等。"""
    out, _ = service.publish_rule(
        body.effective_day, body.transfer_window_minutes,
        body.daily_cap_cents, body.expected_version)
    return out.model_dump()


@app.get("/rules/{op_day}")
def get_rule(op_day: date):
    """查询指定运营日适用规则（命中发布记录或沿用默认，含覆盖区间）。"""
    return service.get_rule(op_day).model_dump()


@app.get("/rules")
def list_rules():
    """列出全部已发布规则（按生效日升序）。"""
    return {"rules": [r.model_dump() for r in service.list_rules()]}


@app.get("/accounts/{account_id}/ledger")
def get_ledger(
    account_id: str,
    after_seq: int = Query(0, ge=0, description="游标：返回 seq 严格大于该值的流水"),
    limit: int = Query(50, ge=1, le=200),
    op_day: date | None = Query(None, description="可选，按运营日过滤"),
):
    entries, next_cursor = service.list_ledger(account_id, after_seq, limit, op_day)
    return {
        "account_id": account_id,
        "after_seq": after_seq,
        "limit": limit,
        "next_cursor": next_cursor,
        "entries": [e.model_dump() for e in entries],
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": to_beijing_iso(datetime.now().astimezone())}
