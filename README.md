# 公交换乘票价清分服务

Python 3 / FastAPI + PostgreSQL 实现的公交换乘票价清分（fare clearing）服务。
支持乘车记录幂等提交、04:00 运营日界、90 分钟链式换乘归组、组内取最高票价、
单账户单日实收封顶、迟到补录与跨日更正的自动重算、版本化更正/撤销、
运营方按运营日发布的换乘时限与单日封顶规则（回溯发布自动重算覆盖区间）、
清分方补录文件 1～100 项提交/更正/撤销的批量原子结算，
以及按提交顺序递增、仅追加、游标翻页的原子流水。

## 一条命令启动

前置：已安装 Docker 与 Docker Compose v2。

```bash
docker compose up --build
```

该命令会：

1. 启动 PostgreSQL 16（账号 `fare` / 密码 `farepass` / 库 `fareclearing`），数据持久化到命名卷 `pgdata`；
2. 等待数据库健康检查通过；
3. 构建并启动 API 容器，自动完成表结构初始化（`app/schema.sql`，幂等）。

启动后：

| 用途 | 地址 |
| --- | --- |
| HTTP API 基地址 | <http://localhost:8080> |
| 健康检查 | <http://localhost:8080/healthz> |
| 交互式接口文档（Swagger UI） | <http://localhost:8080/docs> |
| OpenAPI 描述 | <http://localhost:8080/openapi.json> |
| PostgreSQL | `localhost:5432`（库 `fareclearing`） |

停止：`Ctrl+C` 后 `docker compose down`（保留数据）；连同数据卷一起清除：`docker compose down -v`。

运行调用示例：

```bash
bash scripts/examples.sh            # 默认 http://localhost:8080
```

## 配置项

