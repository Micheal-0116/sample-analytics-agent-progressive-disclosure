-- ═══ L5 分布真实性 + 治理层对账 ═══
-- 前四层查的是「有没有矛盾」，这一层查「像不像真的」。
-- 一份内部无矛盾但分布退化的数据，跑得出漂亮报表，得出的结论全是假的：
-- 均匀分布的事件流会让任何漏斗算出 ~100% 转化，不衰减的活跃会让留存算出 100%。
-- 这一层没有 PASS/FAIL，要跟真实业务的经验形状对照着读，判断写在注释里。

-- L5.1 mart 层与明细对账：逐日 GMV 是否一致
-- mart 口径 = sum(orders.actual_amount) where status in (paid,shipped,delivered)
-- 也就是 mart 采信订单头，不采信 order_items（见 L4.1）
WITH d AS (SELECT placed_at::date AS dt, sum(actual_amount) AS gmv
           FROM orders WHERE status IN ('paid','shipped','delivered') GROUP BY 1),
     r AS (SELECT dt, sum(gmv) AS gmv FROM mart_daily_revenue GROUP BY 1)
SELECT count(*)                                                     AS days,
       sum(CASE WHEN k.dt IS NULL THEN 1 ELSE 0 END)                AS missing_in_kpi,
       sum(CASE WHEN abs(k.gmv - d.gmv) > 0.01 THEN 1 ELSE 0 END)   AS kpi_gmv_ne,
       sum(CASE WHEN abs(r.gmv - d.gmv) > 0.01 THEN 1 ELSE 0 END)   AS revenue_gmv_ne,
       round(sum(d.gmv), 0) AS detail_total,
       round(sum(k.gmv), 0) AS kpi_total,
       round(sum(r.gmv), 0) AS revenue_total
FROM d LEFT JOIN mart_daily_kpi k ON k.dt = d.dt LEFT JOIN r ON r.dt = d.dt;

-- L5.2 日订单量波动。真实电商日订单变异系数通常 0.2~0.4；
-- 明显低于 0.1 说明每天几乎一样，是恒定速率抽样的痕迹
WITH d AS (SELECT placed_at::date AS dt, count(*) AS o FROM orders GROUP BY 1)
SELECT count(*) AS days, min(o) AS min_ord, max(o) AS max_ord,
       round(avg(o), 1) AS avg_ord, round(stddev(o), 1) AS sd,
       round(stddev(o) / avg(o), 3) AS cv
FROM d;

-- L5.3 周内效应。真实消费应周末高于工作日（0=周日, 6=周六）
SELECT EXTRACT(dow FROM placed_at)::int AS dow, count(*) AS orders,
       round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct
FROM orders GROUP BY 1 ORDER BY 1;

-- L5.4 小时作息。真实应凌晨谷底、午间小高峰、晚间主峰
SELECT EXTRACT(hour FROM placed_at)::int AS hr, count(*) AS orders
FROM orders GROUP BY 1 ORDER BY 1;

-- L5.5 消费集中度（重尾）。真实电商 top 10% 用户通常占 GMV 50%~70%；
-- 接近 10%/40% 说明消费额基本同分布抽样，缺少高价值用户尾巴
WITH u AS (SELECT user_id, sum(actual_amount) AS sp FROM orders
           WHERE status IN ('paid','shipped','delivered') GROUP BY 1),
     r AS (SELECT sp, NTILE(100) OVER (ORDER BY sp DESC) AS pct FROM u)
SELECT round(100.0 * sum(CASE WHEN pct  = 1  THEN sp END) / sum(sp), 2) AS top1pct_gmv_share,
       round(100.0 * sum(CASE WHEN pct <= 10 THEN sp END) / sum(sp), 2) AS top10pct_gmv_share,
       round(100.0 * sum(CASE WHEN pct <= 50 THEN sp END) / sum(sp), 2) AS top50pct_gmv_share
FROM r;

-- L5.6 事件漏斗形状。真实应逐级递减且量级分层：
--   view_home >> view_product > add_to_cart > begin_checkout > purchase
-- 各事件量接近相等 = 均匀抽样，漏斗分析在这份数据上无意义
SELECT event_name, count(*) AS n, count(DISTINCT user_id) AS users
FROM events
WHERE event_name IN ('view_home','view_product','add_to_cart','begin_checkout','purchase')
GROUP BY 1 ORDER BY 2 DESC;

-- L5.7 全部事件的量级差。max/min 接近 1 即为均匀分布
SELECT count(*) AS event_types, min(n) AS min_n, max(n) AS max_n,
       round(max(n)::numeric / min(n), 3) AS max_over_min
FROM (SELECT event_name, count(*) AS n FROM events GROUP BY 1);

-- L5.8 留存曲线。取一周注册队列，看注册后第 N 天还活跃的人数。
-- 真实应单调递减（D1 约 40%、D7 约 20%）；持平或上升说明活跃事件与
-- 用户生命周期无关，留存类指标在这份数据上不可用
WITH coh AS (SELECT user_id, registered_at::date AS d0 FROM users
             WHERE registered_at::date BETWEEN '2025-11-03' AND '2025-11-09'),
     act AS (SELECT DISTINCT e.user_id, e.event_time::date AS d
             FROM events e JOIN coh c2 ON c2.user_id = e.user_id)
SELECT DATEDIFF(day, c.d0, a.d) AS lag_days, count(DISTINCT a.user_id) AS active_users
FROM coh c JOIN act a ON a.user_id = c.user_id
WHERE DATEDIFF(day, c.d0, a.d) BETWEEN 0 AND 14
GROUP BY 1 ORDER BY 1;

-- L5.9 派生层是否真的做了清洗。dwd_events_app 的定义是
--   SELECT ... FROM events WHERE user_id IS NOT NULL
-- 行数与 events 相同即说明过滤条件命中 0 行，这张表是整表复制而非清洗层
SELECT (SELECT count(*) FROM events)                          AS events_rows,
       (SELECT count(*) FROM dwd_events_app)                  AS dwd_rows,
       (SELECT count(*) FROM events WHERE user_id IS NULL)     AS events_null_user,
       (SELECT count(*) FROM orders WHERE status IN ('paid','shipped','delivered')) AS orders_valid,
       (SELECT count(*) FROM dwd_orders_valid)                AS dwd_orders_valid_rows;
