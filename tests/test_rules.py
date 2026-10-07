"""按运营日生效规则（换乘时限/单日封顶）的发布、适用与回溯重算测试。"""
import concurrent.futures

import pytest

from app import db


def post_trip(client, tid, acc, ts, fare, status=201):
    r = client.post("/trips", json={
        "trip_id": tid, "account_id": acc,
        "boarding_at": ts, "base_fare_cents": fare})
    assert r.status_code == status, r.text
    return r.json()


def publish(client, day, window, cap, expected, status=200):
    r = client.post("/rules", json={
        "effective_day": day, "transfer_window_minutes": window,
        "daily_cap_cents": cap, "expected_version": expected})
    assert r.status_code == status, r.text
    return r.json()


def ledger_entries(client, acc, limit=200):
    return client.get(f"/accounts/{acc}/ledger?limit={limit}").json()["entries"]


# ---------------------------------------------------------- 适用查询与默认值

def test_default_rule_when_none_published(client):
    d = client.get("/rules/2026-10-06").json()
    assert d == {
        "effective_day": None,
        "transfer_window_minutes": 90,
        "daily_cap_cents": 2000,
        "version": 0,
        "published": False,
        "source": "default",
        "coverage_from": None,
        "coverage_to": None,
    }
    assert client.get("/rules").json() == {"rules": []}


def test_list_rules_sorted_by_effective_day(client):
    publish(client, "2026-11-01", 60, 500, 0)
    publish(client, "2026-10-01", 30, 800, 0)
    rules = client.get("/rules").json()["rules"]
    assert [r["effective_day"] for r in rules] == ["2026-10-01", "2026-11-01"]
    assert all(r["published"] and r["source"] == "published" for r in rules)


def test_coverage_to_next_effective_day(client):
    publish(client, "2026-10-01", 90, 2000, 0)
    publish(client, "2026-11-01", 60, 500, 0)

    before = client.get("/rules/2026-09-30").json()
    middle = client.get("/rules/2026-10-15").json()
    edge = client.get("/rules/2026-11-01").json()

    # 默认区间 (-∞, 首条生效日)
    assert before["source"] == "default" and before["coverage_to"] == "2026-10-01"
    # 命中规则：[生效日, 下一生效日)
    assert middle["effective_day"] == "2026-10-01"
    assert middle["coverage_from"] == "2026-10-01"
    assert middle["coverage_to"] == "2026-11-01"
    # 生效日当天即适用新规则；末条规则覆盖到无限远
    assert edge["effective_day"] == "2026-11-01" and edge["coverage_to"] is None


def test_backdated_insert_splits_previous_coverage(client, account):
    # 先发 11-01，再回溯插入 10-01：10-06 应改由 10-01 规则覆盖
    publish(client, "2026-11-01", 30, 500, 0)
    post_trip(client, "a", account, "2026-10-06T08:00:00+08:00", 600)
    post_trip(client, "b", account, "2026-10-06T09:30:00+08:00", 600)  # 间隔 90
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert len(d["groups"]) == 1  # 默认 90 分钟同组

    out = publish(client, "2026-10-01", 60, 2000, 0)
    # 覆盖 [10-01, 11-01)，区间内 10-06 被重算
    assert out["coverage_to"] == "2026-11-01"
    assert out["recalculated_account_days"] == 1
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert len(d["groups"]) == 2  # 90 分钟 > 新窗口 60，拆成两组


# ---------------------------------------------------------- 发布与版本

