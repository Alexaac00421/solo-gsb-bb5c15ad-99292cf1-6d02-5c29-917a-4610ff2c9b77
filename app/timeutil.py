"""北京时间与运营日工具。

对外时间一律使用带时区的 ISO 8601 字符串，规范输出为北京时间
（如 2026-10-06T08:00:00+08:00）。库内统一存 UTC。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import settings

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def ensure_aware(dt: datetime) -> datetime:
    """入参必须带时区（拒绝 naive 时间）；统一转为 aware。"""
    if dt.tzinfo is None:
        raise ValueError("INVALID_TIME: boarding_at must include a timezone offset, "
                         "e.g. 2026-10-06T08:00:00+08:00")
    return dt


def op_day_of(dt: datetime) -> date:
    """按北京时间 04:00 界折算运营日。

    边界 04:00 整点归入新的一天：[当日04:00, 次日04:00) 为当日运营日。
    """
    bj = dt.astimezone(BEIJING_TZ)
    boundary = bj.replace(hour=settings.day_boundary_hour,
                          minute=0, second=0, microsecond=0)
    if bj < boundary:
        bj = bj - timedelta(days=1)
    return bj.date()


def op_day_bounds_utc(d: date) -> tuple[datetime, datetime]:
    """运营日 [d 04:00 北京, d+1 04:00 北京) 的 UTC 半开区间。"""
    start_bj = datetime(d.year, d.month, d.day,
                        settings.day_boundary_hour, 0, 0, tzinfo=BEIJING_TZ)
    end_bj = start_bj + timedelta(days=1)
    return start_bj.astimezone(UTC), end_bj.astimezone(UTC)


def to_beijing_iso(dt: datetime) -> str:
    """序列化为北京时间 ISO 8601 字符串。"""
    return dt.astimezone(BEIJING_TZ).replace(microsecond=0).isoformat()
