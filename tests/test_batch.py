"""清分方补录批量原子结算（POST /trips/batch）测试。"""
import concurrent.futures
from datetime import datetime, timedelta

from app import db


def post_trip(client, tid, acc, ts, fare, status=201):
    r = client.post("/trips", json={
        "trip_id": tid, "account_id": acc,
        "boarding_at": ts, "base_fare_cents": fare})
    assert r.status_code == status, r.text
    return r.json()


def submit_op(tid, acc, ts, fare):
    return {"op": "submit", "trip_id": tid, "account_id": acc,
            "boarding_at": ts, "base_fare_cents": fare}


def correct_op(tid, acc, ts, fare, ver):
    return {"op": "correct", "trip_id": tid, "account_id": acc,
            "boarding_at": ts, "base_fare_cents": fare,
            "expected_version": ver}


def cancel_op(tid, ver):
    return {"op": "cancel", "trip_id": tid, "expected_version": ver}


def batch(client, ops, status=200):
    r = client.post("/trips/batch", json={"operations": ops})
    assert r.status_code == status, r.text
    return r.json()


def day(client, acc, d):
    return client.get(f"/accounts/{acc}/days/{d}").json()


def ledger(client, acc, limit=200):
    return client.get(f"/accounts/{acc}/ledger?limit={limit}").json()["entries"]


D = "2026-10-05"
def T(h): return f"{D}T{h:02d}:00:00+08:00"


# ---------------------------------------------------------- 成功路径

def test_batch_mixed_submit_correct_cancel_atomic(client, account):
    # 预置两笔既有乘次（09:00=500, 12:00=700 不同组，实收 1200）
    post_trip(client, "old1", account, T(9), 500)
    post_trip(client, "old2", account, T(12), 700)

    out = batch(client, [
        submit_op("new1", account, T(8), 900),              # 迟到补录 08:00
        correct_op("old1", account, T(9), 600, 1),         # 更正票价
        cancel_op("old2", 1),                               # 撤销
    ])
    assert out["idempotent"] is False
    assert [it["index"] for it in out["items"]] == [0, 1, 2]
    assert [it["op"] for it in out["items"]] == ["submit", "correct", "cancel"]
    assert all(it["idempotent"] is False for it in out["items"])
    # 逐项乘次状态与版本
    assert out["items"][0]["trip"]["version"] == 1
    assert out["items"][0]["trip"]["status"] == "active"
    assert out["items"][1]["trip"]["version"] == 2
    assert out["items"][2]["trip"]["status"] == "cancelled"

    # 08:00 与 09:00 同组取最高 900；old2 已撤销
    d = day(client, account, D)
    assert len(d["groups"]) == 1
    assert [t["trip_id"] for t in d["groups"][0]["trips"]] == ["new1", "old1"]
    assert d["charged_cents"] == 900

    # 受影响日汇总：新旧并集去重，仅 (account, 10-05) 一次
    assert out["affected_days"] == [
        {"account_id": account, "op_day": D, "charged_cents": 900}]

    # 非幂等生命周期事件按输入顺序追加，金额成对且每日只重算一次
    led = ledger(client, account)
    # 预置两笔产生 seq 1-5（posted/charge × 2）；批量从 seq 6 起
    tail = led[5:]
    assert [(e["event_type"], e["trip_id"]) for e in tail[:3]] == [
        ("trip_posted", "new1"),
        ("trip_corrected", "old1"),
        ("trip_cancelled", "old2")]
    assert tail[3]["event_type"] == "day_reversal" and tail[3]["delta_cents"] == -1200
    assert tail[4]["event_type"] == "day_charge" and tail[4]["delta_cents"] == 900
    assert len(tail) == 5
    # 金额事件为批量结算产生（trip_id 为空，reason 可追溯）
    assert tail[3]["trip_id"] is None and tail[4]["trip_id"] is None
    assert "batch_settlement" in tail[3]["reason"]


def test_batch_aggregates_new_and_old_days_recalc_once(client, account):
    # 跨日更正：旧账户旧日 + 新账户新日都要重算
    post_trip(client, "mov", account, "2026-10-05T08:00:00+08:00", 300)
    post_trip(client, "keep", account, "2026-10-05T20:00:00+08:00", 200)
    out = batch(client, [
        submit_op("nb", "accB", "2026-10-07T08:00:00+08:00", 100),
        correct_op("mov", "accB", "2026-10-07T09:00:00+08:00", 400, 1),
    ])
    # 受影响日：(account,10-05) 因 mov 移走而重算；(accB,10-07) 新建
    assert {(x["account_id"], x["op_day"], x["charged_cents"])
            for x in out["affected_days"]} == {
        (account, D, 200), ("accB", "2026-10-07", 400)}
    # 两个账户日各自只有一对（或零对）金额流水：每日重算一次
    assert day(client, account, D)["charged_cents"] == 200
    db_day = day(client, "accB", "2026-10-07")
    assert db_day["charged_cents"] == 400
    led_b = ledger(client, "accB")
    assert [e["event_type"] for e in led_b] == [
        "trip_posted", "trip_corrected", "day_charge"]