通过环境变量配置（见 `docker-compose.yml` 与 `.env.example`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://fare:farepass@localhost:5432/fareclearing` | PostgreSQL DSN |
| `APP_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `APP_PORT` | `8080` | HTTP 监听端口（宿主映射也可用同名环境变量覆盖） |
| `DAY_BOUNDARY_HOUR` | `4` | 运营日界（北京时间整点）；`[当日04:00, 次日04:00)` 为当日运营日 |
| `TRANSFER_WINDOW_MINUTES` | `90` | 相邻两程归为同一换乘组的默认最大间隔（分钟，含端点）；无已发布规则覆盖时使用 |
| `DAILY_CAP_CENTS` | `2000` | 单账户单运营日实收默认封顶（分）；无已发布规则覆盖时使用 |

## 业务规则

- **运营日界**：上车时刻按北京时间（UTC+8）折算；04:00 之前归入前一运营日。
  时间一律以带时区偏移的 ISO 8601 提交（如 `2026-10-06T08:00:00+08:00`），库内统一存 UTC。
- **换乘归组**：同一账户、同一运营日内，先按上车时刻、再按乘次号排序；
  **相邻**两程间隔 ≤ 换乘时限即归入同一换乘组（链式：只比较相邻两程，不与首程比较）；
  跨运营日绝不合组。换乘时限取该运营日适用规则（默认 90 分钟）。
- **组费**：每组取组内最高基础票价。
- **日实收**：当日各组组费之和，按该运营日适用封顶值封顶（默认 `2000` 分，
  `min(组费合计, 封顶值)`）；封顶不跨日。
- **费率规则（按运营日发布）**：运营方可发布按运营日生效的「换乘时限 + 单日封顶」。
  - 任一运营日采用 `effective_day <= 该日` 的**最新一条**已发布规则，
    覆盖区间为 `[生效日, 下一生效日)`，末条规则向未来无限开放；
    无任何已发布规则覆盖时沿用环境默认（90 分钟 / 2000 分）。
  - 提交内容：生效日 `YYYY-MM-DD`、正整数窗口分钟数、非负整数封顶分值、
    该生效日的预期版本（未发布为 `0`，已发布为当前版本）。
  - 每个生效日独立计版本：首次发布得到版本 1，同版本同内容重发幂等
    （不改数据、不重算、不产生流水）；不同内容重发版本 +1。
  - **回溯发布**（含向已有规则之间插入新生效日）在**同一事务**内重算覆盖区间
    `[生效日, 下一生效日)` 内已有有效乘次的全部「账户 × 运营日」：重写换乘组与日结；
    实收变化沿用既有 `day_reversal` / `day_charge` 成对流水，金额不变不写金额流水，
    历史流水不可改写。
  - 规则发布与乘次提交/更正/撤销共用同一事务级 advisory 锁严格串行；
    读接口取 `REPEATABLE READ` 快照，任何查询都看不到「规则已更新但账单仍旧」的中间态。
  - 日期或数值非法（窗口 ≤ 0、封顶 < 0、非整数、年份越界等）返回 422；
    预期版本与当前版本不符返回 409 `RULE_VERSION_CONFLICT` 且不改动任何数据。
- **幂等提交**：乘次号全局唯一；同号同内容重传返回当前状态且 `idempotent=true`、不产生新流水；
  同号异内容返回 `409 IDEMPOTENCY_CONFLICT`。
- **更正 / 撤销**：更正须携带乘次当前 `version`，成功后版本 +1；撤销不推进版本，同版本重复撤销幂等。
  未知乘次返回 `404 UNKNOWN_TRIP`，版本不匹配返回 `409 VERSION_CONFLICT`。
- **批量原子结算（清分方补录文件）**：`POST /trips/batch` 在**单个事务**内按输入顺序执行
  1～100 项 `submit` / `correct` / `cancel`，逐项沿用上述幂等、版本与状态规则。
  - 每项携带其对应单笔操作的字段（`submit` 同 `POST /trips`；`correct` 同更正接口，
    路径上的 `trip_id` 放入项内；`cancel` 的 `expected_version` 也在项内）。
  - **同一乘次号在一个批次内不得出现两次**（`422 DUPLICATE_TRIP_IN_BATCH`）；
    因此批次中的更正/撤销针对此前已存在的乘次（补录的典型场景）。
  - 任一项非法（422 字段错误）或业务冲突（404/409）则**整批不改**乘次、换乘组、
    日结与流水，错误体返回出错项下标 `index`（从 0 起）、`trip_id` 及**原单笔业务错误码**；
    回滚不消耗流水序号（seq 仍无空洞）。
  - 成功后非幂等乘次生命周期事件（`trip_posted`/`trip_corrected`/`trip_cancelled`）
    **按输入顺序**追加；汇总新旧受影响的账户与运营日（含跨日/跨账户更正的旧端），
    **每个（账户 × 运营日）只按其适用规则重算一次**，实收变化沿用既有
    `day_reversal` / `day_charge` 成对流水（`reason` 含 `batch_settlement`，
    金额事件 `trip_id` 为 `null`）。
  - 响应按输入顺序给出逐项乘次状态与 `idempotent` 标记，并汇总
    `affected_days`（每项含 `account_id`、`op_day`、批量后 `charged_cents`，
    当日无有效乘次为 `0`）；顶层 `idempotent=true` 表示整批均为幂等重放、
    无任何改动与新流水。
  - 批量与全部单笔写入及规则回溯发布共用同一事务级 advisory 锁严格串行；
    读接口取 `REPEATABLE READ` 快照，读者只能看到批量前或批量后的完整状态。
  - 既有单笔接口的请求/响应、错误格式与流水结构保持不变。
- **重算**：迟到记录或更正（含跨日更正、账户变更）会重算所有受影响的（账户 × 运营日）。
  当日实收发生变化时，流水成对追加 `day_reversal`（冲销旧日金额）与 `day_charge`（记入新金额）；
  实收不变不产生金额流水。
- **原子性**：乘次状态、换乘组、日结、流水在单个数据库事务内提交；
  所有写事务（含规则发布）经同一事务级 advisory 锁串行化；
  读接口使用 `REPEATABLE READ` 快照，规则发布过程中查询只见发布前或发布后的一致视图。
- **流水**：`seq` 按提交顺序全局严格递增且无空洞（回滚的冲突请求不耗号）；
  表级触发器禁止 UPDATE/DELETE（仅追加，已返回流水不可改写）；
  以 `seq > cursor` 的 keyset 分页，翻页不重不漏。

## HTTP API

所有请求/响应均为 JSON。金额单位均为人民币分（非负整数）。

### `POST /trips` —— 提交乘车记录

```bash
curl -X POST http://localhost:8080/trips \
  -H 'Content-Type: application/json' \
  -d '{"trip_id":"T1","account_id":"A001",
       "boarding_at":"2026-10-06T08:00:00+08:00",
       "base_fare_cents":200}'
