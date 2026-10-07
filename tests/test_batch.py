"""清分方补录批量原子结算（POST /batch/trips）测试。"""
import concurrent.futures

from app import db


BATCH_URL = "/batch/trips"


def batch(client, items, status=200):
    r = client.post(BATCH_URL, json={"items": items})
    assert r.status_code == status, r.text
    return r.json()


def submit_item(tid, acc, ts, fare):
    return {"op": "submit", "trip_id": tid, "account_id": acc,
            "boarding_at": ts, "base_fare_cents": fare}


def correct_item(tid, acc, ts, fare, ver):
    return {"op": "correct", "trip_id": tid, "account_id": acc,
            "boarding_at": ts, "base_fare_cents": fare,
            "expected_version": ver}


def cancel_item(tid, ver):
    return {"op": "cancel", "trip_id": tid, "expected_version": ver}


def ledger(client, acc, limit=200):
    return client.get(f"/accounts/{acc}/ledger?limit={limit}").json()["entries"]


# ---------------------------------------------------------- 成功路径

def test_batch_submit_mixed_ops_success(client, account):
    # 批内混合新建 + 对既有乘次的更正/撤销（乘次号在批内不重复）
    batch(client, [submit_item("b1", account,
                               "2026-10-06T08:00:00+08:00", 200),
                   submit_item("b2", account,
                               "2026-10-06T09:00:00+08:00", 500)])
    out = batch(client, [
        # 组费取最高 500；更正 b1 到 800 -> 800；撤销 b2 -> 仅 b1 = 800
        correct_item("b1", account, "2026-10-06T08:00:00+08:00", 800, 1),
        cancel_item("b2", 1),
    ])
    assert out["idempotent"] is False
    assert [r["index"] for r in out["results"]] == [0, 1]
    assert [r["idempotent"] for r in out["results"]] == [False, False]
    # 逐项最终状态
    by_id = {r["trip_id"]: r for r in out["results"]}
    assert by_id["b1"]["trip"]["version"] == 2
    assert by_id["b1"]["trip"]["status"] == "active"
    assert by_id["b1"]["trip"]["base_fare_cents"] == 800
    assert by_id["b2"]["trip"]["status"] == "cancelled"

    # 同一（账户 × 运营日）只重算一次，汇总在 affected_days
    assert out["affected_days"] == [{
        "account_id": account, "op_day": "2026-10-06",
        "charged_cents": 800, "sum_group_fare_cents": 800,
        "daily_cap_cents": 2000,
    }]

    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert len(d["groups"]) == 1
    assert [t["trip_id"] for t in d["groups"][0]["trips"]] == ["b1"]
    assert d["charged_cents"] == 800

    # 非幂等乘次事件按输入顺序追加；同日只重算一次 -> 仅一对金额流水
    led = ledger(client, account)
    life = [e for e in led if e["event_type"].startswith("trip_")]
    assert [(e["trip_id"], e["event_type"]) for e in life] == [
        ("b1", "trip_posted"), ("b2", "trip_posted"),
        ("b1", "trip_corrected"), ("b2", "trip_cancelled"),
    ]
    money = [e for e in led if e["event_type"] in ("day_reversal", "day_charge")]
    # 首批记入 500；次批同一日仅重算一次：冲销 500 再记 800
    assert [(e["delta_cents"], e["event_type"]) for e in money] == [
        (500, "day_charge"), (-500, "day_reversal"), (800, "day_charge")]
    assert all(e["trip_id"] is None for e in money)


