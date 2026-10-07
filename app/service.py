"""清分核心逻辑。

并发正确性：所有写事务先取同一个事务级 advisory 锁，使提交串行化，
从而保证：流水 seq 顺序 == 提交顺序；同账户重算互不踩脚；日结原子可见。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Iterator

import psycopg

from .config import settings
from . import db
from .errors import ApiError, conflict, not_found
from .models import (DayView, GroupView, LedgerView, RulePublishView, RuleView,
                     TripView)
from .timeutil import op_day_of, to_beijing_iso

# 本服务专用 advisory 锁键（随意选取的常量）
ADVISORY_KEY = 0x46415245  # "FARE"

TripRow = dict[str, Any]


# ---------------------------------------------------------------- 序列化

def trip_view(row: TripRow) -> TripView:
    return TripView(
        trip_id=row["trip_id"],
        account_id=row["account_id"],
        boarding_at=to_beijing_iso(row["boarding_at"]),
        base_fare_cents=row["base_fare_cents"],
        status=row["status"],
        op_day=row["op_day"],
        version=row["version"],
    )


# ---------------------------------------------------------------- 费率规则

@contextmanager
def _read_tx() -> Iterator[psycopg.Connection]:
    """读事务：REPEATABLE READ 快照。

    写事务均持同一 advisory 锁串行提交；读侧取一致快照即可保证规则发布
    （改规则 + 重算账单 + 追加流水）过程中，查询永远看不到规则与账单不一致
    的中间态。
    """
    with db.pool.connection() as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            yield conn


def _rule_row_for(conn: psycopg.Connection, op_day: date) -> TripRow | None:
    """不晚于 op_day 的最新一条已发布规则（无则 None，调用方沿用默认配置）。"""
    return conn.execute(
        """SELECT * FROM fare_rules
           WHERE effective_day <= %s
           ORDER BY effective_day DESC LIMIT 1""",
        (op_day,),
    ).fetchone()


def _next_rule_day(conn: psycopg.Connection, effective_day: date) -> date | None:
    row = conn.execute(
        """SELECT MIN(effective_day) AS d FROM fare_rules
           WHERE effective_day > %s""",
        (effective_day,),
    ).fetchone()
    return row["d"] if row and row["d"] is not None else None


def _rule_view(conn: psycopg.Connection, rule: TripRow | None) -> RuleView:
    if rule is None:
        return RuleView(
            effective_day=None,
            transfer_window_minutes=settings.transfer_window_minutes,
            daily_cap_cents=settings.daily_cap_cents,
            version=0, published=False, source="default")
    coverage_to = _next_rule_day(conn, rule["effective_day"])
    return RuleView(
        effective_day=rule["effective_day"],
        transfer_window_minutes=rule["transfer_window_minutes"],
        daily_cap_cents=rule["daily_cap_cents"],
        version=rule["version"], published=True, source="published",
        coverage_from=rule["effective_day"], coverage_to=coverage_to)


def get_rule(op_day: date) -> RuleView:
    """查询指定运营日适用规则（含覆盖区间）。"""
    with _read_tx() as conn:
        rule = _rule_row_for(conn, op_day)
        view = _rule_view(conn, rule)
        if rule is None:
            # 默认规则覆盖 (-∞, 首条生效日)；针对查询日给出其所在默认区间
            first = conn.execute(
                "SELECT MIN(effective_day) AS d FROM fare_rules"
            ).fetchone()
            view.coverage_to = first["d"] if first and first["d"] is not None else None
    return view


def list_rules() -> list[RuleView]:
    """列出全部已发布规则（按生效日升序）。"""
    with _read_tx() as conn:
        rows = conn.execute(
            "SELECT * FROM fare_rules ORDER BY effective_day ASC"
        ).fetchall()
        return [_rule_view(conn, r) for r in rows]


def publish_rule(effective_day: date, window_minutes: int, cap_cents: int,
                 expected_version: int) -> tuple[RulePublishView, bool]:
    """发布（或重发）某运营日生效的规则。返回 (响应, 是否幂等重放)。

    与乘次提交/更正/撤销共用同一事务级 advisory 锁，故规则重算与账单写入
    严格串行；同一事务内重算覆盖区间内已有乘次的账户运营日。
    """
    with db.pool.connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_KEY,))
            existing = conn.execute(
                "SELECT * FROM fare_rules WHERE effective_day = %s FOR UPDATE",
                (effective_day,),
            ).fetchone()

            if existing is None and expected_version != 0:
                raise conflict(
                    "RULE_VERSION_CONFLICT",
                    f"no rule published on {effective_day.isoformat()}; "
                    f"expected version 0, got {expected_version}")
            if existing is not None and existing["version"] != expected_version:
                raise conflict(
                    "RULE_VERSION_CONFLICT",
                    f"rule {effective_day.isoformat()} expected version "
                    f"{existing['version']}, got {expected_version}")

            if (existing is not None
                    and existing["transfer_window_minutes"] == window_minutes
                    and existing["daily_cap_cents"] == cap_cents):
                # 同内容同版本重发：幂等，不改数据、不重算、不产生流水
                view = _rule_view(conn, existing)
                return (RulePublishView(
                            idempotent=True, rule=view,
                            coverage_from=effective_day,
                            coverage_to=view.coverage_to,
                            recalculated_account_days=0),
                        True)

            rule = conn.execute(
                """INSERT INTO fare_rules
                       (effective_day, transfer_window_minutes,
                        daily_cap_cents, version, published_at)
                   VALUES (%s, %s, %s, %s, now())
                   ON CONFLICT (effective_day) DO UPDATE
                   SET transfer_window_minutes = EXCLUDED.transfer_window_minutes,
                       daily_cap_cents = EXCLUDED.daily_cap_cents,
                       version = fare_rules.version + 1,
                       published_at = now()
                   RETURNING *""",
                (effective_day, window_minutes, cap_cents,
                 expected_version + 1),
            ).fetchone()
            coverage_to = _next_rule_day(conn, rule["effective_day"])

            # 覆盖区间 [effective_day, coverage_to) 内已有乘次的
            # 账户运营日全部重算（排序仅为流水序号确定，无业务含义）。
            affected_rows = conn.execute(
                """SELECT DISTINCT account_id, op_day FROM trips
                   WHERE status = 'active'
                     AND op_day >= %s
                     AND (CAST(%s AS date) IS NULL OR op_day < %s)
                   ORDER BY account_id, op_day""",
                (effective_day, coverage_to, coverage_to),
            ).fetchall()
            for r in affected_rows:
                _recalc_day(
                    conn, r["account_id"], r["op_day"],
                    reason=f"rule_published:{effective_day.isoformat()}",
                    trip_id=None)
            view = _rule_view(conn, rule)
            return (RulePublishView(
                        idempotent=False, rule=view,
                        coverage_from=effective_day,
                        coverage_to=coverage_to,
                        recalculated_account_days=len(affected_rows)),
                    False)


# ---------------------------------------------------------------- 读操作

def get_trip(trip_id: str) -> TripRow:
    with db.pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM trips WHERE trip_id = %s", (trip_id,)
        ).fetchone()
    if row is None:
        raise not_found("UNKNOWN_TRIP", f"trip {trip_id!r} does not exist")
    return row


def get_day(account_id: str, op_day: date) -> DayView:
    with _read_tx() as conn:
        # 适用规则与账单取同一快照，保证发布事务提交前/后的视图各自一致
        rule = _rule_row_for(conn, op_day)
        daily_cap = (rule["daily_cap_cents"] if rule is not None
                     else settings.daily_cap_cents)

        groups_raw = conn.execute(
            """SELECT group_id, group_index, group_fare_cents,
                      first_boarding_at, last_boarding_at
               FROM transfer_groups
               WHERE account_id = %s AND op_day = %s
               ORDER BY group_index""",
            (account_id, op_day),
        ).fetchall()

        groups: list[GroupView] = []
        for g in groups_raw:
            members = conn.execute(
                """SELECT t.* FROM group_members m
                   JOIN trips t ON t.trip_id = m.trip_id
                   WHERE m.group_id = %s ORDER BY m.seq""",
                (g["group_id"],),
            ).fetchall()
            groups.append(GroupView(
                group_index=g["group_index"],
                group_fare_cents=g["group_fare_cents"],
                first_boarding_at=to_beijing_iso(g["first_boarding_at"]),
                last_boarding_at=to_beijing_iso(g["last_boarding_at"]),
                trips=[trip_view(t) for t in members],
            ))

        settlement = conn.execute(
            """SELECT sum_group_fare_cents, charged_cents FROM day_settlements
               WHERE account_id = %s AND op_day = %s""",
            (account_id, op_day),
        ).fetchone()

    if settlement is None:
        sum_fare, charged = 0, 0
    else:
        sum_fare, charged = settlement["sum_group_fare_cents"], settlement["charged_cents"]

    return DayView(
        account_id=account_id,
        op_day=op_day,
        groups=groups,
        sum_group_fare_cents=sum_fare,
        daily_cap_cents=daily_cap,
        charged_cents=charged,
    )


def list_ledger(
    account_id: str,
    cursor: int,
    limit: int,
    op_day: date | None = None,
) -> tuple[list[LedgerView], int | None]:
    """游标后流水（keyset pagination，seq > cursor）。"""
    sql = """SELECT * FROM ledger_entries
             WHERE account_id = %s AND seq > %s"""
    params: list[Any] = [account_id, cursor]
    if op_day is not None:
        sql += " AND op_day = %s"
        params.append(op_day)
    sql += " ORDER BY seq ASC LIMIT %s"
    params.append(limit + 1)

    with db.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    has_next = len(rows) > limit
    rows = rows[:limit]
    entries = [LedgerView(
        seq=r["seq"],
        account_id=r["account_id"],
        op_day=r["op_day"],
        event_type=r["event_type"],
        trip_id=r["trip_id"],
        delta_cents=r["delta_cents"],
        charged_after_cents=r["charged_after_cents"],
        reason=r["reason"],
        created_at=to_beijing_iso(r["created_at"]),
    ) for r in rows]
    next_cursor = rows[-1]["seq"] if has_next and rows else None
    return entries, next_cursor


# ---------------------------------------------------------------- 写操作

def _append_ledger(
    conn: psycopg.Connection,
    *,
    account_id: str,
    op_day: date,
    event_type: str,
    delta: int,
    charged_after: int | None,
    reason: str,
    trip_id: str | None = None,
) -> None:
    # 写事务整体持有串行 advisory 锁：MAX(seq)+1 即下一个提交序号，无空洞
    next_seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM ledger_entries"
    ).fetchone()["n"]
    conn.execute(
        """INSERT INTO ledger_entries
               (seq, account_id, op_day, event_type, trip_id,
                delta_cents, charged_after_cents, reason)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (next_seq, account_id, op_day, event_type, trip_id,
         delta, charged_after, reason),
    )