def test_publish_backdated_recalcs_window_and_cap(client, account):
    post_trip(client, "a", account, "2026-10-06T08:00:00+08:00", 600)
    post_trip(client, "b", account, "2026-10-06T09:30:00+08:00", 600)  # 90 分钟
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert len(d["groups"]) == 1 and d["charged_cents"] == 600

    out = publish(client, "2026-10-06", 60, 500, 0)
    assert out["idempotent"] is False
    assert out["rule"]["version"] == 1
    assert out["rule"]["transfer_window_minutes"] == 60
    assert out["rule"]["daily_cap_cents"] == 500
    assert out["coverage_from"] == "2026-10-06" and out["coverage_to"] is None
    assert out["recalculated_account_days"] == 1

    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert len(d["groups"]) == 2                 # 窗口 60：两程拆组
    assert d["daily_cap_cents"] == 500           # 日结响应展示适用封顶
    assert d["sum_group_fare_cents"] == 1200
    assert d["charged_cents"] == 500             # 封顶 500

    led = ledger_entries(client, account)
    rev = [e for e in led if e["event_type"] == "day_reversal"][-1]
    chg = [e for e in led if e["event_type"] == "day_charge"][-1]
    assert rev["delta_cents"] == -600 and rev["trip_id"] is None
    assert chg["delta_cents"] == 500 and chg["trip_id"] is None
    assert "rule_published:2026-10-06" in rev["reason"]


def test_republish_bumps_version_and_recalcs(client, account):
    post_trip(client, "a", account, "2026-10-06T08:00:00+08:00", 600)
    publish(client, "2026-10-06", 60, 500, 0)
    out = publish(client, "2026-10-06", 90, 2000, 1)
    assert out["rule"]["version"] == 2
    assert out["recalculated_account_days"] == 1
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert d["daily_cap_cents"] == 2000 and d["charged_cents"] == 600


def test_expected_version_conflict_changes_nothing(client, account):
    post_trip(client, "a", account, "2026-10-06T08:00:00+08:00", 600)
    # 未发布却声称版本 5 -> 冲突，不改数据
    r = client.post("/rules", json={
        "effective_day": "2026-10-06", "transfer_window_minutes": 60,
        "daily_cap_cents": 500, "expected_version": 5})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RULE_VERSION_CONFLICT"

    rule = client.get("/rules/2026-10-06").json()
    assert rule["source"] == "default"
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert d["charged_cents"] == 600

    publish(client, "2026-10-06", 60, 500, 0)
    # 陈旧版本（0 或 9）重发 -> 冲突
    for stale in (0, 9):
        r = client.post("/rules", json={
            "effective_day": "2026-10-06", "transfer_window_minutes": 30,
            "daily_cap_cents": 100, "expected_version": stale})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "RULE_VERSION_CONFLICT"
    assert client.get("/rules/2026-10-06").json()["version"] == 1
    # 以最新版本重发同内容 -> 幂等，版本不变、不重算
    out = publish(client, "2026-10-06", 60, 500, 1)
    assert out["idempotent"] is True and out["rule"]["version"] == 1
    assert out["recalculated_account_days"] == 0


def test_conflicting_publish_does_not_burn_ledger_seq(client, account):
    post_trip(client, "a", account, "2026-10-06T08:00:00+08:00", 100)
    publish(client, "2026-10-06", 60, 2000, 0)
    # 冲突回滚不应耗流水号
    for bad_expected in (0, 9):
        r = client.post("/rules", json={
            "effective_day": "2026-10-06", "transfer_window_minutes": 30,
            "daily_cap_cents": 100, "expected_version": bad_expected})
        assert r.status_code == 409
    publish(client, "2026-10-06", 30, 2000, 1)
    seqs = [e["seq"] for e in ledger_entries(client, account)]
    assert seqs == list(range(1, len(seqs) + 1))


# ---------------------------------------------------------- 覆盖区间与重算范围

def test_forward_publish_does_not_recalc_earlier_days(client, account):
    post_trip(client, "a", account, "2026-10-06T08:00:00+08:00", 600)
    post_trip(client, "b", account, "2026-10-06T09:30:00+08:00", 600)
    out = publish(client, "2026-10-10", 60, 500, 0)
    assert out["recalculated_account_days"] == 0
    # 10-06 仍按默认 90 分钟
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert len(d["groups"]) == 1 and d["daily_cap_cents"] == 2000
    # 10-10 的查询已适用新规则
    assert client.get("/rules/2026-10-10").json()["version"] == 1


