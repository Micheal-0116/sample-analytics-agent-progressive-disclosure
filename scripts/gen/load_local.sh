#!/bin/bash
# 把 scripts/gen/main.py 生成的 CSV 灌进一个本地 scratch 库，用外键约束验引用完整性。
#
# 这是垂直切片的验证器：Postgres 的 FK / PK / NOT NULL 约束会拒收任何引用错位的数据，
# 比自己写检查更彻底。灌完再跑 mart + 派生层 CTAS，最后与生成器的预期值清单对账。
#
# 用法：
#   scripts/gen/load_local.sh <生成目录> [数据库名]
set -euo pipefail

GEN_DIR="${1:?用法: load_local.sh <生成目录> [dbname]}"
DB="${2:-app_analytics_gen}"
PGBIN="${PGBIN:-/opt/homebrew/opt/postgresql@16/bin}"
PGPORT="${PGPORT:-5433}"
PGUSER="${PGUSER:-postgres}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(cd "$HERE/../.." && pwd)"
DDL="$PROJ/database"
DIM_CSV="$PROJ/data/csv"

psql() { "$PGBIN/psql" -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER" -d "$DB" -v ON_ERROR_STOP=1 "$@"; }

echo "[1/6] 重建 $DB"
"$PGBIN/psql" -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER" -d postgres -q \
  -c "DROP DATABASE IF EXISTS $DB" -c "CREATE DATABASE $DB"

echo "[2/6] 建表 (01-08)"
for f in "$DDL"/0[1-8]_*.sql; do psql -q -f "$f"; done

# 维度表：本轮不重新生成，直接用现有 CSV（量级 2.4 万行，且 eval 金标依赖它们）
echo "[3/6] 灌维度表"
DIMS=(categories products product_tags channels event_definitions user_segments
      campaigns coupons banners ab_tests ab_test_variants
      ad_campaigns ad_creatives channel_daily_costs)
for t in "${DIMS[@]}"; do
  [ -f "$DIM_CSV/$t.csv" ] && psql -q -c "\copy $t FROM '$DIM_CSV/$t.csv' WITH CSV HEADER"
done

echo "[4/6] 灌事实表（外键约束在此把关）"
FACTS=(users user_profiles user_devices user_segment_members
       sessions events page_views
       posts post_likes post_comments post_shares user_follows user_messages
       orders order_items payments
       user_attributions user_coupons push_notifications ab_test_assignments
       subscriptions)
for t in "${FACTS[@]}"; do
  f="$GEN_DIR/$t.csv"
  if [ -f "$f" ]; then
    psql -q -c "\copy $t FROM '$f' WITH CSV HEADER"
    printf "  %-24s ok\n" "$t"
  else
    printf "  %-24s (无文件,跳过)\n" "$t"
  fi
done

echo "[5/6] 重置序列 + mart + 派生层"
psql -q -t -c "
SELECT 'SELECT setval(''' || pg_get_serial_sequence(t.table_name, c.column_name) || ''', COALESCE((SELECT MAX(' || c.column_name || ') FROM ' || t.table_name || '),1));'
FROM information_schema.tables t
JOIN information_schema.columns c ON c.table_name=t.table_name AND c.table_schema='public'
WHERE t.table_schema='public' AND pg_get_serial_sequence(t.table_name, c.column_name) IS NOT NULL
" | grep -v '^\s*$' > /tmp/reseq.sql || true
psql -q -f /tmp/reseq.sql
for f in "$DDL"/09_*.sql "$DDL"/10_*.sql; do
  [ -f "$f" ] && { echo "  $(basename "$f")"; psql -q -f "$f"; }
done
psql -q -f "$PROJ/scripts/snapshot_date.sql"

echo "[6/6] 统计"
psql -c "SELECT
  (SELECT count(*) FROM users) AS users,
  (SELECT count(*) FROM orders) AS orders,
  (SELECT count(*) FROM events) AS events,
  (SELECT count(*) FROM mart_daily_kpi) AS mart_kpi,
  (SELECT count(*) FROM dwd_orders_valid) AS dwd_valid;"
psql -c "SELECT as_of_date, data_start FROM meta_snapshot;"
echo "完成：$DB"