```

响应 `201`：

```json
{
  "idempotent": false,
  "trip": {
    "trip_id": "T1", "account_id": "A001",
    "boarding_at": "2026-10-06T08:00:00+08:00",
    "base_fare_cents": 200, "status": "active",
    "op_day": "2026-10-06", "version": 1
  }
}
```

### `POST /trips/{trip_id}/correct` —— 更正（凭乘次号 + 当前版本）

```bash
curl -X POST http://localhost:8080/trips/T1/correct \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"A001","boarding_at":"2026-10-06T08:00:00+08:00",
       "base_fare_cents":800,"expected_version":1}'
```

成功 `200`，`version` 递增；内容与当前一致时为幂等重放（`idempotent=true`，版本不变）。
跨运营日（或跨账户）更正会自动重算新旧双方运营日。

### `POST /trips/{trip_id}/cancel?expected_version=1` —— 撤销

成功 `200`，乘次置为 `cancelled` 并从换乘组与日结中剔除、重算当日；
同版本重复撤销幂等。

### `POST /trips/batch` —— 清分方补录批量原子结算

请求体为 `{"operations": [...]}`，1～100 项，按数组顺序在**一个事务**内执行。
每项用 `op` 区分操作，其余字段与对应单笔接口完全一致：

| `op` | 字段 |
| --- | --- |
| `submit` | `trip_id`、`account_id`、`boarding_at`、`base_fare_cents` |
| `correct` | `trip_id`、`account_id`、`boarding_at`、`base_fare_cents`、`expected_version` |
| `cancel` | `trip_id`、`expected_version` |

> 一个批次内同一 `trip_id` 只能出现一次；批次中的更正/撤销用于处理此前已存在的乘次。

```bash
curl -X POST http://localhost:8080/trips/batch \
  -H 'Content-Type: application/json' \
  -d '{"operations": [
    {"op": "submit",  "trip_id": "B2", "account_id": "A001",
     "boarding_at": "2026-10-08T08:00:00+08:00", "base_fare_cents": 900},
    {"op": "correct", "trip_id": "B0", "account_id": "A001",
     "boarding_at": "2026-10-08T09:00:00+08:00", "base_fare_cents": 600,
     "expected_version": 1},
    {"op": "cancel",  "trip_id": "B1", "expected_version": 1}
  ]}'