def test_batch_pure_idempotent_replay_changes_nothing(client, account):
    # 三个乘次分别处于“已提交/已更正/已撤销”状态
    post_trip(client, "p0", account, T(7), 100)
    post_trip(client, "p1", account, T(8), 100)
    client.post("/trips/p1/correct", json={
        "account_id": account, "boarding_at": T(8),
        "base_fare_cents": 150, "expected_version": 1})
    post_trip(client, "p2", account, T(12), 200)
    client.post("/trips/p2/cancel?expected_version=1")
    seq_before = [e["seq"] for e in ledger(client, account)]

    # 整批重放：同号同内容提交 / 同内容更正重放 / 重复撤销，全部幂等
    out = batch(client, [
        submit_op("p0", account, T(7), 100),
        correct_op("p1", account, T(8), 150, 2),
        cancel_op("p2", 1),
    ])
    assert out["idempotent"] is True
    assert all(it["idempotent"] for it in out["items"])
    assert out["affected_days"] == []
    # 无任何新流水、无重算
    assert [e["seq"] for e in ledger(client, account)] == seq_before
    assert day(client, account, D)["charged_cents"] == 150


def test_batch_empty_day_still_listed_with_zero(client, account):
    # 撤销当日唯一有效乘次 -> 该日列入受影响日且实收 0
    post_trip(client, "only", account, T(8), 300)
    out = batch(client, [cancel_op("only", 1)])
    assert out["affected_days"] == [
        {"account_id": account, "op_day": D, "charged_cents": 0}]
    assert day(client, account, D)["groups"] == []
    assert day(client, account, D)["charged_cents"] == 0


def test_batch_single_item_matches_single_trip_semantics(client, account):
    # 单元素批次的逐项结果与单笔接口一致（除乘次号外字段相同；状态/版本/幂等一致）
    out = batch(client, [submit_op("s1", account, T(8), 200)])
    single = post_trip(client, "s2", account, T(9), 200)
    bt, st = dict(out["items"][0]["trip"]), dict(single["trip"])
    assert bt.pop("trip_id") == "s1" and st.pop("trip_id") == "s2"
    bt.pop("boarding_at"); st.pop("boarding_at")
    assert bt == st
    assert out["items"][0]["idempotent"] is False
    assert single["idempotent"] is False


def test_batch_up_to_100_items(client, account):
    ops = [submit_op(f"h{i:03d}", account, f"{D}T{6 + i // 30:02d}:"
                     f"{(i % 30) * 2:02d}:00+08:00", 100) for i in range(100)]
    out = batch(client, ops)
    assert len(out["items"]) == 100
    assert len({(x["account_id"], x["op_day"]) for x in out["affected_days"]}) >= 1


# ---------------------------------------------------------- 原子回滚与错误定位

def test_batch_conflict_at_index_rolls_back_entire_batch(client, account):
    post_trip(client, "old1", account, T(9), 500)
    # 先把 old1 更正到 v2
    post_trip(client, "new_seed", account, T(8), 100)
    r0 = client.post("/trips/old1/correct", json={
        "account_id": account, "boarding_at": T(9),
        "base_fare_cents": 600, "expected_version": 1})
    assert r0.status_code == 200

    # 再来一批：第 1 项陈旧版本 -> 整批回滚（含第 0、2 项的新提交）
    before = day(client, account, D)
    r = client.post("/trips/batch", json={"operations": [
        submit_op("aaa", account, T(21), 100),
        correct_op("old1", account, T(9), 660, 1),   # 陈旧版本（当前 v2）
        submit_op("bbb", account, T(22), 100),
    ]})
    assert r.status_code == 409
    assert r.json()["error"] == {
        "code": "VERSION_CONFLICT",
        "message": "expected version 2, got 1",
        "index": 1, "trip_id": "old1"}
    # 同批其余项全部未落库
    assert client.get("/trips/aaa").status_code == 404
    assert client.get("/trips/bbb").status_code == 404
    # 日结、流水与回滚前完全一致
    assert day(client, account, D) == before
    assert client.get("/trips/old1").json()["trip"]["version"] == 2


