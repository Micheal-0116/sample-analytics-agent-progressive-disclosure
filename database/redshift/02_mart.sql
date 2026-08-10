-- ============================================
-- 治理层 / 数据集市 (Mart Layer) —— Redshift 方言版
-- ============================================
-- 与 database/09_mart.sql 口径逐字一致，只改写 Redshift 不支持的三处构造：
--
--   1. generate_series(d0, d1, interval '1 day')
--      Redshift 的 generate_series 是 **leader-node-only 函数**，不能出现在引用用户表的
--      查询里。改成从一张够长的表上 ROW_NUMBER() 造数列，再 DATEADD 出日期轴。
--
--   2. DISTINCT ON (user_id) ... ORDER BY user_id, attributed_at DESC
--      Postgres 专有语法。改成 ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...) = 1。
--
--   3. (array_agg(placed_at ORDER BY placed_at))[2]
--      Redshift 没有 array_agg 下标取值。改成 ROW_NUMBER() = 2 取第二笔。
--
-- 另外：CREATE INDEX 全部删除（Redshift 没有二级索引，排序由建表时的 SORTKEY 承担）。
-- 口径定义（GMV / 新客 / last_touch 归因 / CAC / 复购）见 09_mart.sql 的头部注释。
-- ============================================

-- --------------------------------------------
-- 1. mart_daily_kpi —— 每日业务大盘（粒度：dt）
-- --------------------------------------------
DROP TABLE IF EXISTS mart_daily_kpi;
CREATE TABLE mart_daily_kpi
DISTSTYLE ALL
SORTKEY(dt)
AS
WITH bounds AS (
    SELECT LEAST(
               (SELECT min(event_time)::date    FROM events),
               (SELECT min(placed_at)::date     FROM orders),
               (SELECT min(registered_at)::date FROM users)
           ) AS d0,
           GREATEST(
               (SELECT max(event_time)::date    FROM events),
               (SELECT max(placed_at)::date     FROM orders),
               (SELECT max(registered_at)::date FROM users)
           ) AS d1
),
nums AS (   -- 代替 generate_series：channel_daily_costs 有 910 行，够撑 910 天日期轴
    SELECT ROW_NUMBER() OVER () - 1 AS n FROM channel_daily_costs
),
spine AS (
    SELECT DATEADD(day, n.n, b.d0)::date AS dt
    FROM nums n CROSS JOIN bounds b
    WHERE DATEADD(day, n.n, b.d0) <= b.d1
),
dau AS (
    SELECT event_time::date AS dt, count(DISTINCT user_id) AS dau
    FROM events GROUP BY 1
),
nu AS (
    SELECT registered_at::date AS dt, count(*) AS new_users
    FROM users GROUP BY 1
),
ord AS (
    SELECT placed_at::date AS dt,
           count(*)                AS orders,
           count(DISTINCT user_id) AS paying_users,
           sum(actual_amount)      AS gmv
    FROM orders
    WHERE status IN ('paid','shipped','delivered')
    GROUP BY 1
),
rf AS (
    SELECT refunded_at::date AS dt, sum(actual_amount) AS refund_amt
    FROM orders WHERE refunded_at IS NOT NULL GROUP BY 1
),
sub AS (
    SELECT start_date::date AS dt, count(*) AS new_subscriptions
    FROM subscriptions GROUP BY 1
)
SELECT s.dt,
       COALESCE(dau.dau, 0)                        AS dau,
       COALESCE(nu.new_users, 0)                   AS new_users,
       COALESCE(ord.orders, 0)                     AS orders,
       COALESCE(ord.paying_users, 0)               AS paying_users,
       COALESCE(ord.gmv, 0)::numeric(14,2)         AS gmv,
       COALESCE(rf.refund_amt, 0)::numeric(14,2)   AS refund_amt,
       COALESCE(sub.new_subscriptions, 0)          AS new_subscriptions
FROM spine s
LEFT JOIN dau ON dau.dt = s.dt
LEFT JOIN nu  ON nu.dt  = s.dt
LEFT JOIN ord ON ord.dt = s.dt
LEFT JOIN rf  ON rf.dt  = s.dt
LEFT JOIN sub ON sub.dt = s.dt;

COMMENT ON TABLE mart_daily_kpi IS
'每日业务大盘。粒度=dt。gmv=有效订单实付合计;dau=events去重用户;新客=注册当天。';

