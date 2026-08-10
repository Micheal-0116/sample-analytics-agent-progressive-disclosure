-- ═══ L3 引用完整性：外键指向的行存不存在 ═══
-- Redshift 不强制 FK。这一层查孤儿行：事实表引用了一个维表里没有的 ID。
-- 孤儿行的危害是静默的：INNER JOIN 会直接少算，不报错。
--
-- 每条检查分三列报，不要合并：
--   rows     该表总行数
--   null_fk  外键为 NULL（可空外键的正常情况，例如事务型推送不挂运营活动）
--   dangling 外键非空但维表里查不到（真正的引用完整性缺陷）
-- 只看 dangling 判 PASS/FAIL。把 null_fk 算进孤儿是常见假阳性：
-- 本文件初版就犯过，push_notifications 有 18% 行的 campaign_id 为 NULL，
-- 被 LEFT JOIN 报成 76 万孤儿，实际真正悬空 0 行。

-- L3.1 用户维度
SELECT 'orders.user_id -> users' AS fk, count(*) AS rows,
       sum(CASE WHEN o.user_id IS NULL THEN 1 ELSE 0 END) AS null_fk,
       sum(CASE WHEN o.user_id IS NOT NULL AND u.user_id IS NULL THEN 1 ELSE 0 END) AS dangling
FROM orders o LEFT JOIN users u ON u.user_id = o.user_id
UNION ALL
SELECT 'events.user_id -> users', count(*),
       sum(CASE WHEN e.user_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN e.user_id IS NOT NULL AND u.user_id IS NULL THEN 1 ELSE 0 END)
FROM events e LEFT JOIN users u ON u.user_id = e.user_id
UNION ALL
SELECT 'sessions.user_id -> users', count(*),
       sum(CASE WHEN s.user_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN s.user_id IS NOT NULL AND u.user_id IS NULL THEN 1 ELSE 0 END)
FROM sessions s LEFT JOIN users u ON u.user_id = s.user_id
UNION ALL
SELECT 'user_profiles.user_id -> users', count(*),
       sum(CASE WHEN r.user_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN r.user_id IS NOT NULL AND u.user_id IS NULL THEN 1 ELSE 0 END)
FROM user_profiles r LEFT JOIN users u ON u.user_id = r.user_id
UNION ALL
SELECT 'user_devices.user_id -> users', count(*),
       sum(CASE WHEN d.user_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN d.user_id IS NOT NULL AND u.user_id IS NULL THEN 1 ELSE 0 END)
FROM user_devices d LEFT JOIN users u ON u.user_id = d.user_id
ORDER BY 1;

-- L3.2 交易链路
SELECT 'order_items.order_id -> orders' AS fk, count(*) AS rows,
       sum(CASE WHEN i.order_id IS NULL THEN 1 ELSE 0 END) AS null_fk,
       sum(CASE WHEN i.order_id IS NOT NULL AND o.order_id IS NULL THEN 1 ELSE 0 END) AS dangling
FROM order_items i LEFT JOIN orders o ON o.order_id = i.order_id
UNION ALL
SELECT 'order_items.product_id -> products', count(*),
       sum(CASE WHEN i.product_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN i.product_id IS NOT NULL AND p.product_id IS NULL THEN 1 ELSE 0 END)
FROM order_items i LEFT JOIN products p ON p.product_id = i.product_id
UNION ALL
SELECT 'payments.order_id -> orders', count(*),
       sum(CASE WHEN y.order_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN y.order_id IS NOT NULL AND o.order_id IS NULL THEN 1 ELSE 0 END)
FROM payments y LEFT JOIN orders o ON o.order_id = y.order_id
UNION ALL
SELECT 'products.category_id -> categories', count(*),
       sum(CASE WHEN p.category_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN p.category_id IS NOT NULL AND c.category_id IS NULL THEN 1 ELSE 0 END)
FROM products p LEFT JOIN categories c ON c.category_id = p.category_id
UNION ALL
SELECT 'user_coupons.coupon_id -> coupons', count(*),
       sum(CASE WHEN uc.coupon_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN uc.coupon_id IS NOT NULL AND c.coupon_id IS NULL THEN 1 ELSE 0 END)
FROM user_coupons uc LEFT JOIN coupons c ON c.coupon_id = uc.coupon_id
UNION ALL
SELECT 'user_coupons.order_id -> orders', count(*),
       sum(CASE WHEN uc.order_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN uc.order_id IS NOT NULL AND o.order_id IS NULL THEN 1 ELSE 0 END)
FROM user_coupons uc LEFT JOIN orders o ON o.order_id = uc.order_id
UNION ALL
SELECT 'orders.coupon_id -> coupons', count(*),
       sum(CASE WHEN o.coupon_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN o.coupon_id IS NOT NULL AND c.coupon_id IS NULL THEN 1 ELSE 0 END)
FROM orders o LEFT JOIN coupons c ON c.coupon_id = o.coupon_id
ORDER BY 1;

-- L3.3 社交与行为链路
SELECT 'post_likes.post_id -> posts' AS fk, count(*) AS rows,
       sum(CASE WHEN l.post_id IS NULL THEN 1 ELSE 0 END) AS null_fk,
       sum(CASE WHEN l.post_id IS NOT NULL AND p.post_id IS NULL THEN 1 ELSE 0 END) AS dangling
