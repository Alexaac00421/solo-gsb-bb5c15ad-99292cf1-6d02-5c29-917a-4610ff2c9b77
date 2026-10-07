"""统一错误类型。"""
from __future__ import annotations


class ApiError(Exception):
    """业务错误：code 供客户端程序化判断，message 供阅读。"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def not_found(code: str, message: str) -> ApiError:
    return ApiError(404, code, message)


def conflict(code: str, message: str) -> ApiError:
    return ApiError(409, code, message)