def test_recalc_count_scoped_to_coverage_and_active_trips(client):
    # 两账户在 [10-06, 10-08) 各有乘次；另有一个仅含已撤销乘次的账户日
    post_trip(client, "a1", "acc1", "2026-10-06T08:00:00+08:00", 100)
    post_trip(client, "a2", "acc1", "2026-10-07T08:00:00+08:00", 100)
    post_trip(client, "b1", "acc2", "2026-10-06T08:00:00+08:00", 100)
    post_trip(client, "dead", "acc3", "2026-10-06T08:00:00+08:00", 100)
    client.post("/trips/dead/cancel?expected_version=1")
    # 生效日 10-07：覆盖 [10-07, 下一生效日)，10-06 不受影响
    out = publish(client, "2026-10-07", 60, 500, 0)
    assert out["recalculated_account_days"] == 1  # 仅 (acc1, 10-07)
    d6 = client.get("/accounts/acc1/days/2026-10-06").json()
    assert d6["daily_cap_cents"] == 2000
    d7 = client.get("/accounts/acc1/days/2026-10-07").json()
    assert d7["daily_cap_cents"] == 500


def test_new_trips_are_fared_with_applicable_rule(client, account):
    publish(client, "2026-10-10", 60, 500, 0)
    # 生效日之后：间隔 90 分钟在 60 分钟窗口下拆组，600 封顶 500
    post_trip(client, "n1", account, "2026-10-10T08:00:00+08:00", 300)
    post_trip(client, "n2", account, "2026-10-10T09:30:00+08:00", 300)
    d10 = client.get(f"/accounts/{account}/days/2026-10-10").json()
    assert len(d10["groups"]) == 2 and d10["charged_cents"] == 500
    # 生效日之前：默认窗口 90，同组、合计 300
    post_trip(client, "o1", account, "2026-10-09T08:00:00+08:00", 300)
    post_trip(client, "o2", account, "2026-10-09T09:30:00+08:00", 300)
    d09 = client.get(f"/accounts/{account}/days/2026-10-09").json()
    assert len(d09["groups"]) == 1 and d09["charged_cents"] == 300


# ---------------------------------------------------------- 封顶/流水边界

def test_zero_cap_rule_charges_zero(client, account):
    post_trip(client, "a", account, "2026-10-06T08:00:00+08:00", 500)
    publish(client, "2026-10-06", 90, 0, 0)
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert d["daily_cap_cents"] == 0 and d["charged_cents"] == 0
    led = ledger_entries(client, account)
    assert led[-1]["event_type"] == "day_reversal"
    assert led[-1]["delta_cents"] == -500
    assert not [e for e in led if e["event_type"] == "day_charge"
                and "rule_published" in (e["reason"] or "")]

    # 恢复封顶：重新记入（历史流水不删除，只追加）
    publish(client, "2026-10-06", 90, 2000, 1)
    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    assert d["charged_cents"] == 500
    assert ledger_entries(client, account)[-1]["delta_cents"] == 500


def test_rule_change_with_unchanged_amount_writes_no_money_ledger(client, account):
    post_trip(client, "a", account, "2026-10-06T08:00:00+08:00", 100)
    # 单乘次：窗口 90 -> 60 不改变分组与实收，不写金额流水
    publish(client, "2026-10-06", 60, 2000, 0)
    money = [e for e in ledger_entries(client, account)
             if e["event_type"] in ("day_reversal", "day_charge")]
    assert len(money) == 1 and money[0]["delta_cents"] == 100


def test_historical_ledger_is_never_rewritten(client, account):
    post_trip(client, "a", account, "2026-10-06T08:00:00+08:00", 600)
    publish(client, "2026-10-06", 60, 500, 0)
    with db.pool.connection() as conn:
        with pytest.raises(Exception):
            conn.execute("UPDATE ledger_entries SET delta_cents = 0 WHERE seq = 1")
        with pytest.raises(Exception):
            conn.execute("DELETE FROM ledger_entries WHERE seq = 1")


# ---------------------------------------------------------- 非法输入

