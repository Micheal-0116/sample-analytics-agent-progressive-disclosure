-- ═══ L1 体检：库里有什么，覆盖到哪一天 ═══
-- 只看规模和边界，不做任何一致性判断。这层挂了说明装载没跑完，后面几层不用跑。

-- L1.1 按层统计表数与行数（base=35 原始表，derived=8 派生表，mart=4 治理表）
-- 注意：文档里那个「7992 万」只等于 base 一层，全库真实总量是 base+derived+mart
SELECT CASE
         WHEN "table" IN ('dwd_orders_valid','dwd_events_app','dws_user_daily',
                          'dws_channel_weekly','fin_daily_revenue','growth_daily_gmv',
                          'orders_backup_20251201','tmp_campaign_roi_analysis')
              THEN '2_derived'
         WHEN "table" LIKE 'mart_%'      THEN '3_mart'
         WHEN "table" =    'meta_snapshot' THEN '4_meta'
         ELSE '1_base'
       END                      AS tier,
       count(*)                 AS tbls,
       sum(tbl_rows)::bigint    AS rows
FROM svv_table_info WHERE schema = 'public'
GROUP BY 1 ORDER BY 1;

-- L1.2 表清单与行数（降序）。留意行数完全相同的表：那通常是整表复制，不是清洗
SELECT "table" AS tbl, tbl_rows::bigint AS rows
FROM svv_table_info WHERE schema = 'public'
ORDER BY tbl_rows DESC;

-- L1.3 空表检查（装载漏表会在这里现形）
SELECT count(*) AS empty_tables,
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS verdict
FROM svv_table_info WHERE schema = 'public' AND tbl_rows = 0;

-- L1.4 快照边界：所有「最近 / 上周 / 本月」的时间基准
SELECT * FROM meta_snapshot;

-- L1.5 核心事实表的时间边界，与 L1.4 对照。超出快照日期即为越界
SELECT 'orders'   AS tbl, min(placed_at)::date     AS d0, max(placed_at)::date     AS d1, count(*) AS n FROM orders
UNION ALL SELECT 'events',   min(event_time)::date,    max(event_time)::date,    count(*) FROM events
UNION ALL SELECT 'sessions', min(start_time)::date,    max(start_time)::date,    count(*) FROM sessions
UNION ALL SELECT 'users',    min(registered_at)::date, max(registered_at)::date, count(*) FROM users
ORDER BY 1;