def test_batch_affected_days_new_and_old_once_each(client, account):
    # 先建两程：08:00(300) 10:00(700) 各独立组，实收 1000
    batch(client, [
        submit_item("a", account, "2026-10-06T08:00:00+08:00", 300),
        submit_item("b", account, "2026-10-06T10:00:00+08:00", 700),
    ])
    # 一批：a 跨日更正到 10-07（旧 10-06 / 新 10-07 各重算一次）；
    # b 票价改为 900（10-06 已在汇总集合中，不再重算第二次）
    out = batch(client, [
        correct_item("a", account, "2026-10-07T08:00:00+08:00", 300, 1),
        correct_item("b", account, "2026-10-06T10:00:00+08:00", 900, 1),
    ])
    days = {(d["account_id"], d["op_day"]): d for d in out["affected_days"]}
    assert set(days) == {(account, "2026-10-06"), (account, "2026-10-07")}
    assert days[(account, "2026-10-06")]["charged_cents"] == 900
    assert days[(account, "2026-10-07")]["charged_cents"] == 300

    # 10-06 只重算一次：实收 1000 -> 900，仅一对冲销（不是两次冲销）
    led6 = [e for e in ledger(client, account) if e["op_day"] == "2026-10-06"]
    rev = [e for e in led6 if e["event_type"] == "day_reversal"]
    assert [e["delta_cents"] for e in rev] == [-1000]
    # 首批批量记入 1000；本批只重算一次，最终一笔 batch 重算记账为 900
    chg = [e["delta_cents"] for e in led6 if e["event_type"] == "day_charge"]
    assert chg == [1000, 900]
    assert chg[-1] == 900


def test_batch_cross_account_correction(client):
    batch(client, [submit_item("xa", "accOLD",
                               "2026-10-06T08:00:00+08:00", 300)])
    out = batch(client, [
        correct_item("xa", "accNEW", "2026-10-06T08:00:00+08:00", 300, 1)])
    days = {(d["account_id"], d["op_day"]): d["charged_cents"]
            for d in out["affected_days"]}
    assert days == {("accOLD", "2026-10-06"): 0,
                    ("accNEW", "2026-10-06"): 300}
    assert client.get("/accounts/accOLD/days/2026-10-06").json()["groups"] == []
    assert client.get("/accounts/accNEW/days/2026-10-06").json() \
        ["charged_cents"] == 300


def test_batch_cancel_empties_day_removes_settlement(client, account):
    batch(client, [submit_item("only", account,
                               "2026-10-06T08:00:00+08:00", 400)])
    batch(client, [cancel_item("only", 1)])
    # 当日无有效乘次：affected_days 仍汇总该日，charged=0，日结被删除
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert d["groups"] == [] and d["charged_cents"] == 0
    with db.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM day_settlements WHERE account_id = %s",
            (account,)).fetchone()["n"]
    assert n == 0


# ---------------------------------------------------------- 幂等

def test_batch_all_idempotent_writes_nothing(client, account):
    body = [submit_item("i1", account, "2026-10-06T08:00:00+08:00", 200)]
    first = batch(client, body)
    assert first["idempotent"] is False

    out = batch(client, body)  # 同内容重放
    assert out["idempotent"] is True
    assert out["results"][0]["idempotent"] is True
    assert out["affected_days"] == []
    led = ledger(client, account)
    assert [e["event_type"] for e in led].count("trip_posted") == 1


def test_batch_idempotent_cancel_and_correct_replay(client, account):
    batch(client, [
        submit_item("p", account, "2026-10-06T08:00:00+08:00", 200),
        submit_item("q", account, "2026-10-06T08:00:00+08:00", 500),
    ])
    # q 同内容更正（幂等）+ p 同版本撤销（生效）
    out1 = batch(client, [
        correct_item("q", account, "2026-10-06T08:00:00+08:00", 500, 1),
        cancel_item("p", 1),
    ])
    assert [r["idempotent"] for r in out1["results"]] == [True, False]
    assert out1["idempotent"] is False
    assert [(d["op_day"], d["charged_cents"]) for d in out1["affected_days"]] \
        == [("2026-10-06", 500)]

    # 再次同版本撤销 p（幂等）：整批幂等，不重算、不追加流水
    before = ledger(client, account)
    out2 = batch(client, [cancel_item("p", 1)])
    assert out2["idempotent"] is True and out2["affected_days"] == []
    assert ledger(client, account) == before