def test_invalid_rule_inputs(client):
    def pub(body, code, status=422):
        r = client.post("/rules", json=body)
        assert r.status_code == status, r.text
        assert r.json()["error"]["code"] == code, r.text

    base = {"effective_day": "2026-10-06", "transfer_window_minutes": 60,
            "daily_cap_cents": 500, "expected_version": 0}
    pub(dict(base, effective_day="10/06/2026"), "INVALID_DATE")
    pub(dict(base, effective_day="2150-01-01"), "INVALID_DATE")
    pub(dict(base, transfer_window_minutes=0), "INVALID_WINDOW")
    pub(dict(base, transfer_window_minutes=-5), "INVALID_WINDOW")
    pub(dict(base, transfer_window_minutes=1.5), "INVALID_WINDOW")
    pub(dict(base, daily_cap_cents=-1), "INVALID_CAP")
    pub(dict(base, daily_cap_cents=1.5), "INVALID_CAP")
    pub(dict(base, expected_version=-1), "INVALID_VERSION")
    pub(dict(base, unexpected="x"), "INVALID_REQUEST")
    pub({k: v for k, v in base.items() if k != "daily_cap_cents"},
        "INVALID_CAP")

    r = client.get("/rules/not-a-date")
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_DATE"


# ---------------------------------------------------------- 并发

def test_concurrent_publish_and_trips_never_inconsistent(client, account):
    # 相邻间隔（分钟）：40, 50, 70, 80, 120
    times = ["2026-10-06T06:00:00+08:00", "2026-10-06T06:40:00+08:00",
             "2026-10-06T07:30:00+08:00", "2026-10-06T08:40:00+08:00",
             "2026-10-06T10:00:00+08:00", "2026-10-06T12:00:00+08:00"]
    fares = [600, 100, 900, 200, 800, 300]

    def submit(i):
        return post_trip(client, f"c{i}", account, times[i], fares[i])

    def publish_chain():
        # 串行发布 3 个版本（30 -> 60 -> 90），最终窗口 90 / 封顶 2000
        publish(client, "2026-10-06", 30, 3000, 0)
        publish(client, "2026-10-06", 60, 2500, 1)
        publish(client, "2026-10-06", 90, 2000, 2)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(submit, i) for i in range(6)]
        futs.append(ex.submit(publish_chain))
        [f.result() for f in futs]

    rule = client.get("/rules/2026-10-06").json()
    assert rule["version"] == 3
    window, cap = rule["transfer_window_minutes"], rule["daily_cap_cents"]

    d = client.get(f"/accounts/{account}/days/2026-10-06").json()
    # 用最终规则独立校验：组内相邻间隔 <= window，组间断口 > window
    from datetime import datetime, timedelta
    all_members, sum_group = [], 0
    for g in d["groups"]:
        ts = [datetime.fromisoformat(t["boarding_at"]) for t in g["trips"]]
        assert all(b - a <= timedelta(minutes=window)
                   for a, b in zip(ts, ts[1:]))
        all_members.extend(t["trip_id"] for t in g["trips"])
        assert g["group_fare_cents"] == max(t["base_fare_cents"]
                                            for t in g["trips"])
        sum_group += g["group_fare_cents"]
    # 相邻组之间必有一对相邻乘次间隔 > window（贪心分组边界）
    boundaries = zip(d["groups"], d["groups"][1:])
    for g1, g2 in boundaries:
        last = datetime.fromisoformat(g1["trips"][-1]["boarding_at"])
        first = datetime.fromisoformat(g2["trips"][0]["boarding_at"])
        assert first - last > timedelta(minutes=window)
    # 最终窗口 90：前 5 程（间隔 40/50/70/80）链式同组，末程间隔 120 另起一组
    assert len(d["groups"]) == 2
    assert [t["trip_id"] for t in d["groups"][0]["trips"]] == \
           ["c0", "c1", "c2", "c3", "c4"]
    assert [t["trip_id"] for t in d["groups"][1]["trips"]] == ["c5"]
    assert sorted(all_members) == [f"c{i}" for i in range(6)]
    assert d["sum_group_fare_cents"] == sum_group
    assert d["charged_cents"] == min(sum_group, cap)

    # 流水序号仍连续无空洞
    seqs = [e["seq"] for e in ledger_entries(client, account, limit=200)]
    assert seqs == list(range(1, len(seqs) + 1))