def test_batch_rollback_does_not_burn_ledger_seq(client, account):
    post_trip(client, "old1", account, T(9), 500)
    post_trip(client, "dead", account, T(7), 100)
    client.post("/trips/dead/cancel?expected_version=1")
    seqs0 = [e["seq"] for e in ledger(client, account)]
    n0 = len(seqs0)

    # 版本冲突（index=1）
    r = client.post("/trips/batch", json={"operations": [
        submit_op("q1", account, T(8), 100),
        cancel_op("old1", 9),
    ]})
    assert r.status_code == 409
    assert r.json()["error"]["index"] == 1
    assert r.json()["error"]["code"] == "VERSION_CONFLICT"
    # 未知乘次：原 404 错误码 + 位置
    r = client.post("/trips/batch", json={"operations": [
        submit_op("q2", account, T(8), 100),
        correct_op("ghost", account, T(8), 100, 1),
    ]})
    assert r.status_code == 404
    assert r.json()["error"] == {
        "code": "UNKNOWN_TRIP",
        "message": "cannot correct unknown trip 'ghost'",
        "index": 1, "trip_id": "ghost"}
    # 已撤销乘次更正：TRIP_NOT_ACTIVE
    r = client.post("/trips/batch", json={"operations": [
        correct_op("dead", account, T(7), 200, 1)]})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TRIP_NOT_ACTIVE"
    assert r.json()["error"]["index"] == 0
    # 同号异内容提交：IDEMPOTENCY_CONFLICT
    r = client.post("/trips/batch", json={"operations": [
        submit_op("old1", account, T(9), 999)]})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert r.json()["error"]["index"] == 0
    # 所有失败批次整体回滚：流水不增、全局序号仍连续无空洞
    seqs1 = [e["seq"] for e in ledger(client, account)]
    assert seqs1 == seqs0 == list(range(1, n0 + 1))
    assert client.get("/trips/q1").status_code == 404
    assert client.get("/trips/q2").status_code == 404


