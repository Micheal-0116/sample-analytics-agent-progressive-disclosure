-- ═══ L4 业务语义一致性：数字之间讲不讲得通 ═══
-- L2/L3 过了只说明「格式对、ID 对」。这一层问的是：两张表说的是不是同一件事，
-- 以及 knowledge/ 卡片里写的业务规则在数据里成不成立。
-- 这是生成数据最容易塌的一层，因为它要求跨表闭环，而逐表独立抽样天然做不到。

-- L4.1 订单头 vs 订单明细：total_amount 应等于明细 actual_amount 之和
-- 差值双向说明两边独立生成；单向且固定说明只是漏加某一项（如运费）
WITH it AS (SELECT order_id, sum(actual_amount) AS items_amt, count(*) AS n_items
            FROM order_items GROUP BY 1)
SELECT count(*)                                       AS ord,
       sum(CASE WHEN abs(o.total_amount - i.items_amt) > 0.01 THEN 1 ELSE 0 END) AS amt_ne,
       round(avg(o.total_amount), 2)                  AS avg_head,
       round(avg(i.items_amt), 2)                     AS avg_items,
       round(avg(o.total_amount - i.items_amt), 2)    AS avg_diff,
       round(min(o.total_amount - i.items_amt), 2)    AS min_diff,
       round(max(o.total_amount - i.items_amt), 2)    AS max_diff,
       sum(CASE WHEN o.total_amount > i.items_amt THEN 1 ELSE 0 END) AS head_gt_items
FROM orders o JOIN it i ON i.order_id = o.order_id;

-- L4.2 订单声明的 item_count vs 明细真实行数
WITH it AS (SELECT order_id, count(*) AS n_items FROM order_items GROUP BY 1)
SELECT count(*) AS ord,
       sum(CASE WHEN o.item_count <> i.n_items THEN 1 ELSE 0 END) AS cnt_ne,
       round(avg(o.item_count), 2) AS avg_declared,
       round(avg(i.n_items), 2)    AS avg_real
FROM orders o JOIN it i ON i.order_id = o.order_id;

-- L4.3 冗余计数器 vs 真实明细行数（posts.like_count / comment_count）
-- 冗余计数器是最常见的静默错误源：报表读它，明细算另一个数，两边永远对不上
WITH a AS (SELECT post_id, count(*) AS n FROM post_likes    GROUP BY 1),
     c AS (SELECT post_id, count(*) AS n FROM post_comments GROUP BY 1)
SELECT count(*) AS posts,
       sum(CASE WHEN p.like_count    <> COALESCE(a.n, 0) THEN 1 ELSE 0 END) AS like_ne,
       round(avg(p.like_count), 1)        AS declared_likes,
       round(avg(COALESCE(a.n, 0)), 1)    AS real_likes,
       sum(CASE WHEN p.comment_count <> COALESCE(c.n, 0) THEN 1 ELSE 0 END) AS cmt_ne,
       round(avg(p.comment_count), 1)     AS declared_cmt,
       round(avg(COALESCE(c.n, 0)), 1)    AS real_cmt
FROM posts p LEFT JOIN a ON a.post_id = p.post_id LEFT JOIN c ON c.post_id = p.post_id;

-- L4.4 冗余计数器 vs 真实明细行数（sessions.event_count）
WITH a AS (SELECT session_id, count(*) AS n FROM events GROUP BY 1)
SELECT count(*) AS sess,
       sum(CASE WHEN s.event_count <> COALESCE(a.n, 0) THEN 1 ELSE 0 END) AS ev_cnt_ne,
       round(avg(s.event_count), 2)     AS declared_ev,
       round(avg(COALESCE(a.n, 0)), 2)  AS real_ev
FROM sessions s LEFT JOIN a ON a.session_id = s.session_id;