```

成功 `200`：逐项结果按输入顺序返回，另汇总新旧受影响日的批量后实收：

```json
{
  "idempotent": false,
  "items": [
    {"index": 0, "op": "submit", "trip_id": "B2", "idempotent": false,
     "trip": {"trip_id": "B2", "account_id": "A001",
              "boarding_at": "2026-10-08T08:00:00+08:00",
              "base_fare_cents": 900, "status": "active",
              "op_day": "2026-10-08", "version": 1}},
    {"index": 1, "op": "correct", "trip_id": "B0", "idempotent": false,
     "trip": {"trip_id": "B0", "...": "...", "version": 2}},
    {"index": 2, "op": "cancel", "trip_id": "B1", "idempotent": false,
     "trip": {"trip_id": "B1", "...": "...", "status": "cancelled"}}
  ],
  "affected_days": [
    {"account_id": "A001", "op_day": "2026-10-08", "charged_cents": 900}
  ]
}
```

- 任一项非法或冲突时**整批回滚**（乘次、换乘组、日结、流水均不改），错误体在统一
  格式上额外给出出错位置，错误码沿用单笔原码（如 `VERSION_CONFLICT`、
  `UNKNOWN_TRIP`、`IDEMPOTENCY_CONFLICT`、`TRIP_NOT_ACTIVE`）：

  ```json
  {"error": {"code": "VERSION_CONFLICT",
             "message": "expected version 2, got 1",
             "index": 1, "trip_id": "B0"}}
  ```
- 批量结构错误同样带 `index`：字段非法沿用 `INVALID_TIME` / `INVALID_FARE` /
  `INVALID_VERSION` 等码；`op` 非法为 `INVALID_OP`；条目结构非法为 `INVALID_BATCH`；
  列表为空/超过 100 项/缺失为 `INVALID_BATCH`；批内乘次号重复为
  `DUPLICATE_TRIP_IN_BATCH`（指向第二次出现的下标）。
- 整批均为幂等重放时顶层 `idempotent=true`，`affected_days` 为空，且不产生任何流水。
- 金额流水结构不变：批量重算产生的成对 `day_reversal` / `day_charge`
  `reason` 含 `batch_settlement`、`trip_id` 为 `null`，仍可经
  `GET /accounts/{account_id}/ledger` 按序翻页查询。

### `GET /trips/{trip_id}` —— 查询乘次状态（含版本）

### `GET /accounts/{account_id}/days/{op_day}` —— 查询运营日

`op_day` 格式 `YYYY-MM-DD`（北京日期）。返回换乘组（含组内乘次明细）、
封顶前组费合计 `sum_group_fare_cents`、封顶值 `daily_cap_cents` 与实收 `charged_cents`。

```bash
curl http://localhost:8080/accounts/A001/days/2026-10-06
```

### `GET /accounts/{account_id}/ledger` —— 游标后流水

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `after_seq` | `0` | 只返回 `seq` 严格大于该值的条目 |
| `limit` | `50` | 每页条数（1–200） |
| `op_day` | 无 | 可选，按运营日过滤 |

```bash
curl "http://localhost:8080/accounts/A001/ledger?after_seq=0&limit=50"
```

响应含 `next_cursor`：为 `null` 表示已取完；否则将其作为下一页的 `after_seq`，
即可保证不重不漏。流水事件类型：

| `event_type` | 含义 |
| --- | --- |
| `trip_posted` / `trip_corrected` / `trip_cancelled` | 乘次生命周期（`delta_cents=0`） |
| `day_reversal` | 重算前冲销旧日实收（`delta_cents` 为负，`charged_after_cents=0`） |
| `day_charge` | 重算后记入新实收（`delta_cents` 为正，`charged_after_cents` 为新实收） |

### `GET /healthz` —— 健康检查

## 费率规则接口（运营方）

### `POST /rules` —— 发布按运营日生效的规则

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `effective_day` | `YYYY-MM-DD` | 生效运营日（北京日期） |
| `transfer_window_minutes` | 正整数 | 换乘时限（分钟，含端点） |
| `daily_cap_cents` | 非负整数 | 单账户单运营日实收封顶（分），允许 `0` |
| `expected_version` | 非负整数 | 该生效日当前版本：未发布为 `0`，重发须为最新版本 |

```bash
curl -X POST http://localhost:8080/rules \
  -H 'Content-Type: application/json' \
  -d '{"effective_day":"2026-10-07",
       "transfer_window_minutes":60,
       "daily_cap_cents":500,
       "expected_version":0}'
```

成功 `200`：

```json
{
  "idempotent": false,
  "rule": {
    "effective_day": "2026-10-07",
    "transfer_window_minutes": 60,
    "daily_cap_cents": 500,
    "version": 1,
    "published": true,
    "source": "published",
    "coverage_from": "2026-10-07",
    "coverage_to": null
  },
  "coverage_from": "2026-10-07",
  "coverage_to": null,
  "recalculated_account_days": 0
}
```

- `version`：发布后该生效日规则版本（首次为 1，内容变更重发递增）；
- `coverage_from` / `coverage_to`：覆盖区间 `[from, to)`，`coverage_to` 为 `null`
  表示向未来无限开放；
- `recalculated_account_days`：本次发布在同一事务内重算的「账户 × 运营日」数量
  （仅统计覆盖区间内有有效乘次的日；向未来发布且区间内无历史乘次时为 `0`）；
- 同内容、同 `expected_version` 重发为幂等：`idempotent=true`、版本不变、
  `recalculated_account_days=0`。

重发示例（携带当前版本 1，得到版本 2）：

```bash
curl -X POST http://localhost:8080/rules \
  -H 'Content-Type: application/json' \
  -d '{"effective_day":"2026-10-07",
       "transfer_window_minutes":45,
       "daily_cap_cents":800,
       "expected_version":1}'