def _build_groups(trips: Iterable[TripRow], window_minutes: int) -> list[list[TripRow]]:
    """按 (上车时刻, 乘次号) 排序后贪心归组：相邻间隔 <= 窗口分钟即同组。

    trips 已由 SQL 按 (boarding_at, trip_id) 排序；窗口取该运营日适用规则。
    """
    window = timedelta(minutes=window_minutes)
    groups: list[list[TripRow]] = []
    prev_time: datetime | None = None
    for t in trips:
        if groups and t["boarding_at"] - prev_time <= window:
            groups[-1].append(t)
        else:
            groups.append([t])
        prev_time = t["boarding_at"]
    return groups


def _recalc_day(conn: psycopg.Connection, account_id: str, op_day: date,
                reason: str, trip_id: str | None) -> int:
    """重算单个账户单个运营日：重写换乘组/日结，实收变化时追加冲销+新收流水。

    换乘时限与封顶值取该运营日适用的已发布规则（无记录时沿用环境默认）。
    返回重算后的实收金额。
    """
    rule = _rule_row_for(conn, op_day)
    window_minutes = (rule["transfer_window_minutes"] if rule is not None
                      else settings.transfer_window_minutes)
    daily_cap = (rule["daily_cap_cents"] if rule is not None
                 else settings.daily_cap_cents)

    trips = conn.execute(
        """SELECT * FROM trips
           WHERE account_id = %s AND op_day = %s AND status = 'active'
           ORDER BY boarding_at ASC, trip_id ASC
           FOR UPDATE""",
        (account_id, op_day),
    ).fetchall()

    # 旧实收（行锁，避免与其它事务的重算交错；写事务整体已持有 advisory 锁）
    old = conn.execute(
        """SELECT sum_group_fare_cents, charged_cents FROM day_settlements
           WHERE account_id = %s AND op_day = %s FOR UPDATE""",
        (account_id, op_day),
    ).fetchone()
    old_charged = old["charged_cents"] if old else 0

    # 派生组整组重写，分两步，顺序无关地消除跨日/跨账户更正产生的错挂成员：
    # 1) 先删除所有“与其乘次当前归属不一致”的成员行
    #    （乘次被更正到另一天/另一账户、或已撤销时，旧组下的成员即错挂）；
    # 2) 再级联删除当日全部组。两端先重算哪一端都不会再撞主键。
    conn.execute(
        """DELETE FROM group_members gm
           USING trips t
           WHERE gm.trip_id = t.trip_id
             AND (t.status = 'cancelled'
                  OR NOT EXISTS (
                      SELECT 1 FROM transfer_groups g
                      WHERE g.group_id = gm.group_id
                        AND g.account_id = t.account_id
                        AND g.op_day = t.op_day))""",
    )
    conn.execute(
        "DELETE FROM transfer_groups WHERE account_id = %s AND op_day = %s",
        (account_id, op_day),
    )

    sum_group_fare = 0
    if trips:
        for idx, members in enumerate(_build_groups(trips, window_minutes)):
            fares = [m["base_fare_cents"] for m in members]
            group_fare = max(fares)
            sum_group_fare += group_fare
            row = conn.execute(
                """INSERT INTO transfer_groups
                       (account_id, op_day, group_index, group_fare_cents,
                        first_boarding_at, last_boarding_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING group_id""",
                (account_id, op_day, idx, group_fare,
                 members[0]["boarding_at"], members[-1]["boarding_at"]),
            ).fetchone()
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO group_members (group_id, trip_id, seq) VALUES (%s, %s, %s)",
                    [(row["group_id"], m["trip_id"], seq) for seq, m in enumerate(members)],
                )

    new_charged = min(sum_group_fare, daily_cap)

    # 日结 upsert（当日无有效乘次则删除日结）
    if not trips:
        conn.execute(
            "DELETE FROM day_settlements WHERE account_id = %s AND op_day = %s",
            (account_id, op_day),
        )
    else:
        conn.execute(
            """INSERT INTO day_settlements
                   (account_id, op_day, sum_group_fare_cents, charged_cents, recalculated_at)
               VALUES (%s, %s, %s, %s, now())
               ON CONFLICT (account_id, op_day) DO UPDATE
               SET sum_group_fare_cents = EXCLUDED.sum_group_fare_cents,
                   charged_cents = EXCLUDED.charged_cents,
                   recalculated_at = now()""",
            (account_id, op_day, sum_group_fare, new_charged),
        )

    # 实收变化时才冲销旧日金额并记入新金额（金额不变不产生金额流水）
    if new_charged != old_charged:
        if old_charged:
            _append_ledger(conn, account_id=account_id, op_day=op_day,
                           event_type="day_reversal", delta=-old_charged,
                           charged_after=0,
                           reason=f"reverse prior charged amount before {reason}",
                           trip_id=trip_id)
        if new_charged:
            _append_ledger(conn, account_id=account_id, op_day=op_day,
                           event_type="day_charge", delta=new_charged,
                           charged_after=new_charged,
                           reason=f"charge recalculated amount after {reason}",
                           trip_id=trip_id)
    return new_charged