-- L4.5 user_level 的业务定义是否成立
-- knowledge/domains/user/users.md 写：4 = 累计消费>=1000，5 = 累计消费>=10000 或 VIP
-- 若各 level 的 avg_spend 相近，说明等级与消费无关（等级是独立抽的）
WITH spend AS (
  SELECT u.user_id, u.user_level, u.is_vip,
         COALESCE(sum(CASE WHEN o.status IN ('paid','shipped','delivered')
                           THEN o.actual_amount END), 0) AS sp
  FROM users u LEFT JOIN orders o ON o.user_id = u.user_id
  GROUP BY 1, 2, 3
)
SELECT user_level,
       count(*)                                       AS users,
       sum(CASE WHEN is_vip THEN 1 ELSE 0 END)        AS vips,
       round(avg(sp), 0)                              AS avg_spend,
       round(max(sp), 0)                              AS max_spend,
       sum(CASE WHEN sp >= 1000  THEN 1 ELSE 0 END)   AS n_ge_1k,
       sum(CASE WHEN sp >= 10000 THEN 1 ELSE 0 END)   AS n_ge_10k,
       round(100.0 * sum(CASE WHEN sp >= 1000 THEN 1 ELSE 0 END) / count(*), 1) AS pct_ge_1k
FROM spend GROUP BY 1 ORDER BY 1;

-- L4.6 支付覆盖与生命周期时间戳
SELECT '已付订单缺 success 支付记录' AS rule,
       (SELECT count(*) FROM orders o WHERE o.status IN ('paid','shipped','delivered')
          AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id AND p.status = 'success')) AS bad
UNION ALL SELECT 'success 支付金额 <> 订单 actual_amount',
       (SELECT count(*) FROM payments p JOIN orders o ON o.order_id = p.order_id
        WHERE p.status = 'success' AND abs(p.amount - o.actual_amount) > 0.01)
UNION ALL SELECT 'status=refunded 但 refunded_at 为空',
       (SELECT count(*) FROM orders WHERE status = 'refunded' AND refunded_at IS NULL)
UNION ALL SELECT 'status=cancelled 但 cancelled_at 为空',
       (SELECT count(*) FROM orders WHERE status = 'cancelled' AND cancelled_at IS NULL)
UNION ALL SELECT 'status=pending 但已有 success 支付',
       (SELECT count(*) FROM orders o WHERE o.status = 'pending'
          AND EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id AND p.status = 'success'))
UNION ALL SELECT 'status=paid 之后 但 paid_at 为空',
       (SELECT count(*) FROM orders WHERE status IN ('paid','shipped','delivered') AND paid_at IS NULL)
ORDER BY 1;

-- L4.7 优惠券核销 vs 订单：一单最多用一张主券，used 券数不该远超订单数
SELECT (SELECT count(*) FROM user_coupons WHERE status = 'used')                    AS used_coupons,
       (SELECT count(DISTINCT order_id) FROM user_coupons WHERE order_id IS NOT NULL) AS distinct_orders_cited,
       (SELECT count(*) FROM orders)                                                AS total_orders,
       (SELECT count(*) FROM orders WHERE coupon_id IS NOT NULL)                    AS orders_declaring_coupon,
       round((SELECT count(*)::numeric FROM user_coupons WHERE status = 'used')
             / NULLIF((SELECT count(DISTINCT order_id) FROM user_coupons WHERE order_id IS NOT NULL), 0), 2)
                                                                                    AS coupons_per_order;

-- L4.8 行为事件 vs 事实表：purchase 事件数应 >= 有效订单数（一单至少一个事件）
SELECT (SELECT count(*) FROM events WHERE event_name = 'purchase')   AS ev_purchase,
       (SELECT count(*) FROM orders WHERE status IN ('paid','shipped','delivered')) AS real_paid_orders,
       (SELECT count(*) FROM events WHERE event_name = 'use_coupon') AS ev_use_coupon,
       (SELECT count(*) FROM user_coupons WHERE status = 'used')     AS real_used_coupons,
       (SELECT count(*) FROM events WHERE event_name = 'register')   AS ev_register,
       (SELECT count(*) FROM users)                                 AS real_users;
