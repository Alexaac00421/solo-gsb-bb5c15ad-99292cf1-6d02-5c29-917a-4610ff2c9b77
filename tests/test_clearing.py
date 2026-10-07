"""清分业务规则测试。"""
import concurrent.futures

import pytest

from app import db


def post(client, tid, acc, ts, fare, status=201):
    r = client.post("/trips", json={
        "trip_id": tid, "account_id": acc,
        "boarding_at": ts, "base_fare_cents": fare})
    assert r.status_code == status, r.text
    return r.json()


# ---------------------------------------------------------- 分组

def test_transfer_grouping_window(client, account):
    # 08:00 / 09:30 间隔 90 分钟 -> 同组；11:00 与 09:30 间隔 91 -> 新组
    post(client, "t1", account, "2026-10-06T08:00:00+08:00", 200)
    post(client, "t2", account, "2026-10-06T09:30:00+08:00", 500)
    post(client, "t3", account, "2026-10-06T11:01:00+08:00", 100)

    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert len(d["groups"]) == 2
    assert [t["trip_id"] for t in d["groups"][0]["trips"]] == ["t1", "t2"]
    # 组费取组内最高票价
    assert d["groups"][0]["group_fare_cents"] == 500
    assert d["groups"][1]["group_fare_cents"] == 100
    assert d["sum_group_fare_cents"] == 600
    assert d["charged_cents"] == 600


def test_window_boundary_inclusive(client, account):
    post(client, "a", account, "2026-10-06T08:00:00+08:00", 100)
    post(client, "b", account, "2026-10-06T09:30:00+08:00", 100)
    post(client, "c", account, "2026-10-06T11:00:00+08:00", 100)
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    # 恰好 90 分钟归同组：三程在同一条链式换乘中
    assert len(d["groups"]) == 1


def test_chained_groups_not_compared_to_first(client, account):
    # 链式：相邻 <=90 即同组，即使首尾跨度很大
    post(client, "a", account, "2026-10-06T08:00:00+08:00", 100)
    post(client, "b", account, "2026-10-06T09:00:00+08:00", 100)
    post(client, "c", account, "2026-10-06T10:00:00+08:00", 100)
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert len(d["groups"]) == 1


def test_day_boundary_at_4am(client, account):
    # 03:59 计入前一运营日 10-05；04:00 计入 10-06，不跨日合组
    post(client, "late", account, "2026-10-06T03:59:00+08:00", 100)
    post(client, "early", account, "2026-10-06T04:00:00+08:00", 100)
    d5 = client.get(f"/accounts/{account}/days/2026-10-05").json()
    d6 = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert len(d5["groups"]) == 1 and d5["groups"][0]["trips"][0]["trip_id"] == "late"
    assert len(d6["groups"]) == 1 and d6["groups"][0]["trips"][0]["trip_id"] == "early"
    assert d5["charged_cents"] == 100 and d6["charged_cents"] == 100


def test_ordering_by_time_then_trip_id(client, account):
    # 同一时刻按乘次号排序
    for tid, fare in [("z9", 300), ("a1", 700), ("m5", 100)]:
        post(client, tid, account, "2026-10-06T08:00:00+08:00", fare)
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert [t["trip_id"] for t in d["groups"][0]["trips"]] == ["a1", "m5", "z9"]
    assert d["groups"][0]["group_fare_cents"] == 700


# ---------------------------------------------------------- 封顶

def test_daily_cap(client, account):
    # 3 个独立组，每组 800，合计 2400 -> 封顶 2000
    post(client, "a", account, "2026-10-06T06:00:00+08:00", 800)
    post(client, "b", account, "2026-10-06T10:00:00+08:00", 800)
    post(client, "c", account, "2026-10-06T14:00:00+08:00", 800)
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert d["sum_group_fare_cents"] == 2400
    assert d["charged_cents"] == 2000


def test_cap_does_not_span_days(client, account):
    post(client, "a", account, "2026-10-06T03:00:00+08:00", 1500)  # op_day 10-05
    post(client, "b", account, "2026-10-06T12:00:00+08:00", 1500)  # op_day 10-06
    d5 = client.get(f"/accounts/{account}/days/2026-10-05").json()
    d6 = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert d5["charged_cents"] == 1500 and d6["charged_cents"] == 1500


# ---------------------------------------------------------- 幂等/冲突

def test_idempotent_same_content(client, account):
    body = {"trip_id": "x1", "account_id": account,
            "boarding_at": "2026-10-06T08:00:00+08:00", "base_fare_cents": 200}
    r1 = client.post("/trips", json=body)
    r2 = client.post("/trips", json=body)
    assert r1.json()["idempotent"] is False
    assert r2.status_code == 201 and r2.json()["idempotent"] is True
    assert r1.json()["trip"]["version"] == r2.json()["trip"]["version"] == 1
    # 幂等重传不产生重复流水
    led = client.get(f"/accounts/{account}/ledger?limit=200").json()
    assert sum(e["event_type"] == "trip_posted" for e in led["entries"]) == 1