def test_batch_version_chain_across_batches(client, account):
    # 同一乘次不得在一批内出现两次；版本链跨批次推进
    batch(client, [submit_item("chain", account,
                               "2026-10-06T08:00:00+08:00", 100)])
    batch(client, [correct_item("chain", account,
                                "2026-10-06T08:00:00+08:00", 600, 1)])
    out = batch(client, [cancel_item("chain", 2)])
    assert out["results"][0]["trip"]["status"] == "cancelled"
    assert out["results"][0]["trip"]["version"] == 2


def test_batch_grouping_reflects_all_items_atomically(client, account):
    # 批内新建的多程在整批提交时一次性参与归组：读者不会看到半成品
    out = batch(client, [
        submit_item("t0", account, "2026-10-06T08:00:00+08:00", 100),
        submit_item("t1", account, "2026-10-06T09:00:00+08:00", 100),
        submit_item("t2", account, "2026-10-06T10:00:00+08:00", 900),
    ])
    assert out["affected_days"][0]["sum_group_fare_cents"] == 900
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert len(d["groups"]) == 1
    assert [t["trip_id"] for t in d["groups"][0]["trips"]] == ["t0", "t1", "t2"]


def test_batch_idempotent_submit_does_not_join_recalc(client, account):
    # 已有同内容 x1；一批里重放 x1（幂等）+ 新建 x2：只按 x2 重算一次
    batch(client, [submit_item("x1", account,
                               "2026-10-06T08:00:00+08:00", 900)])
    out = batch(client, [
        submit_item("x1", account, "2026-10-06T08:00:00+08:00", 900),
        submit_item("x2", account, "2026-10-06T09:00:00+08:00", 100),
    ])
    assert out["results"][0]["idempotent"] is True
    assert out["results"][1]["idempotent"] is False
    assert len(out["affected_days"]) == 1
    assert out["affected_days"][0]["charged_cents"] == 900  # 同组取最高


# ---------------------------------------------------------- 原子失败

def test_batch_conflict_at_later_item_rolls_back_whole_batch(client, account):
    batch(client, [submit_item("e1", account,
                               "2026-10-06T08:00:00+08:00", 200)])
    # 第 0 项新建 e2 成功（事务内），第 1 项 e1 版本冲突 -> 整批回滚
    r = client.post(BATCH_URL, json={"items": [
        submit_item("e2", account, "2026-10-06T09:00:00+08:00", 300),
        correct_item("e1", account, "2026-10-06T08:00:00+08:00", 800, 9),
    ]})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "VERSION_CONFLICT"
    assert err["index"] == 1 and err["op"] == "correct" and err["trip_id"] == "e1"

    # e2 未提交，e1 未更正，流水无 trip_posted(e2)/trip_corrected
    assert client.get("/trips/e2").status_code == 404
    assert client.get("/trips/e1").json()["trip"]["version"] == 1
    led = ledger(client, account)
    assert not any(e["trip_id"] == "e2" for e in led)
    assert not any(e["event_type"] == "trip_corrected" for e in led)
    # 日结维持 200
    assert client.get(f"/accounts/{account}/days/2026-10-06").json() \
        ["charged_cents"] == 200


def test_batch_unknown_trip_preserves_original_code_and_position(client, account):
    r = client.post(BATCH_URL, json={"items": [
        submit_item("ok1", account, "2026-10-06T08:00:00+08:00", 100),
        cancel_item("ghost", 1),
    ]})
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "UNKNOWN_TRIP"
    assert err["index"] == 1 and err["op"] == "cancel"
    assert client.get("/trips/ok1").status_code == 404


def test_batch_idempotency_conflict_rollback(client, account):
    batch(client, [submit_item("dup1", account,
                               "2026-10-06T08:00:00+08:00", 100)])
    r = client.post(BATCH_URL, json={"items": [
        submit_item("dup1", account, "2026-10-06T08:05:00+08:00", 100),
    ]})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "IDEMPOTENCY_CONFLICT"
    assert err["index"] == 0 and err["trip_id"] == "dup1"


