#!/usr/bin/env bash
# 公交换乘票价清分服务 —— 调用示例（需先启动服务：docker compose up --build）
# 用法：bash scripts/examples.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
ACC="demo-account-001"
j() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 1) 健康检查 =="
curl -s "$BASE/healthz" | j; echo

echo "== 2) 提交三程：08:00(200) 09:30(500) 间隔90分钟同组；11:01(100) 另起一组 =="
for spec in "T1 2026-10-06T08:00:00+08:00 200" \
            "T2 2026-10-06T09:30:00+08:00 500" \
            "T3 2026-10-06T11:01:00+08:00 100"; do
  set -- $spec
  curl -s -X POST "$BASE/trips" -H 'Content-Type: application/json' -d "{
    \"trip_id\": \"$1\", \"account_id\": \"$ACC\",
    \"boarding_at\": \"$2\", \"base_fare_cents\": $3
  }" | j; echo
done

echo "== 3) 同号同内容重传（幂等，idempotent=true，不产生新流水） =="
curl -s -X POST "$BASE/trips" -H 'Content-Type: application/json' -d "{
  \"trip_id\": \"T1\", \"account_id\": \"$ACC\",
  \"boarding_at\": \"2026-10-06T08:00:00+08:00\", \"base_fare_cents\": 200
}" | j; echo

echo "== 4) 异内容冲突（409 IDEMPOTENCY_CONFLICT） =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/trips" \
  -H 'Content-Type: application/json' -d "{
  \"trip_id\": \"T1\", \"account_id\": \"$ACC\",
  \"boarding_at\": \"2026-10-06T08:05:00+08:00\", \"base_fare_cents\": 200
}"

echo "== 5) 查询运营日换乘组与实收（组费 500 + 100 = 600） =="
curl -s "$BASE/accounts/$ACC/days/2026-10-06" | j; echo

echo "== 6) 凭版本更正 T1 票价 200 -> 800（组费变 800，冲销500再记800） =="
curl -s -X POST "$BASE/trips/T1/correct" -H 'Content-Type: application/json' -d "{
  \"account_id\": \"$ACC\", \"boarding_at\": \"2026-10-06T08:00:00+08:00\",
  \"base_fare_cents\": 800, \"expected_version\": 1
}" | j; echo

echo "== 7) 陈旧版本更正（409 VERSION_CONFLICT） =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/trips/T1/correct" \
  -H 'Content-Type: application/json' -d "{
  \"account_id\": \"$ACC\", \"boarding_at\": \"2026-10-06T08:00:00+08:00\",
  \"base_fare_cents\": 800, \"expected_version\": 1
}"

echo "== 8) 撤销 T3（404/409 错误示例见 README） =="
curl -s -X POST "$BASE/trips/T3/cancel?expected_version=1" | j; echo

echo "== 9) 游标翻页查询流水（首页 limit=3，之后用 next_cursor） =="
curl -s "$BASE/accounts/$ACC/ledger?after_seq=0&limit=3" | j; echo

echo "== 10) 查询 2026-10-07 适用规则（尚无发布记录 -> 默认 90 分钟 / 2000 分） =="
curl -s "$BASE/rules/2026-10-07" | j; echo

echo "== 11) 运营方发布 2026-10-07 生效规则：窗口 60 分钟、封顶 500 分（首次 expected_version=0） =="
curl -s -X POST "$BASE/rules" -H 'Content-Type: application/json' -d '{
  "effective_day": "2026-10-07",
  "transfer_window_minutes": 60,
  "daily_cap_cents": 500,
  "expected_version": 0
}' | j; echo

echo "== 12) 在 10-07 提交三程：08:00(600) 09:00(600) 间隔60分钟同组；11:01(100) 另组；组费合计700封顶500 =="
for spec in "R1 2026-10-07T08:00:00+08:00 600" \
            "R2 2026-10-07T09:00:00+08:00 600" \
            "R3 2026-10-07T11:01:00+08:00 100"; do
  set -- $spec
  curl -s -X POST "$BASE/trips" -H 'Content-Type: application/json' -d "{
    \"trip_id\": \"$1\", \"account_id\": \"$ACC\",
    \"boarding_at\": \"$2\", \"base_fare_cents\": $3
  }" > /dev/null
done
curl -s "$BASE/accounts/$ACC/days/2026-10-07" | j; echo

echo "== 13) 回溯发布 2026-10-06 规则（窗口60/封顶500，期望版本0），同事务重算区间内已有乘次 =="
curl -s -X POST "$BASE/rules" -H 'Content-Type: application/json' -d '{
  "effective_day": "2026-10-06",
  "transfer_window_minutes": 60,
  "daily_cap_cents": 500,
  "expected_version": 0
}' | j; echo
echo "-- 10-06 日结：T1(08:00,800)/T2(09:30,500) 间隔90分钟在60分钟窗口下拆成两组，组费 800+500=1300 封顶 500 --"
curl -s "$BASE/accounts/$ACC/days/2026-10-06" | j; echo

echo "== 14) 陈旧预期版本重发规则（409 RULE_VERSION_CONFLICT） =="
curl -s -X POST "$BASE/rules" -H 'Content-Type: application/json' -d '{
  "effective_day": "2026-10-06",
  "transfer_window_minutes": 30,
  "daily_cap_cents": 200,
  "expected_version": 0
}' | j; echo