def test_conflict_different_content(client, account):
    post(client, "x2", account, "2026-10-06T08:00:00+08:00", 200)
    r = client.post("/trips", json={"trip_id": "x2", "account_id": account,
                                    "boarding_at": "2026-10-06T08:05:00+08:00",
                                    "base_fare_cents": 200})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# ---------------------------------------------------------- 更正/撤销

def test_correction_version_flow_and_recalc(client, account):
    post(client, "c1", account, "2026-10-06T08:00:00+08:00", 200)
    post(client, "c2", account, "2026-10-06T09:00:00+08:00", 500)
    # 初始同组，组费 500
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert len(d["groups"]) == 1 and d["charged_cents"] == 500

    # 更正 c1 票价为 800 -> 组费变 800
    r = client.post("/trips/c1/correct", json={
        "account_id": account, "boarding_at": "2026-10-06T08:00:00+08:00",
        "base_fare_cents": 800, "expected_version": 1})
    assert r.status_code == 200 and r.json()["trip"]["version"] == 2

    r = client.post("/trips/c1/correct", json={
        "account_id": account, "boarding_at": "2026-10-06T08:00:00+08:00",
        "base_fare_cents": 800, "expected_version": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "VERSION_CONFLICT"

    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert d["groups"][0]["group_fare_cents"] == 800 and d["charged_cents"] == 800

    led = client.get(f"/accounts/{account}/ledger?limit=200").json()["entries"]
    types = [e["event_type"] for e in led]
    # 实收 500 -> 800：冲销 500，再记 800
    assert "day_reversal" in types and "day_charge" in types
    rev = [e for e in led if e["event_type"] == "day_reversal"][-1]
    chg = [e for e in led if e["event_type"] == "day_charge"][-1]
    assert rev["delta_cents"] == -500 and chg["delta_cents"] == 800


def test_cross_day_correction_recalcs_both_days(client, account):
    post(client, "d1", account, "2026-10-06T08:00:00+08:00", 300)  # day 10-06
    # 改到 10-07 凌晨 03:00（仍属运营日 10-06，先不动），再改到 10-07 05:00（运营日 10-07）
    client.post("/trips/d1/correct", json={
        "account_id": account, "boarding_at": "2026-10-07T05:00:00+08:00",
        "base_fare_cents": 300, "expected_version": 1})
    d6 = client.get(f"/accounts/{account}/days/2026-10-06").json()
    d7 = client.get(f"/accounts/{account}/days/2026-10-07").json()
    assert len(d6["groups"]) == 0 and d6["charged_cents"] == 0
    assert len(d7["groups"]) == 1 and d7["charged_cents"] == 300

    led = client.get(f"/accounts/{account}/ledger?limit=200").json()["entries"]
    days = {(e["op_day"], e["event_type"]) for e in led}
    assert ("2026-10-06", "day_reversal") in days
    assert ("2026-10-07", "day_charge") in days


def test_cross_account_correction_recalcs_both(client):
    post(client, "xacc", "accOLD", "2026-10-06T08:00:00+08:00", 300)
    r = client.post("/trips/xacc/correct", json={
        "account_id": "accNEW", "boarding_at": "2026-10-06T08:00:00+08:00",
        "base_fare_cents": 300, "expected_version": 1})
    assert r.status_code == 200
    old = client.get("/accounts/accOLD/days/2026-10-06").json()
    new = client.get("/accounts/accNEW/days/2026-10-06").json()
    assert old["charged_cents"] == 0 and len(old["groups"]) == 0
    assert new["charged_cents"] == 300 and len(new["groups"]) == 1
    # 重复同样的更正为幂等
    r2 = client.post("/trips/xacc/correct", json={
        "account_id": "accNEW", "boarding_at": "2026-10-06T08:00:00+08:00",
        "base_fare_cents": 300, "expected_version": 2})
    assert r2.json()["idempotent"] is True


def test_late_arrival_triggers_recalc(client, account):
    # 先提交 09:00（独立组 500），迟到补录 08:00（间隔 60）-> 合并成一组取最高 500，金额不变
    post(client, "late2", account, "2026-10-06T09:00:00+08:00", 500)
    post(client, "early2", account, "2026-10-06T08:00:00+08:00", 900)
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert len(d["groups"]) == 1
    assert d["groups"][0]["group_fare_cents"] == 900
    assert d["charged_cents"] == 900


def test_no_money_ledger_when_charged_unchanged(client, account):
    # 组费本就是 900；迟到补录一张低价票，组归属变化但实收仍 900 -> 不新增冲销/新收
    post(client, "hi", account, "2026-10-06T09:00:00+08:00", 900)
    post(client, "lo", account, "2026-10-06T08:00:00+08:00", 100)
    led = client.get(f"/accounts/{account}/ledger?limit=200").json()["entries"]
    # 仅首次提交产生一笔 day_charge(+900)，第二次迟到补录无任何金额流水
    money = [e for e in led if e["event_type"] in ("day_reversal", "day_charge")]
    assert len(money) == 1
    assert money[0]["event_type"] == "day_charge" and money[0]["delta_cents"] == 900


def test_cancel_trip(client, account):
    post(client, "k1", account, "2026-10-06T08:00:00+08:00", 400)
    post(client, "k2", account, "2026-10-06T12:00:00+08:00", 600)
    r = client.post("/trips/k1/cancel?expected_version=1")
    assert r.status_code == 200 and r.json()["trip"]["status"] == "cancelled"
    # 重复撤销（同版本）幂等
    r2 = client.post("/trips/k1/cancel?expected_version=1")
    assert r2.json()["idempotent"] is True
    # 陈旧版本撤销
    assert client.post("/trips/k2/cancel?expected_version=9").status_code == 409
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert len(d["groups"]) == 1 and d["charged_cents"] == 600


def test_unknown_trip_errors(client):
    assert client.get("/trips/nope").status_code == 404
    r = client.post("/trips/nope/cancel?expected_version=1")
    assert r.status_code == 404 and r.json()["error"]["code"] == "UNKNOWN_TRIP"
    r = client.post("/trips/nope/correct", json={
        "account_id": "a", "boarding_at": "2026-10-06T08:00:00+08:00",
        "base_fare_cents": 1, "expected_version": 1})
    assert r.status_code == 404 and r.json()["error"]["code"] == "UNKNOWN_TRIP"


# ---------------------------------------------------------- 非法输入

def test_invalid_inputs(client, account):
    base = {"trip_id": "v1", "account_id": account,
            "boarding_at": "2026-10-06T08:00:00+08:00", "base_fare_cents": 1}

    bad_time = dict(base, boarding_at="2026-10-06T08:00:00")  # naive
    assert client.post("/trips", json=bad_time).status_code == 422
    bad_time2 = dict(base, boarding_at="not-a-time")
    assert client.post("/trips", json=bad_time2).json()["error"]["code"] == "INVALID_TIME"
    bad_fare = dict(base, base_fare_cents=-1)
    r = client.post("/trips", json=bad_fare)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_FARE"
    bad_fare2 = dict(base, base_fare_cents=1.5)
    assert client.post("/trips", json=bad_fare2).status_code == 422
    empty = dict(base, account_id="  ")
    assert client.post("/trips", json=empty).status_code == 422


# ---------------------------------------------------------- 流水

def test_ledger_keyset_no_gap_no_dup_no_rewrite(client, account):
    for i in range(7):
        post(client, f"p{i}", account, f"2026-10-06T{8+i:02d}:00:00+08:00", 100)

    # 翻页（每页 5 条）不重不漏，seq 连续无空洞
    cursor, seen = 0, []
    for _ in range(10):
        page = client.get(
            f"/accounts/{account}/ledger?after_seq={cursor}&limit=5").json()
        batch = page["entries"]
        assert len(batch) <= 5
        seen.extend(e["seq"] for e in batch)
        if page["next_cursor"] is None:
            break
        cursor = page["next_cursor"]
    assert seen == sorted(seen)
    assert len(seen) == len(set(seen))
    assert seen == list(range(1, len(seen) + 1))  # 无空洞

    # 已返回流水不可改写
    with db.pool.connection() as conn:
        with pytest.raises(Exception):
            conn.execute("UPDATE ledger_entries SET reason = 'x' WHERE seq = 1")
        with pytest.raises(Exception):
            conn.execute("DELETE FROM ledger_entries WHERE seq = 1")


def test_ledger_seq_no_holes_after_rollback(client, account):
    post(client, "g1", account, "2026-10-06T08:00:00+08:00", 100)
    # 触发若干回滚（冲突请求不应烧掉序号）
    for ts, fare in [("2026-10-06T09:00:00+08:00", 200)]:
        r = client.post("/trips", json={"trip_id": "g1", "account_id": account,
                                        "boarding_at": ts, "base_fare_cents": fare})
        assert r.status_code == 409
    post(client, "g2", account, "2026-10-06T10:00:00+08:00", 300)
    seqs = [e["seq"] for e in
            client.get(f"/accounts/{account}/ledger?limit=200").json()["entries"]]
    assert seqs == list(range(1, len(seqs) + 1))


def test_ledger_scoped_by_account(client):
    post(client, "w1", "accA", "2026-10-06T08:00:00+08:00", 100)
    post(client, "w2", "accB", "2026-10-06T08:00:00+08:00", 100)
    a = client.get("/accounts/accA/ledger?limit=200").json()["entries"]
    b = client.get("/accounts/accB/ledger?limit=200").json()["entries"]
    assert {e["account_id"] for e in a} == {"accA"}
    assert {e["account_id"] for e in b} == {"accB"}


# ---------------------------------------------------------- 并发

def test_concurrent_submits_serialized(client, account):
    # 每个乘次同内容并发提交两次：恰好一次新建，另一次幂等
    ids = [f"s{i}" for i in range(10)]

    def submit(tid):
        return client.post("/trips", json={
            "trip_id": tid, "account_id": account,
            "boarding_at": f"2026-10-06T{8 + (int(tid[1:]) % 10):02d}:00:00+08:00",
            "base_fare_cents": 100})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(submit, ids * 2))
    created = [r for r in results if not r.json()["idempotent"]]
    assert len(created) == 10
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    total_trips = sum(len(g["trips"]) for g in d["groups"])
    assert total_trips == 10