def _same_content(existing: TripRow, account_id: str,
                  boarding_at: datetime, fare: int) -> bool:
    return (existing["account_id"] == account_id
            and existing["boarding_at"] == boarding_at
            and existing["base_fare_cents"] == fare)


def submit_trip(trip_id: str, account_id: str,
                boarding_at: datetime, fare: int) -> tuple[TripRow, bool]:
    """提交乘车记录。返回 (记录, 是否幂等重传)。"""
    op_day = op_day_of(boarding_at)
    with db.pool.connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_KEY,))
            existing = conn.execute(
                "SELECT * FROM trips WHERE trip_id = %s FOR UPDATE",
                (trip_id,),
            ).fetchone()

            if existing is not None:
                if _same_content(existing, account_id, boarding_at, fare):
                    return existing, True  # 同号同内容：幂等，直接回当前状态
                raise conflict(
                    "IDEMPOTENCY_CONFLICT",
                    f"trip {trip_id!r} already exists with different content; "
                    "use POST /trips/{trip_id}/correct to amend it",
                )

            row = conn.execute(
                """INSERT INTO trips
                       (trip_id, account_id, boarding_at, base_fare_cents,
                        status, op_day, version)
                   VALUES (%s, %s, %s, %s, 'active', %s, 1)
                   RETURNING *""",
                (trip_id, account_id, boarding_at, fare, op_day),
            ).fetchone()
            _append_ledger(conn, account_id=account_id, op_day=op_day,
                           event_type="trip_posted", delta=0,
                           charged_after=None,
                           reason="trip submitted", trip_id=trip_id)
            _recalc_day(conn, account_id, op_day,
                        reason=f"trip_posted:{trip_id}", trip_id=trip_id)
            return row, False