-- --------------------------------------------
-- 2. mart_daily_revenue —— 收入事实表（粒度：日 × 渠道 × 新老客）
-- --------------------------------------------
DROP TABLE IF EXISTS mart_daily_revenue;
CREATE TABLE mart_daily_revenue
DISTSTYLE ALL
SORTKEY(dt)
AS
WITH user_channel AS (   -- last_touch 归因：DISTINCT ON → ROW_NUMBER()=1
    SELECT user_id, channel_id FROM (
        SELECT ua.user_id, ua.channel_id,
               ROW_NUMBER() OVER (PARTITION BY ua.user_id
                                  ORDER BY ua.attributed_at DESC NULLS LAST) AS rn
        FROM user_attributions ua
        WHERE ua.attribution_type = 'last_touch'
    ) t WHERE rn = 1
),
first_order AS (         -- 每用户首笔有效订单
    SELECT order_id, user_id FROM (
        SELECT order_id, user_id,
               ROW_NUMBER() OVER (PARTITION BY user_id
                                  ORDER BY placed_at ASC, order_id ASC) AS rn
        FROM orders WHERE status IN ('paid','shipped','delivered')
    ) t WHERE rn = 1
)
SELECT o.placed_at::date                       AS dt,
       COALESCE(c.channel_id, 0)               AS channel_id,
       COALESCE(c.channel_name, '未归因')      AS channel_name,
       COALESCE(c.channel_type, 'unknown')     AS channel_type,
       (fo.order_id IS NOT NULL)               AS is_new_user,
       count(*)                                AS order_cnt,
       count(DISTINCT o.user_id)               AS paying_user_cnt,
       sum(o.actual_amount)::numeric(14,2)     AS gmv
FROM orders o
LEFT JOIN user_channel uc ON uc.user_id = o.user_id
LEFT JOIN channels c      ON c.channel_id = uc.channel_id
LEFT JOIN first_order fo  ON fo.order_id = o.order_id
WHERE o.status IN ('paid','shipped','delivered')
GROUP BY 1, 2, 3, 4, 5;

COMMENT ON TABLE mart_daily_revenue IS
'收入事实表。粒度=日×渠道×新老客。渠道=last_touch归因;is_new_user=该用户首笔有效订单;gmv=实付。GMV合计可对齐 mart_daily_kpi.gmv。';

-- --------------------------------------------
-- 3. mart_channel_daily —— 渠道效果表（粒度：dt × 渠道）
-- --------------------------------------------
DROP TABLE IF EXISTS mart_channel_daily;
CREATE TABLE mart_channel_daily
DISTSTYLE ALL
SORTKEY(dt)
AS
WITH user_channel AS (
    SELECT user_id, channel_id FROM (
        SELECT ua.user_id, ua.channel_id,
               ROW_NUMBER() OVER (PARTITION BY ua.user_id
                                  ORDER BY ua.attributed_at DESC NULLS LAST) AS rn
        FROM user_attributions ua
        WHERE ua.attribution_type = 'last_touch'
    ) t WHERE rn = 1
),
cost AS (
    SELECT date AS dt, channel_id,
           sum(impressions)         AS impressions,
           sum(clicks)              AS clicks,
           sum(installs)            AS installs,
           sum(cost)::numeric(14,2) AS cost
    FROM channel_daily_costs
    GROUP BY 1, 2
),
new_user_by_channel AS (
    SELECT u.registered_at::date AS dt, uc.channel_id,
           count(*) AS new_users_attributed
    FROM users u
    JOIN user_channel uc ON uc.user_id = u.user_id
    GROUP BY 1, 2
),
gmv_by_channel AS (
    SELECT o.placed_at::date AS dt, uc.channel_id,
           sum(o.actual_amount)::numeric(14,2) AS gmv_attributed
    FROM orders o
    JOIN user_channel uc ON uc.user_id = o.user_id
    WHERE o.status IN ('paid','shipped','delivered')
    GROUP BY 1, 2
),
keys AS (
    SELECT dt, channel_id FROM cost
    UNION SELECT dt, channel_id FROM new_user_by_channel
    UNION SELECT dt, channel_id FROM gmv_by_channel
)
SELECT k.dt,
       c.channel_id, c.channel_name, c.channel_type,
       COALESCE(cost.cost, 0)::numeric(14,2)        AS cost,
       COALESCE(cost.impressions, 0)                AS impressions,
       COALESCE(cost.clicks, 0)                     AS clicks,
       COALESCE(cost.installs, 0)                   AS installs,
       COALESCE(nu.new_users_attributed, 0)         AS new_users_attributed,
       COALESCE(g.gmv_attributed, 0)::numeric(14,2) AS gmv_attributed