FROM post_likes l LEFT JOIN posts p ON p.post_id = l.post_id
UNION ALL
SELECT 'post_comments.post_id -> posts', count(*),
       sum(CASE WHEN c.post_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN c.post_id IS NOT NULL AND p.post_id IS NULL THEN 1 ELSE 0 END)
FROM post_comments c LEFT JOIN posts p ON p.post_id = c.post_id
UNION ALL
SELECT 'post_shares.post_id -> posts', count(*),
       sum(CASE WHEN s.post_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN s.post_id IS NOT NULL AND p.post_id IS NULL THEN 1 ELSE 0 END)
FROM post_shares s LEFT JOIN posts p ON p.post_id = s.post_id
UNION ALL
SELECT 'user_follows.following_id -> users', count(*),
       sum(CASE WHEN f.following_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN f.following_id IS NOT NULL AND u.user_id IS NULL THEN 1 ELSE 0 END)
FROM user_follows f LEFT JOIN users u ON u.user_id = f.following_id
UNION ALL
SELECT 'events.session_id -> sessions', count(*),
       sum(CASE WHEN e.session_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN e.session_id IS NOT NULL AND s.session_id IS NULL THEN 1 ELSE 0 END)
FROM events e LEFT JOIN sessions s ON s.session_id = e.session_id
UNION ALL
SELECT 'page_views.session_id -> sessions', count(*),
       sum(CASE WHEN v.session_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN v.session_id IS NOT NULL AND s.session_id IS NULL THEN 1 ELSE 0 END)
FROM page_views v LEFT JOIN sessions s ON s.session_id = v.session_id
UNION ALL
SELECT 'events.event_name -> event_definitions', count(*),
       sum(CASE WHEN e.event_name IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN e.event_name IS NOT NULL AND d.event_name IS NULL THEN 1 ELSE 0 END)
FROM events e LEFT JOIN event_definitions d ON d.event_name = e.event_name
ORDER BY 1;

-- L3.4 归因、营销与实验链路
-- push_notifications.campaign_id 可空是设计的一部分：事务型推送不挂运营活动
SELECT 'push_notifications.campaign_id -> campaigns' AS fk, count(*) AS rows,
       sum(CASE WHEN p.campaign_id IS NULL THEN 1 ELSE 0 END) AS null_fk,
       sum(CASE WHEN p.campaign_id IS NOT NULL AND c.campaign_id IS NULL THEN 1 ELSE 0 END) AS dangling
FROM push_notifications p LEFT JOIN campaigns c ON c.campaign_id = p.campaign_id
UNION ALL
SELECT 'user_attributions.channel_id -> channels', count(*),
       sum(CASE WHEN a.channel_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN a.channel_id IS NOT NULL AND c.channel_id IS NULL THEN 1 ELSE 0 END)
FROM user_attributions a LEFT JOIN channels c ON c.channel_id = a.channel_id
UNION ALL
SELECT 'user_attributions.user_id -> users', count(*),
       sum(CASE WHEN a.user_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN a.user_id IS NOT NULL AND u.user_id IS NULL THEN 1 ELSE 0 END)
FROM user_attributions a LEFT JOIN users u ON u.user_id = a.user_id
UNION ALL
SELECT 'ad_campaigns.channel_id -> channels', count(*),
       sum(CASE WHEN a.channel_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN a.channel_id IS NOT NULL AND c.channel_id IS NULL THEN 1 ELSE 0 END)
FROM ad_campaigns a LEFT JOIN channels c ON c.channel_id = a.channel_id
UNION ALL
SELECT 'ad_creatives.ad_campaign_id -> ad_campaigns', count(*),
       sum(CASE WHEN r.ad_campaign_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN r.ad_campaign_id IS NOT NULL AND a.ad_campaign_id IS NULL THEN 1 ELSE 0 END)
FROM ad_creatives r LEFT JOIN ad_campaigns a ON a.ad_campaign_id = r.ad_campaign_id
UNION ALL
SELECT 'channel_daily_costs.channel_id -> channels', count(*),
       sum(CASE WHEN d.channel_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN d.channel_id IS NOT NULL AND c.channel_id IS NULL THEN 1 ELSE 0 END)
FROM channel_daily_costs d LEFT JOIN channels c ON c.channel_id = d.channel_id
UNION ALL
SELECT 'ab_test_assignments.variant_id -> ab_test_variants', count(*),
       sum(CASE WHEN a.variant_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN a.variant_id IS NOT NULL AND v.variant_id IS NULL THEN 1 ELSE 0 END)
FROM ab_test_assignments a LEFT JOIN ab_test_variants v ON v.variant_id = a.variant_id
UNION ALL
SELECT 'user_segment_members.segment_id -> user_segments', count(*),
       sum(CASE WHEN m.segment_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN m.segment_id IS NOT NULL AND s.segment_id IS NULL THEN 1 ELSE 0 END)
FROM user_segment_members m LEFT JOIN user_segments s ON s.segment_id = m.segment_id
UNION ALL
SELECT 'subscriptions.payment_id -> payments', count(*),
       sum(CASE WHEN b.payment_id IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN b.payment_id IS NOT NULL AND y.payment_id IS NULL THEN 1 ELSE 0 END)
FROM subscriptions b LEFT JOIN payments y ON y.payment_id = b.payment_id
ORDER BY 1;