def correct_trip(trip_id: str, account_id: str,
                 boarding_at: datetime, fare: int,
                 expected_version: int) -> tuple[TripRow, bool]:
    """更正乘车记录（凭乘次号 + 当前版本）。返回 (记录, 是否幂等重放)。"""
    new_day = op_day_of(boarding_at)
    with db.pool.connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_KEY,))
            existing = conn.execute(
                "SELECT * FROM trips WHERE trip_id = %s FOR UPDATE",
                (trip_id,),
            ).fetchone()
            if existing is None:
                raise not_found("UNKNOWN_TRIP",
                                f"cannot correct unknown trip {trip_id!r}")
            if existing["status"] == "cancelled":
                raise conflict("TRIP_NOT_ACTIVE",
                               f"trip {trip_id!r} is cancelled and cannot be corrected")
            if existing["version"] != expected_version:
                raise conflict("VERSION_CONFLICT",
                               f"expected version {existing['version']}, "
                               f"got {expected_version}")

            old_day = existing["op_day"]
            if _same_content(existing, account_id, boarding_at, fare):
                return existing, True  # 内容一致：幂等重放

            row = conn.execute(
                """UPDATE trips SET account_id = %s, boarding_at = %s,
                       base_fare_cents = %s, op_day = %s,
                       version = version + 1, updated_at = now()
                   WHERE trip_id = %s RETURNING *""",
                (account_id, boarding_at, fare, new_day, trip_id),
            ).fetchone()
            _append_ledger(conn, account_id=account_id, op_day=new_day,
                           event_type="trip_corrected", delta=0,
                           charged_after=None,
                           reason=(f"trip corrected v{existing['version']}->"
                                   f"v{row['version']}"),
                           trip_id=trip_id)
            # 受影响的（账户 × 运营日）全部重算；账户变更时旧账户旧日也要冲销重算
            affected = {(existing["account_id"], old_day), (account_id, new_day)}
            for acc, day in affected:
                _recalc_day(conn, acc, day,
                            reason=f"trip_corrected:{trip_id}", trip_id=trip_id)
            return row, False