def test_batch_correct_cancelled_trip_returns_trip_not_active(client, account):
    batch(client, [submit_item("dead", account,
                               "2026-10-06T08:00:00+08:00", 100)])
    batch(client, [cancel_item("dead", 1)])
    r = client.post(BATCH_URL, json={"items": [
        correct_item("dead", account, "2026-10-06T08:00:00+08:00", 200, 1),
    ]})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TRIP_NOT_ACTIVE"


def test_batch_failure_does_not_burn_ledger_seq(client, account):
    batch(client, [submit_item("g1", account,
                               "2026-10-06T08:00:00+08:00", 100)])
    r = client.post(BATCH_URL, json={"items": [
        cancel_item("g1", 9),  # 版本冲突，整批回滚
    ]})
    assert r.status_code == 409
    batch(client, [submit_item("g2", account,
                               "2026-10-06T10:00:00+08:00", 300)])
    seqs = [e["seq"] for e in ledger(client, account)]
    assert seqs == list(range(1, len(seqs) + 1))


# ---------------------------------------------------------- 批内校验

def test_batch_duplicate_trip_id_rejected_with_positions(client, account):
    r = client.post(BATCH_URL, json={"items": [
        submit_item("same", account, "2026-10-06T08:00:00+08:00", 100),
        cancel_item("other", 1),
        cancel_item("same", 1),
    ]})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "DUPLICATE_TRIP_IN_BATCH"
    assert err["index"] == 2 and err["first_index"] == 0
    # 整批未执行：无任何乘次
    assert client.get("/trips/other").status_code == 404


def test_batch_item_field_validation_carries_index(client, account):
    r = client.post(BATCH_URL, json={"items": [
        submit_item("ok", account, "2026-10-06T08:00:00+08:00", 100),
        submit_item("badtime", account, "2026-10-06T08:00:00", 100),  # naive
    ]})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_TIME" and err["index"] == 1