echo "== 15) 查询指定运营日适用规则与覆盖区间 =="
curl -s "$BASE/rules/2026-10-06" | j; echo
echo "-- 全部已发布规则（按生效日升序） --"
curl -s "$BASE/rules" | j; echo

# ---------------------------------------------------------------
# 清分方补录批量原子结算（POST /trips/batch，1～100 项一次事务）
# ---------------------------------------------------------------
B="demo-batch-001"

echo "== 16) 预置两笔既有乘次（供批量中更正/撤销） =="
for spec in "B0 2026-10-08T09:00:00+08:00 500" \
            "B1 2026-10-08T12:00:00+08:00 700"; do
  set -- $spec
  curl -s -X POST "$BASE/trips" -H 'Content-Type: application/json' -d "{
    \"trip_id\": \"$1\", \"account_id\": \"$B\",
    \"boarding_at\": \"$2\", \"base_fare_cents\": $3
  }" > /dev/null
done
echo "预置完成：B0(09:00,500)、B1(12:00,700)"

echo "== 17) 一次批量原子结算：补录 B2(08:00,900) + 更正 B0(500->600,v1) + 撤销 B1 =="
curl -s -X POST "$BASE/trips/batch" -H 'Content-Type: application/json' -d "{
  \"operations\": [
    {\"op\": \"submit\",  \"trip_id\": \"B2\", \"account_id\": \"$B\",
     \"boarding_at\": \"2026-10-08T08:00:00+08:00\", \"base_fare_cents\": 900},
    {\"op\": \"correct\", \"trip_id\": \"B0\", \"account_id\": \"$B\",
     \"boarding_at\": \"2026-10-08T09:00:00+08:00\", \"base_fare_cents\": 600,
     \"expected_version\": 1},
    {\"op\": \"cancel\",  \"trip_id\": \"B1\", \"expected_version\": 1}
  ]
}" | j; echo
echo "-- 日结：B2(08:00) 与 B0(09:00) 间隔恰为60分钟（10-08 适用第13步发布的规则：窗口60/封顶500），同组组费900封顶实收500；B1 已撤销剔除 --"
curl -s "$BASE/accounts/$B/days/2026-10-08" | j; echo

echo "== 18) 整批幂等重放（同内容提交/同版本更正重放/重复撤销 -> 全 idempotent，无流水） =="
curl -s -X POST "$BASE/trips/batch" -H 'Content-Type: application/json' -d "{
  \"operations\": [
    {\"op\": \"submit\",  \"trip_id\": \"B2\", \"account_id\": \"$B\",
     \"boarding_at\": \"2026-10-08T08:00:00+08:00\", \"base_fare_cents\": 900},
    {\"op\": \"correct\", \"trip_id\": \"B0\", \"account_id\": \"$B\",
     \"boarding_at\": \"2026-10-08T09:00:00+08:00\", \"base_fare_cents\": 600,
     \"expected_version\": 2},
    {\"op\": \"cancel\",  \"trip_id\": \"B1\", \"expected_version\": 1}
  ]
}" | j; echo

echo "== 19) 任一项冲突整批回滚（B0 现为 v2，第 1 项带陈旧 v1 -> 409 VERSION_CONFLICT；第 0/2 项也不落库） =="
curl -s -X POST "$BASE/trips/batch" -H 'Content-Type: application/json' -d "{
  \"operations\": [
    {\"op\": \"submit\", \"trip_id\": \"B8\", \"account_id\": \"$B\",
     \"boarding_at\": \"2026-10-08T20:00:00+08:00\", \"base_fare_cents\": 100},
    {\"op\": \"correct\", \"trip_id\": \"B0\", \"account_id\": \"$B\",
     \"boarding_at\": \"2026-10-08T09:00:00+08:00\", \"base_fare_cents\": 650,
     \"expected_version\": 1},
    {\"op\": \"submit\", \"trip_id\": \"B9\", \"account_id\": \"$B\",
     \"boarding_at\": \"2026-10-08T21:00:00+08:00\", \"base_fare_cents\": 100}
  ]
}" | j; echo
echo "-- B8/B9 均不应存在（整批回滚）；B0 仍为 v2、日结不变 --"
curl -s -o /dev/null -w "GET /trips/B8 -> HTTP %{http_code} (期望 404)\n" "$BASE/trips/B8"
curl -s -o /dev/null -w "GET /trips/B9 -> HTTP %{http_code} (期望 404)\n" "$BASE/trips/B9"
curl -s "$BASE/accounts/$B/days/2026-10-08" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("回滚后日结 charged_cents =", d["charged_cents"], "（仍为 500）")'

echo "== 20) 批次内同一乘次号出现两次（422 DUPLICATE_TRIP_IN_BATCH，定位 index=1） =="
curl -s -X POST "$BASE/trips/batch" -H 'Content-Type: application/json' -d "{
  \"operations\": [
    {\"op\": \"submit\", \"trip_id\": \"DUP\", \"account_id\": \"$B\",
     \"boarding_at\": \"2026-10-08T07:00:00+08:00\", \"base_fare_cents\": 100},
    {\"op\": \"cancel\", \"trip_id\": \"DUP\", \"expected_version\": 1}
  ]
}" | j; echo