def cancel_trip(trip_id: str, expected_version: int) -> tuple[TripRow, bool]:
    """撤销乘次（凭乘次号 + 当前版本）。撤销不推进 version；返回 (记录, 是否幂等重放)。"""
    with db.pool.connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_KEY,))
            existing = conn.execute(
                "SELECT * FROM trips WHERE trip_id = %s FOR UPDATE",
                (trip_id,),
            ).fetchone()
            if existing is None:
                raise not_found("UNKNOWN_TRIP",
                                f"cannot cancel unknown trip {trip_id!r}")
            if existing["version"] != expected_version:
                raise conflict("VERSION_CONFLICT",
                               f"expected version {existing['version']}, "
                               f"got {expected_version}")
            if existing["status"] == "cancelled":
                return existing, True  # 同版本重复撤销：幂等

            row = conn.execute(
                """UPDATE trips SET status = 'cancelled', updated_at = now()
                   WHERE trip_id = %s RETURNING *""",
                (trip_id,),
            ).fetchone()
            _append_ledger(conn, account_id=row["account_id"], op_day=row["op_day"],
                           event_type="trip_cancelled", delta=0,
                           charged_after=None, reason="trip cancelled",
                           trip_id=trip_id)
            _recalc_day(conn, row["account_id"], row["op_day"],
                        reason=f"trip_cancelled:{trip_id}", trip_id=trip_id)
            return row, False