def test_batch_invalid_op(client, account):
    r = client.post(BATCH_URL, json={"items": [
        {"op": "explode", "trip_id": "z", "expected_version": 1},
    ]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_OP"


def test_batch_item_field_codes(client, account):
    cases = [
        ([submit_item("z", 123, "2026-10-06T08:00:00+08:00", 1)],
         "INVALID_ACCOUNT_ID"),
        ([submit_item("z", account, "2026-10-06T08:00:00+08:00", -5)],
         "INVALID_FARE"),
        ([cancel_item("z", "v1")], "INVALID_VERSION"),
        ([{"op": "submit", "trip_id": "z",
           "account_id": account,
           "boarding_at": "2026-10-06T08:00:00+08:00",
           "base_fare_cents": 1, "extra": 1}], "INVALID_REQUEST"),
    ]
    for items, code in cases:
        r = client.post(BATCH_URL, json={"items": items})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == code, (code, r.text)

    # 批量更正 expected_version 必须 >= 1（与单笔 correct 一致）
    r = client.post(BATCH_URL, json={"items": [
        correct_item("z", account, "2026-10-06T08:00:00+08:00", 1, 0)]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_VERSION"

    # 批量撤销不做 >=1 入参限制（单笔 cancel 同样如此）：0 对未知乘次报 404
    r = client.post(BATCH_URL, json={"items": [cancel_item("zzz", 0)]})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "UNKNOWN_TRIP"


def test_batch_size_bounds(client, account):
    too_big = [submit_item(f"t{i}", account,
                           "2026-10-06T08:00:00+08:00", 1) for i in range(101)]
    assert client.post(BATCH_URL, json={"items": too_big}).status_code == 422
    assert client.post(BATCH_URL, json={"items": []}).status_code == 422
    # 恰好 100 项允许（同一乘次号限制不适用：id 各异）
    ok = [submit_item(f"h{i:03d}", account,
                      "2026-10-06T08:00:00+08:00", 1) for i in range(100)]
    r = client.post(BATCH_URL, json={"items": ok})
    assert r.status_code == 200, r.text
    assert len(r.json()["results"]) == 100


def test_batch_extra_top_level_field_rejected(client, account):
    r = client.post(BATCH_URL, json={"items": [], "note": "x"})
    assert r.status_code == 422
    # 空列表本身也是长度错误；统一 INVALID_REQUEST
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


# ---------------------------------------------------------- 规则适用

def test_batch_recalc_uses_applicable_rule_per_day(client, account):
    client.post("/rules", json={
        "effective_day": "2026-10-06", "transfer_window_minutes": 60,
        "daily_cap_cents": 500, "expected_version": 0})
    # 10-07 单独发布一条规则，避免被 10-06 规则的开放区间覆盖
    client.post("/rules", json={
        "effective_day": "2026-10-07", "transfer_window_minutes": 90,
        "daily_cap_cents": 3000, "expected_version": 0})
    out = batch(client, [
        submit_item("r1", account, "2026-10-06T08:00:00+08:00", 600),
        submit_item("r2", account, "2026-10-06T09:30:00+08:00", 600),
        submit_item("r3", account, "2026-10-07T08:00:00+08:00", 2500),
    ])
    days = {(d["op_day"]): d for d in out["affected_days"]}
    # 10-06：窗口 60 拆两组 1200，封顶 500
    assert days["2026-10-06"]["daily_cap_cents"] == 500
    assert days["2026-10-06"]["sum_group_fare_cents"] == 1200
    assert days["2026-10-06"]["charged_cents"] == 500
    # 10-07：适用当日发布规则（窗口 90 / 封顶 3000），2500 足额
    assert days["2026-10-07"]["daily_cap_cents"] == 3000
    assert days["2026-10-07"]["charged_cents"] == 2500


# ---------------------------------------------------------- 并发串行

def test_concurrent_batches_and_single_writes_serialized(client, account):
    def one(i):
        if i % 3 == 0:
            return client.post("/trips", json={
                "trip_id": f"s{i}", "account_id": account,
                "boarding_at": f"2026-10-06T{6 + (i % 18):02d}:00:00+08:00",
                "base_fare_cents": 100})
        return client.post(BATCH_URL, json={"items": [
            submit_item(f"s{i}", account,
                        f"2026-10-06T{6 + (i % 18):02d}:00:00+08:00", 100)]})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(one, range(24)))
    assert all(r.status_code in (200, 201) for r in results)

    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    total = sum(len(g["trips"]) for g in d["groups"])
    assert total == 24
    with db.pool.connection() as conn:
        seqs = [r["seq"] for r in conn.execute(
            "SELECT seq FROM ledger_entries WHERE account_id = %s ORDER BY seq",
            (account,)).fetchall()]
    assert seqs == list(range(1, len(seqs) + 1))


def test_concurrent_batch_and_rule_publish_never_inconsistent(client, account):
    def push_trips():
        return batch(client, [
            submit_item("c0", account, "2026-10-06T06:00:00+08:00", 600),
            submit_item("c1", account, "2026-10-06T07:00:00+08:00", 100),
            submit_item("c2", account, "2026-10-06T08:00:00+08:00", 900),
        ])

    def publish_chain():
        for v, w in ((0, 30), (1, 60), (2, 90)):
            r = client.post("/rules", json={
                "effective_day": "2026-10-06",
                "transfer_window_minutes": w, "daily_cap_cents": 2000,
                "expected_version": v})
            assert r.status_code == 200, r.text

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        f1 = ex.submit(push_trips)
        f2 = ex.submit(publish_chain)
        f1.result(); f2.result()

    # 最终状态必须与“最终规则 90 分钟”独立重算结果一致（三程同组，取最高 900）
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert len(d["groups"]) == 1
    assert d["groups"][0]["group_fare_cents"] == 900
    assert d["charged_cents"] == 900
    with db.pool.connection() as conn:
        seqs = [r["seq"] for r in conn.execute(
            "SELECT seq FROM ledger_entries WHERE account_id = %s ORDER BY seq",
            (account,)).fetchall()]
    assert seqs == list(range(1, len(seqs) + 1))
