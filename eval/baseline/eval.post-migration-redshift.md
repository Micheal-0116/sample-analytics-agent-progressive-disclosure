# Eval Report

- 运行时间: 2026-08-03 23:00:26  · 模型: global.anthropic.claude-opus-4-8
- 通过率: **21/21** (100%)
- 平均耗时: 47.0s/题 · 平均读文档 2.8 次 · 平均 SQL 1.0 条

| Level | 通过 | 总数 |
|---|---|---|
| L1 | 7 | 7 |
| L2 | 6 | 6 |
| L3 | 4 | 4 |
| L4 | 2 | 2 |
| L5 | 2 | 2 |

## 逐题明细

| # | L | 结果 | 耗时 | 文档 | SQL | 说明 |
|---|---|---|---|---|---|---|
| L1-users-count | 1 | ✅ | 29.3s | 3 | 1 | 命中 golden[all users]≈213520.0 |
| L1-order-count | 1 | ✅ | 46.8s | 3 | 1 | 命中 golden[all orders]≈854078.0 |
| L1-post-count | 1 | ✅ | 39.7s | 3 | 1 | 命中 golden[all posts]≈427039.0 |
| L1-campaign-count | 1 | ✅ | 35.8s | 3 | 1 | 命中 golden[all campaigns]≈50.0 |
| L1-dau-latest | 1 | ✅ | 39.3s | 0 | 0 | 命中 golden[events last day distinct users]≈88908.0 |
| L1-ab-running | 1 | ✅ | 41.0s | 3 | 1 | 命中 golden[status=running] 键4/4 |
| L1-channel-list | 1 | ✅ | 51.2s | 3 | 1 | 命中 golden[all channels] 键14/14 |
| L2-gender-dist | 2 | ✅ | 35.9s | 3 | 1 | 命中 golden[group by gender] 键3/3 |
| L2-device-dist | 2 | ✅ | 53.9s | 3 | 1 | 命中 golden[device rows] 键3/3 |
| L2-order-status-dist | 2 | ✅ | 41.4s | 3 | 1 | 命中 golden[group by status] 键6/6 |
| L2-top-pages-7d | 2 | ✅ | 44.6s | 3 | 1 | 命中 golden[page_views 7d anchored] 10/10 |
| L2-top-liked-posts | 2 | ✅ | 50.5s | 3 | 1 | 命中 golden[denormalized like_count] 4/5 |
| L2-coupon-usage-rate | 2 | ✅ | 50.0s | 3 | 1 | 命中 golden[used/total]≈50.0 |
| L3-gmv-30d | 3 | ✅ | 28.5s | 0 | 0 | 命中 golden[actual_amount valid status]≈55679342.35 |
| L3-top-products-gmv | 3 | ✅ | 73.3s | 4 | 1 | 命中 golden[order_items joined valid orders] 10/10 |
| L3-churn-30d | 3 | ✅ | 51.9s | 5 | 1 | 命中 golden[last_active_at variant]≈157787.0 |
| L3-coupon-aov-compare | 3 | ✅ | 36.7s | 3 | 1 | 命中 golden[valid status] 两值均匹配 |
| L4-funnel | 4 | ✅ | 75.8s | 3 | 3 | 命中 golden[all-time distinct users] 4/4 步 |
| L4-arpu-by-channel | 4 | ✅ | 50.9s | 5 | 1 | 命中 golden[attribution join orders] 3/3 |
| L5-repurchase-rate | 5 | ✅ | 40.2s | 1 | 0 | 命中 golden[governed mart definition]≈52.4 |
| L5-wow-gmv | 5 | ✅ | 71.0s | 1 | 3 | 命中 golden[mart_daily_kpi wow] 两值均匹配 |