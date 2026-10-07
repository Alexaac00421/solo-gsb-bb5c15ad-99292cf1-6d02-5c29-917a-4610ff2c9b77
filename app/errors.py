"""统一错误类型。"""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """业务错误：code 供客户端程序化判断，message 供阅读。

    批量结算时可携带 index（出错项在输入中的下标，从 0 起）与 trip_id，
    由异常处理器原样放入错误响应体。
    """

    def __init__(self, status_code: int, code: str, message: str,
                 *, index: int | None = None, trip_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.index = index
        self.trip_id = trip_id

    def extra(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.index is not None:
            out["index"] = self.index
        if self.trip_id is not None:
            out["trip_id"] = self.trip_id
        return out


def not_found(code: str, message: str, **kw: Any) -> ApiError:
    return ApiError(404, code, message, **kw)


def conflict(code: str, message: str, **kw: Any) -> ApiError:
    return ApiError(409, code, message, **kw)


def unprocessable(code: str, message: str, **kw: Any) -> ApiError:
    return ApiError(422, code, message, **kw)
