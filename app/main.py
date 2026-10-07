"""HTTP API 入口。

接口一览：
  POST   /trips                       提交乘车记录（同号同内容幂等，异内容冲突）
  POST   /trips/batch                 补录批量原子结算（1～100 项提交/更正/撤销）
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
from .models import (BatchSettleIn, BatchSettleView, CorrectIn, RulePublishIn,
                     TripIn)
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
    body: dict = {"code": exc.code, "message": exc.message}
    # 批量结算错误附带出错位置（下标从 0 起）及对应乘次号
    body.update(exc.extra())
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": body},
    )


def _loc_index(loc) -> int | None:
    """批量校验错误定位：loc 形如 ('body', 'operations', i, ...)，取下标 i。"""
    if loc and len(loc) >= 3 and loc[1] == "operations" \
            and isinstance(loc[2], int):
        return int(loc[2])
    return None


def _parse_validation_code(exc: RequestValidationError) -> tuple[str, str, int | None]:
    """把 pydantic 校验信息映射为明确的业务错误码。

    返回 (code, message, 批量出错下标|None)；批量项内错误的 loc 为
    ('body','operations',i,...)，下标 i 原样返回给调用方定位出错项。
    """
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
    code_tokens = ("INVALID_TIME", "INVALID_FARE", "INVALID_TRIP_ID",
                   "INVALID_ACCOUNT_ID", "INVALID_VERSION", "INVALID_DATE",
                   "INVALID_WINDOW", "INVALID_CAP")
    errs = exc.errors()
    messages, code, index = [], "INVALID_REQUEST", None
    for e in errs:
        msg = e.get("msg", "")
        messages.append(msg)
        loc = e.get("loc") or ()
        if index is None:
            index = _loc_index(loc)
        etype = e.get("type", "")
        # 自定义 validator 抛出的 "CODE: message" 优先
        for token in code_tokens:
            if token in msg:
                code = token
                break
        else:
            # 批量请求：判别字段非法 -> INVALID_OP；条目结构非法 -> INVALID_BATCH；
            # 条目内字段按字段名沿用单笔校验码（loc 末位为字段名）
            if loc[:2] == ("body", "operations") and len(loc) >= 3:
                if etype in ("union_tag_invalid", "union_tag_not_found"):
                    code = "INVALID_OP"
                elif isinstance(loc[-1], str) and loc[-1] in field_code:
                    code = field_code[loc[-1]]
                elif code == "INVALID_REQUEST":
                    code = "INVALID_BATCH"
            else:
                field = loc[-1] if loc else None
                if field in field_code and code == "INVALID_REQUEST":
                    code = field_code[field]
                # operations 列表缺失/为空/超长等结构错误
                elif field == "operations" and code == "INVALID_REQUEST":
                    code = "INVALID_BATCH"
    return code, "; ".join(messages) or "request validation failed", index


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    code, message, index = _parse_validation_code(exc)
    body: dict = {"code": code, "message": message}
    if index is not None:
        body["index"] = index
    return JSONResponse(status_code=422, content={"error": body})


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


@app.post("/trips/batch")
def submit_batch(body: BatchSettleIn):
    """补录批量原子结算：按输入顺序执行 1～100 项提交/更正/撤销。

    任一项非法或冲突则整批不改乘次、换乘组、日结与流水，错误体携带
    出错项下标（index，从 0 起）与原单笔业务错误码。
    """
    out = service.submit_batch(body.operations)
    return BatchSettleView.model_validate(out).model_dump()


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