FROM keys k
JOIN channels c ON c.channel_id = k.channel_id
LEFT JOIN cost                   ON cost.dt = k.dt AND cost.channel_id = k.channel_id
LEFT JOIN new_user_by_channel nu ON nu.dt = k.dt   AND nu.channel_id = k.channel_id
LEFT JOIN gmv_by_channel g       ON g.dt = k.dt    AND g.channel_id = k.channel_id;

COMMENT ON TABLE mart_channel_daily IS
'渠道效果表。粒度=日×渠道。cost来自channel_daily_costs;归因=last_touch。CAC=cost/new_users_attributed;ROI=gmv_attributed/cost。';

-- --------------------------------------------
-- 4. mart_user_summary —— 用户汇总表（粒度：用户）
-- --------------------------------------------
DROP TABLE IF EXISTS mart_user_summary;
CREATE TABLE mart_user_summary
DISTKEY(user_id)
SORTKEY(register_date)
AS
WITH user_channel AS (
    SELECT user_id, channel_id FROM (
        SELECT ua.user_id, ua.channel_id,
               ROW_NUMBER() OVER (PARTITION BY ua.user_id
                                  ORDER BY ua.attributed_at DESC NULLS LAST) AS rn
        FROM user_attributions ua
        WHERE ua.attribution_type = 'last_touch'
    ) t WHERE rn = 1
),
paid AS (
    SELECT user_id,
           min(placed_at)                    AS first_paid_ts,
           count(*)                          AS paid_order_cnt,
           sum(actual_amount)::numeric(14,2) AS total_gmv
    FROM orders
    WHERE status IN ('paid','shipped','delivered')
    GROUP BY user_id
),
second_order AS (   -- 第二笔有效订单：array_agg(...)[2] → ROW_NUMBER()=2
    SELECT user_id, placed_at AS second_paid_ts FROM (
        SELECT user_id, placed_at,
               ROW_NUMBER() OVER (PARTITION BY user_id
                                  ORDER BY placed_at ASC) AS rn
        FROM orders WHERE status IN ('paid','shipped','delivered')
    ) t WHERE rn = 2
),
last_act AS (
    SELECT user_id, max(event_time) AS last_active FROM events GROUP BY user_id
)
SELECT u.user_id,
       u.registered_at::date                    AS register_date,
       COALESCE(c.channel_name, '未归因')       AS register_channel,
       p.first_paid_ts::date                    AS first_paid_date,
       COALESCE(p.paid_order_cnt, 0)            AS paid_order_cnt,
       COALESCE(p.total_gmv, 0)::numeric(14,2)  AS total_gmv,
       (so.second_paid_ts IS NOT NULL
        AND so.second_paid_ts <= DATEADD(day, 30, p.first_paid_ts)) AS is_repurchaser_30d,
       la.last_active::date                     AS last_active_date
FROM users u
LEFT JOIN user_channel uc ON uc.user_id = u.user_id
LEFT JOIN channels c      ON c.channel_id = uc.channel_id
LEFT JOIN paid p          ON p.user_id = u.user_id
LEFT JOIN second_order so ON so.user_id = u.user_id
LEFT JOIN last_act la     ON la.user_id = u.user_id;

COMMENT ON TABLE mart_user_summary IS
'用户汇总表。粒度=用户。register_channel=last_touch;复购口径=首单后30天内第二笔有效订单。复购率=avg(is_repurchaser_30d) over 有首单用户。';

-- meta_snapshot：数据"今天"的锚点（静态样本铁律：以 max(dt) 为今天，禁用 current_date）
DROP TABLE IF EXISTS meta_snapshot;
CREATE TABLE meta_snapshot
DISTSTYLE ALL
AS
SELECT (SELECT max(dt) FROM mart_daily_kpi) AS as_of_date,
       (SELECT min(dt) FROM mart_daily_kpi) AS data_start;
