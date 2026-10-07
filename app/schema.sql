-- 公交换乘票价清分服务数据库结构
-- 所有时间以 UTC 存储（TIMESTAMPTZ）；运营日按北京时间(UTC+8) 04:00 切分，落库为北京日期。

BEGIN;

-- 乘车记录：乘次号全局唯一；version 单调递增，用于乐观并发控制（陈旧版本拒绝）
CREATE TABLE IF NOT EXISTS trips (
    trip_id        TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL,
    boarding_at    TIMESTAMPTZ NOT NULL,           -- 北京时间上车时刻（内部存 UTC）
    base_fare_cents BIGINT NOT NULL,               -- 非负基础票价（分）
    status         TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'cancelled')),
    op_day         DATE NOT NULL,                  -- 按北京时间 04:00 界折算的运营日
    version        BIGINT NOT NULL DEFAULT 1,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fares_non_negative CHECK (base_fare_cents >= 0)
);

CREATE INDEX IF NOT EXISTS idx_trips_account_day
    ON trips (account_id, op_day, boarding_at, trip_id);

-- 日结：账户 × 运营日一条；单日实收 = min(组费合计, 封顶值)
CREATE TABLE IF NOT EXISTS day_settlements (
    account_id        TEXT NOT NULL,
    op_day            DATE NOT NULL,
    sum_group_fare_cents  BIGINT NOT NULL,         -- 组内最高票价之和（封顶前）
    charged_cents    BIGINT NOT NULL,             -- 当日实际收取（封顶后）
    recalculated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, op_day),
    CONSTRAINT settlement_non_negative
        CHECK (charged_cents >= 0 AND sum_group_fare_cents >= 0)
);

-- 换乘组：日内按 (上车时刻, 乘次号) 排序后，相邻间隔 <=90 分钟归同组
CREATE TABLE IF NOT EXISTS transfer_groups (
    group_id        BIGSERIAL PRIMARY KEY,
    account_id      TEXT NOT NULL,
    op_day          DATE NOT NULL,
    group_index     INTEGER NOT NULL,              -- 日内组序号（从 0 开始，按时间排序）
    group_fare_cents BIGINT NOT NULL,              -- 组内最高基础票价
    first_boarding_at TIMESTAMPTZ NOT NULL,
    last_boarding_at  TIMESTAMPTZ NOT NULL,
    UNIQUE (account_id, op_day, group_index)
);

CREATE INDEX IF NOT EXISTS idx_groups_account_day
    ON transfer_groups (account_id, op_day, group_index);

-- 组成员：乘次号到组的归属（组为重算派生产物，整组重写）
CREATE TABLE IF NOT EXISTS group_members (
    group_id   BIGINT NOT NULL REFERENCES transfer_groups(group_id) ON DELETE CASCADE,
    trip_id    TEXT NOT NULL PRIMARY KEY REFERENCES trips(trip_id),
    seq        INTEGER NOT NULL                    -- 组内顺序（按上车时刻、乘次号）
);

-- 运营方按运营日发布的费率规则：换乘时限（正整数分钟）与单日封顶（非负整数分）。
-- 任一运营日采用 effective_day <= 该日 的最新一条；无记录时沿用环境默认（90/2000）。
-- 每个生效日一行，version 从 1 起单调递增，用于乐观并发（未发布时期望版本为 0）。
CREATE TABLE IF NOT EXISTS fare_rules (
    effective_day           DATE PRIMARY KEY,           -- 规则生效运营日（北京日期）
    transfer_window_minutes INTEGER NOT NULL,           -- 相邻两程同组最大间隔（分钟，含端点）
    daily_cap_cents         BIGINT NOT NULL,            -- 单账户单运营日实收封顶（分）
    version                 BIGINT NOT NULL DEFAULT 1,  -- 该生效日规则版本（重发 +1）
    published_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT rule_window_positive CHECK (transfer_window_minutes > 0),
    CONSTRAINT rule_cap_non_negative CHECK (daily_cap_cents >= 0)
);

-- 流水：仅追加（append-only），seq 按提交顺序全局严格递增且无空洞。
-- 所有写事务先取同一个事务级 advisory 锁并串行提交，应用层以 MAX(seq)+1 赋 seq。
-- 金额类条目成对出现：day_reversal（冲销旧日实收）+ day_charge（记入新金额）。
CREATE TABLE IF NOT EXISTS ledger_entries (
    seq            BIGINT PRIMARY KEY,
    account_id     TEXT NOT NULL,
    op_day         DATE NOT NULL,
    event_type     TEXT NOT NULL CHECK (event_type IN (
                       'trip_posted', 'trip_corrected', 'trip_cancelled',
                       'day_reversal', 'day_charge')),
    trip_id        TEXT,
    delta_cents    BIGINT NOT NULL,                -- 对当日实收的增量（生命周期事件为 0）
    charged_after_cents BIGINT,                    -- 金额事件后的当日实收（生命周期事件为 NULL）
    reason         TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ledger_account_day_seq
    ON ledger_entries (account_id, op_day, seq);
CREATE INDEX IF NOT EXISTS idx_ledger_trip ON ledger_entries (trip_id);

-- seq 不使用 SEQUENCE：事务回滚（如冲突请求）会烧掉序号产生空洞。
-- 应用层所有写事务持有同一事务级 advisory 锁（严格串行提交），
-- 插入时取 MAX(seq)+1，故已提交流水的 seq 严格连续且等于提交顺序。

-- 强制流水不可改写、不可删除
CREATE OR REPLACE FUNCTION ledger_append_only() RETURNS trigger AS $fn$
BEGIN
    RAISE EXCEPTION 'ledger_entries is append-only; % is not permitted', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ledger_no_update ON ledger_entries;
CREATE TRIGGER trg_ledger_no_update BEFORE UPDATE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_append_only();
DROP TRIGGER IF EXISTS trg_ledger_no_delete ON ledger_entries;
CREATE TRIGGER trg_ledger_no_delete BEFORE DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_append_only();

COMMIT;
