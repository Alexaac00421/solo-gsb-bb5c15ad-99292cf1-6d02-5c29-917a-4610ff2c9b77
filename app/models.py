"""请求/响应模型与入参校验。

校验错误信息形如 "CODE: message"，由异常处理器解析为统一的 422 响应体。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .timeutil import ensure_aware, op_day_of

MAX_TEXT_LEN = 128
MIN_YEAR, MAX_YEAR = 2000, 2100


def _validate_text(v: Any, field: str) -> str:
    if not isinstance(v, str):
        raise ValueError(f"INVALID_{field.upper()}: {field} must be a string")
    v = v.strip()
    if not v:
        raise ValueError(f"INVALID_{field.upper()}: {field} must not be empty")
    if len(v) > MAX_TEXT_LEN:
        raise ValueError(f"INVALID_{field.upper()}: {field} must be <= {MAX_TEXT_LEN} chars")
    if any(ord(c) < 0x20 for c in v):
        raise ValueError(f"INVALID_{field.upper()}: {field} must not contain control chars")
    return v


def _validate_fare(v: Any) -> int:
    # bool 是 int 的子类，须先排除；只接受整数，拒绝 1.0 / 字符串等
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("INVALID_FARE: base_fare_cents must be a non-negative integer")
    if v < 0:
        raise ValueError("INVALID_FARE: base_fare_cents must be non-negative")
    return v


def _validate_boarding_at(v: Any) -> datetime:
    if not isinstance(v, datetime):
        raise ValueError("INVALID_TIME: boarding_at must be an ISO 8601 timestamp")
    v = ensure_aware(v)
    if not (MIN_YEAR <= v.year <= MAX_YEAR):
        raise ValueError(f"INVALID_TIME: year must be between {MIN_YEAR} and {MAX_YEAR}")
    return v


class TripIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trip_id: str
    account_id: str
    boarding_at: datetime
    base_fare_cents: int

    @field_validator("trip_id")
    @classmethod
    def _tid(cls, v: Any) -> str:
        return _validate_text(v, "trip_id")

    @field_validator("account_id")
    @classmethod
    def _acc(cls, v: Any) -> str:
        return _validate_text(v, "account_id")

    @field_validator("boarding_at")
    @classmethod
    def _time(cls, v: Any) -> datetime:
        return _validate_boarding_at(v)

    @field_validator("base_fare_cents")
    @classmethod
    def _fare(cls, v: Any) -> int:
        return _validate_fare(v)

    def op_day(self) -> date:
        return op_day_of(self.boarding_at)


class CorrectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    boarding_at: datetime
    base_fare_cents: int
    expected_version: int = Field(..., ge=1,
                                  description="乘次当前版本，陈旧版本将被拒绝")

    @field_validator("account_id")
    @classmethod
    def _acc(cls, v: Any) -> str:
        return _validate_text(v, "account_id")

    @field_validator("boarding_at")
    @classmethod
    def _time(cls, v: Any) -> datetime:
        return _validate_boarding_at(v)

    @field_validator("base_fare_cents")
    @classmethod
    def _fare(cls, v: Any) -> int:
        return _validate_fare(v)


class TripView(BaseModel):
    trip_id: str
    account_id: str
    boarding_at: str
    base_fare_cents: int
    status: Literal["active", "cancelled"]
    op_day: date
    version: int


class GroupView(BaseModel):
    group_index: int
    group_fare_cents: int
    first_boarding_at: str
    last_boarding_at: str
    trips: list[TripView]


class DayView(BaseModel):
    account_id: str
    op_day: date
    groups: list[GroupView]
    sum_group_fare_cents: int
    daily_cap_cents: int
    charged_cents: int


class RulePublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effective_day: date
    transfer_window_minutes: int
    daily_cap_cents: int
    # 该生效日当前规则版本：未发布为 0，重发须为最新版本
    expected_version: int = Field(..., ge=0)

    @field_validator("effective_day")
    @classmethod
    def _day(cls, v: Any) -> date:
        if not isinstance(v, date) or isinstance(v, datetime):
            raise ValueError("INVALID_DATE: effective_day must be a date YYYY-MM-DD")
        if not (MIN_YEAR <= v.year <= MAX_YEAR):
            raise ValueError(f"INVALID_DATE: year must be between {MIN_YEAR} and {MAX_YEAR}")
        return v

    @field_validator("transfer_window_minutes")
    @classmethod
    def _window(cls, v: Any) -> int:
        # bool 是 int 的子类，须先排除；只接受整数
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(
                "INVALID_WINDOW: transfer_window_minutes must be a positive integer")
        if v <= 0:
            raise ValueError(
                "INVALID_WINDOW: transfer_window_minutes must be positive")
        return v

    @field_validator("daily_cap_cents")
    @classmethod
    def _cap(cls, v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(
                "INVALID_CAP: daily_cap_cents must be a non-negative integer")
        if v < 0:
            raise ValueError("INVALID_CAP: daily_cap_cents must be non-negative")
        return v


class RuleView(BaseModel):
    """某运营日适用（或已发布）的规则。"""
    effective_day: date | None = None          # null 表示沿用环境默认（尚无已发布规则）
    transfer_window_minutes: int
    daily_cap_cents: int
    version: int = 0                           # 默认规则版本为 0
    published: bool                            # 是否存在已发布记录
    source: Literal["published", "default"]    # 命中发布记录 / 默认配置
    coverage_from: date | None = None          # 覆盖区间起（生效日）
    coverage_to: date | None = None            # 覆盖区间止（下一生效日，开区间）；null 为开放


class RulePublishView(BaseModel):
    idempotent: bool
    rule: RuleView
    coverage_from: date
    coverage_to: date | None
    recalculated_account_days: int             # 覆盖区间内被重算的账户运营日数


class LedgerView(BaseModel):
    seq: int
    account_id: str
    op_day: date
    event_type: str
    trip_id: str | None
    delta_cents: int
    charged_after_cents: int | None
    reason: str
    created_at: str