def test_batch_duplicate_trip_id_rejected_with_position(client, account):
    r = client.post("/trips/batch", json={"operations": [
        submit_op("dup", account, T(7), 100),
        cancel_op("x1", 1),
        cancel_op("dup", 1),                        # 与第 0 项同乘次号
    ]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DUPLICATE_TRIP_IN_BATCH"
    assert r.json()["error"]["index"] == 2
    assert r.json()["error"]["trip_id"] == "dup"
    # 整批未执行
    assert client.get("/trips/dup").status_code == 404


def test_batch_idempotency_conflict_does_not_partially_apply(client, account):
    # 第 2 项与库中既有乘次同号异内容：第 0、1 项已在事务内写入但随回滚消失
    post_trip(client, "ex", account, T(9), 100)
    r = client.post("/trips/batch", json={"operations": [
        submit_op("n1", account, T(7), 100),
        submit_op("n2", account, T(8), 100),
        submit_op("ex", account, T(9), 200),       # 异内容冲突
    ]})
    assert r.status_code == 409
    assert r.json()["error"]["index"] == 2
    assert client.get("/trips/n1").status_code == 404
    assert client.get("/trips/n2").status_code == 404
    g = client.get(f"/accounts/{account}/days/{D}").json()
    assert [t["trip_id"] for gr in g["groups"] for t in gr["trips"]] == ["ex"]


# ---------------------------------------------------------- 结构校验

def test_batch_validation_errors_carry_index(client):
    def err(ops, code, index):
        r = client.post("/trips/batch", json={"operations": ops})
        assert r.status_code == 422, r.text
        e = r.json()["error"]
        assert e["code"] == code and e["index"] == index, r.text

    err([submit_op("t", "a", f"{D}T07:00:00", 100)], "INVALID_TIME", 0)
    err([submit_op("ok", "a", T(7), 100),
         submit_op("t", "a", T(8), -5)], "INVALID_FARE", 1)
    err([{"op": "submit", "trip_id": "  ", "account_id": "a",
          "boarding_at": T(7), "base_fare_cents": 1}],
        "INVALID_TRIP_ID", 0)
    err([submit_op("ok", "a", T(7), 100),
         {"op": "frob", "trip_id": "t"}], "INVALID_OP", 1)
    err([submit_op("ok", "a", T(7), 100),
         {"op": "cancel", "trip_id": "t", "expected_version": 0}],
        "INVALID_VERSION", 1)
    err([submit_op("ok", "a", T(7), 100), 7], "INVALID_BATCH", 1)
    err([cancel_op("x", 1), {"op": "cancel", "trip_id": "y",
                             "expected_version": 1, "z": 2}],
        "INVALID_BATCH", 1)


def test_batch_envelope_validation(client):
    # 空批次 / 超过 100 项 / 缺 operations / 多余顶层字段
    assert client.post("/trips/batch", json={"operations": []}).status_code == 422
    r = client.post("/trips/batch",
                    json={"operations": [submit_op("x", "a", T(7), 1)
                                         for _ in range(101)]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_BATCH"
    r = client.post("/trips/batch", json={})
    assert r.status_code == 422
    r = client.post("/trips/batch", json={
        "operations": [submit_op("x", "a", T(7), 1)], "batch_id": "b1"})
    assert r.status_code == 422


# ---------------------------------------------------------- 与规则/单笔的串行

def test_batch_serialized_with_rule_publish(client, account):
    # 预置 10-05 两程间隔 90 分钟
    post_trip(client, "r1", account, "2026-10-05T08:00:00+08:00", 600)
    post_trip(client, "r2", account, "2026-10-05T09:30:00+08:00", 600)

    def publish_60():
        r = client.post("/rules", json={
            "effective_day": "2026-10-05", "transfer_window_minutes": 60,
            "daily_cap_cents": 500, "expected_version": 0})
        assert r.status_code == 200, r.text

    def batch_some():
        r = client.post("/trips/batch", json={"operations": [
            submit_op("r3", account, "2026-10-05T11:00:00+08:00", 100),
            submit_op("r4", account, "2026-10-05T11:30:00+08:00", 100)]})
        assert r.status_code == 200, r.text

    # 规则回溯发布与批量写入共用同一 advisory 锁：只能串行完成
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        f1 = ex.submit(publish_60)
        f2 = ex.submit(batch_some)
        f1.result(); f2.result()

    d = day(client, account, "2026-10-05")
    # 无论提交与发布谁先，最终按 60 分钟窗口重算后的组结构必须自洽
    from datetime import datetime as dt
    for g in d["groups"]:
        ts = [dt.fromisoformat(t["boarding_at"]) for t in g["trips"]]
        assert all(b - a <= timedelta(minutes=60) for a, b in zip(ts, ts[1:]))
    # 规则版本为 1，封顶 500
    rule = client.get("/rules/2026-10-05").json()
    assert rule["version"] == 1 and rule["daily_cap_cents"] == 500
    assert d["daily_cap_cents"] == 500
    assert d["charged_cents"] == min(d["sum_group_fare_cents"], 500)
    # 流水无空洞
    seqs = [e["seq"] for e in ledger(client, account, limit=200)]
    assert seqs == list(range(1, len(seqs) + 1))


def test_reader_never_sees_partial_batch(client, account):
    # 批量与并发读：读接口只可能见到批量前或批量后的完整状态
    post_trip(client, "base", account, T(8), 100)
    stop = {"v": False}

    def reader():
        import time
        seen = set()
        while not stop["v"]:
            d = day(client, account, D)
            n_trips = sum(len(g["trips"]) for g in d["groups"])
            # 批量整体提交：只可能见到 1（批量前）或 5（4 程全部并入同组后），
            # 绝无 2/3/4 的部分结果
            assert n_trips in (1, 5), n_trips
            seen.add(n_trips)
            time.sleep(0.001)
        return seen

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        fut = ex.submit(reader)
        # 四程均在 08:00 的 90 分钟窗口内，批量后与 base 同组 -> 共 5 程
        ops = [submit_op(f"m{i}", account,
                         f"{D}T08:{i * 10:02d}:00+08:00", 100) for i in range(4)]
        r = client.post("/trips/batch", json={"operations": ops})
        assert r.status_code == 200
        # 留出窗口让读者至少观测到一次批量后状态
        import time
        time.sleep(0.05)
        stop["v"] = True
        seen = fut.result()
    assert seen <= {1, 5} and 5 in seen


def test_batch_recalc_uses_per_day_applicable_rule(client, account):
    # 10-05 起窗口 30/封顶 500；10-06 起恢复默认窗口 90/封顶 2000（区间各自适用）
    client.post("/rules", json={
        "effective_day": "2026-10-05", "transfer_window_minutes": 30,
        "daily_cap_cents": 500, "expected_version": 0})
    client.post("/rules", json={
        "effective_day": "2026-10-06", "transfer_window_minutes": 90,
        "daily_cap_cents": 2000, "expected_version": 0})
    out = batch(client, [
        submit_op("w1", account, "2026-10-05T08:00:00+08:00", 300),
        submit_op("w2", account, "2026-10-05T09:00:00+08:00", 300),  # 间隔60拆组
        submit_op("d1", account, "2026-10-06T08:00:00+08:00", 300),
        submit_op("d2", account, "2026-10-06T09:00:00+08:00", 300),  # 间隔60同组
    ])
    days = {(x["op_day"], x["charged_cents"]) for x in out["affected_days"]}
    assert days == {("2026-10-05", 500), ("2026-10-06", 300)}
    d5 = day(client, account, "2026-10-05")
    d6 = day(client, account, "2026-10-06")
    assert len(d5["groups"]) == 2 and d5["charged_cents"] == 500
    assert len(d6["groups"]) == 1 and d6["charged_cents"] == 300