```

### `GET /rules/{op_day}` —— 查询指定运营日适用规则

`op_day` 格式 `YYYY-MM-DD`。返回该日**实际适用**的规则（不晚于该日的最新发布），
以及其覆盖区间。无任何已发布规则时返回默认配置：

```bash
curl http://localhost:8080/rules/2026-10-06
```

```json
{
  "effective_day": null,
  "transfer_window_minutes": 90,
  "daily_cap_cents": 2000,
  "version": 0,
  "published": false,
  "source": "default",
  "coverage_from": null,
  "coverage_to": "2026-10-07"
}
```

`source` 为 `published`（命中发布记录）或 `default`（沿用环境默认）；
默认规则的 `coverage_to` 为首条规则生效日（无任何规则时为 `null`）。

### `GET /rules` —— 列出全部已发布规则

按 `effective_day` 升序返回，结构同 `GET /rules/{op_day}`：

```bash
curl http://localhost:8080/rules
```

```json
{"rules": [ {"effective_day": "2026-10-07", "...": "..."} ]}
```

> 规则发布不改变乘次/账单/流水接口的调用方式、请求字段、响应结构与错误格式；
> 费用（换乘组与日实收）一律按运营日适用规则计算，运营日查询返回的
> `daily_cap_cents` 亦为该日适用封顶。

## 错误响应

统一格式：

```json
{"error": {"code": "VERSION_CONFLICT", "message": "expected version 2, got 1"}}
```

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 404 | `UNKNOWN_TRIP` | 更正/撤销/查询未知乘次 |
| 409 | `IDEMPOTENCY_CONFLICT` | 同号异内容重复提交 |
| 409 | `VERSION_CONFLICT` | 携带的 `expected_version` 不是当前版本 |
| 409 | `TRIP_NOT_ACTIVE` | 更正已撤销的乘次 |
| 409 | `RULE_VERSION_CONFLICT` | 规则发布携带的预期版本与该生效日当前版本不符（未发布须为 0） |
| 422 | `INVALID_TIME` | 时间缺失时区偏移、格式非法、年份越界 |
| 422 | `INVALID_FARE` | 票价不是非负整数 |
| 422 | `INVALID_DATE` | 规则生效日（或路径日期）格式非法、年份越界 |
| 422 | `INVALID_WINDOW` | 换乘时限不是正整数 |
| 422 | `INVALID_CAP` | 封顶分值不是非负整数 |
| 422 | `INVALID_TRIP_ID` / `INVALID_ACCOUNT_ID` / `INVALID_VERSION` | 对应字段非法 |
| 422 | `INVALID_OP` | 批量项 `op` 不是 `submit`/`correct`/`cancel`（错误体含 `index`） |
| 422 | `INVALID_BATCH` | 批量请求结构非法：`operations` 缺失/为空/超过 100 项、条目不是对象、条目内多余字段等 |
| 422 | `DUPLICATE_TRIP_IN_BATCH` | 同一乘次号在一个批次内出现两次（错误体含 `index` 与 `trip_id`） |
| 422 | `INVALID_REQUEST` | 其它请求体校验错误（多余字段等） |

> 批量结算（`POST /trips/batch`）的 4xx 错误体在统一格式上额外携带
> `index`（出错项在 `operations` 中的下标，从 0 起）与 `trip_id`，
> 业务错误码（404/409）与单笔接口完全一致。

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql://fare:farepass@localhost:5432/fareclearing
uvicorn app.main:app --reload --port 8080   # 首次启动自动建表
```

运行测试（使用便携 PostgreSQL，无需本机安装 PG）：

```bash
pip install -r requirements.txt pytest httpx pgserver
pytest
```

## 项目结构

```
app/
  main.py        FastAPI 路由与异常映射
  service.py     归组/重算/幂等/版本控制/流水/费率规则/批量原子结算（事务核心）
  models.py      请求响应模型与输入校验（含批量判别联合）
  db.py          连接池与幂等建表
  schema.sql     表、索引、append-only 触发器
  timeutil.py    北京时间与 04:00 运营日界
  config.py      环境变量配置（默认窗口/封顶）
scripts/examples.sh   端到端调用示例（含规则发布与批量原子结算）
docker-compose.yml / Dockerfile / docker-entrypoint.sh
tests/test_clearing.py 乘次/账单/流水业务规则与并发测试
tests/test_rules.py    按运营日规则发布、覆盖区间、回溯重算与并发测试
tests/test_batch.py    补录批量原子结算、整批回滚定位、幂等重放与并发可见性测试
```
