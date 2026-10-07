"""统一错误类型。"""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """业务错误：code 供客户端程序化判断，message 供阅读。

    details 为可选的结构化附加字段（如批量接口的出错位置 index），
    会并入响应体的 error 对象；单笔接口不携带，错误格式保持不变。
    """

    def __init__(self, status_code: int, code: str, message: str,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def not_found(code: str, message: str,
              details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(404, code, message, details)


def conflict(code: str, message: str,
             details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(409, code, message, details)
