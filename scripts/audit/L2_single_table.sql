-- ═══ L2 单表完整性：每张表自己站不站得住 ═══
-- Redshift 不强制 PK / FK / NOT NULL（见 database/redshift/01_tables.sql，
-- 里面一条约束都没有），所以这些性质全靠生成器自觉，必须查。

-- L2.1 主键唯一性
SELECT 'users.user_id' AS pk, count(*) AS n, count(DISTINCT user_id) AS uniq,
       CASE WHEN count(*) = count(DISTINCT user_id) THEN 'PASS' ELSE 'FAIL' END AS verdict
FROM users
UNION ALL SELECT 'orders.order_id', count(*), count(DISTINCT order_id),
       CASE WHEN count(*) = count(DISTINCT order_id) THEN 'PASS' ELSE 'FAIL' END FROM orders
UNION ALL SELECT 'order_items.item_id', count(*), count(DISTINCT item_id),
       CASE WHEN count(*) = count(DISTINCT item_id) THEN 'PASS' ELSE 'FAIL' END FROM order_items
UNION ALL SELECT 'payments.payment_id', count(*), count(DISTINCT payment_id),
       CASE WHEN count(*) = count(DISTINCT payment_id) THEN 'PASS' ELSE 'FAIL' END FROM payments
UNION ALL SELECT 'events.event_id', count(*), count(DISTINCT event_id),
       CASE WHEN count(*) = count(DISTINCT event_id) THEN 'PASS' ELSE 'FAIL' END FROM events
UNION ALL SELECT 'posts.post_id', count(*), count(DISTINCT post_id),
       CASE WHEN count(*) = count(DISTINCT post_id) THEN 'PASS' ELSE 'FAIL' END FROM posts
UNION ALL SELECT 'user_profiles.user_id', count(*), count(DISTINCT user_id),
       CASE WHEN count(*) = count(DISTINCT user_id) THEN 'PASS' ELSE 'FAIL' END FROM user_profiles
ORDER BY 1;

-- L2.2 枚举越界：卡片里写的取值之外还有没有别的值
-- 取值来源 knowledge/domains/<域>/<表>.md 的「字段枚举值」段
SELECT 'users.registration_source' AS col,
       sum(CASE WHEN registration_source NOT IN ('app','web','mini_program','h5') THEN 1 ELSE 0 END) AS bad
FROM users
UNION ALL SELECT 'users.status',
       sum(CASE WHEN status NOT IN ('active','inactive','banned') THEN 1 ELSE 0 END) FROM users
UNION ALL SELECT 'users.user_level',
       sum(CASE WHEN user_level < 1 OR user_level > 5 THEN 1 ELSE 0 END) FROM users
UNION ALL SELECT 'orders.status',
       sum(CASE WHEN status NOT IN ('pending','paid','shipped','delivered','cancelled','refunded') THEN 1 ELSE 0 END) FROM orders
UNION ALL SELECT 'payments.status',
       sum(CASE WHEN status NOT IN ('pending','success','failed','refunded') THEN 1 ELSE 0 END) FROM payments
UNION ALL SELECT 'user_coupons.status',
       sum(CASE WHEN status NOT IN ('unused','used','expired') THEN 1 ELSE 0 END) FROM user_coupons
UNION ALL SELECT 'posts.status',
       sum(CASE WHEN status NOT IN ('published','draft','hidden','deleted') THEN 1 ELSE 0 END) FROM posts
ORDER BY 1;

-- L2.3 关键列空值（这些列为空会让下游 JOIN 静默丢行）
SELECT 'users.user_id'        AS col, sum(CASE WHEN user_id  IS NULL THEN 1 ELSE 0 END) AS nulls FROM users
UNION ALL SELECT 'orders.user_id',      sum(CASE WHEN user_id  IS NULL THEN 1 ELSE 0 END) FROM orders
UNION ALL SELECT 'orders.actual_amount',sum(CASE WHEN actual_amount IS NULL THEN 1 ELSE 0 END) FROM orders
UNION ALL SELECT 'orders.placed_at',    sum(CASE WHEN placed_at IS NULL THEN 1 ELSE 0 END) FROM orders
UNION ALL SELECT 'events.user_id',      sum(CASE WHEN user_id  IS NULL THEN 1 ELSE 0 END) FROM events
UNION ALL SELECT 'events.session_id',   sum(CASE WHEN session_id IS NULL THEN 1 ELSE 0 END) FROM events
UNION ALL SELECT 'order_items.product_id', sum(CASE WHEN product_id IS NULL THEN 1 ELSE 0 END) FROM order_items
ORDER BY 1;

-- L2.4 orders 表内金额公式：actual = total - discount + shipping
SELECT count(*) AS ord,
       sum(CASE WHEN abs(actual_amount - (total_amount - discount_amount + shipping_fee)) > 0.01
                THEN 1 ELSE 0 END) AS formula_bad,
       sum(CASE WHEN total_amount < 0 OR actual_amount < 0 THEN 1 ELSE 0 END) AS negative_amt
FROM orders;

-- L2.5 时序顺序：同一行内的时间戳必须递增
SELECT 'users: last_active < registered'      AS rule,
       sum(CASE WHEN last_active_at < registered_at THEN 1 ELSE 0 END) AS bad FROM users
UNION ALL SELECT 'orders: paid < placed',
       sum(CASE WHEN paid_at IS NOT NULL AND paid_at < placed_at THEN 1 ELSE 0 END) FROM orders
UNION ALL SELECT 'orders: shipped < paid',
       sum(CASE WHEN shipped_at IS NOT NULL AND paid_at IS NOT NULL AND shipped_at < paid_at THEN 1 ELSE 0 END) FROM orders
UNION ALL SELECT 'orders: delivered < shipped',
       sum(CASE WHEN delivered_at IS NOT NULL AND shipped_at IS NOT NULL AND delivered_at < shipped_at THEN 1 ELSE 0 END) FROM orders
UNION ALL SELECT 'sessions: end < start',
       sum(CASE WHEN end_time < start_time THEN 1 ELSE 0 END) FROM sessions
UNION ALL SELECT 'user_coupons: used < received',
       sum(CASE WHEN used_at IS NOT NULL AND used_at < received_at THEN 1 ELSE 0 END) FROM user_coupons
ORDER BY 1;

-- L2.6 sessions.duration_seconds 与起止时间差是否吻合
SELECT count(*) AS sess,
       sum(CASE WHEN abs(duration_seconds - DATEDIFF(second, start_time, end_time)) > 1
                THEN 1 ELSE 0 END) AS duration_ne
FROM sessions;
