# Eval Report

- 运行时间: 2026-08-03 20:34:04  · 模型: global.anthropic.claude-opus-4-8
- 通过率: **21/21** (100%)
- 平均耗时: 46.3s/题 · 平均读文档 2.5 次 · 平均 SQL 1.0 条

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
| L1-users-count | 1 | ✅ | 51.2s | 3 | 1 | 命中 golden[all users]≈500.0 |
| L1-order-count | 1 | ✅ | 40.7s | 3 | 1 | 命中 golden[all orders]≈2000.0 |
| L1-post-count | 1 | ✅ | 47.3s | 3 | 1 | 命中 golden[all posts]≈1000.0 |
| L1-campaign-count | 1 | ✅ | 36.3s | 3 | 1 | 命中 golden[all campaigns]≈50.0 |
| L1-dau-latest | 1 | ✅ | 38.9s | 0 | 0 | 命中 golden[events last day distinct users]≈27.0 |
| L1-ab-running | 1 | ✅ | 41.7s | 3 | 1 | 命中 golden[status=running] 键4/4 |
| L1-channel-list | 1 | ✅ | 45.0s | 3 | 1 | 命中 golden[all channels] 键14/14 |
| L2-gender-dist | 2 | ✅ | 42.6s | 3 | 1 | 命中 golden[group by gender] 键2/2 |
| L2-device-dist | 2 | ✅ | 38.3s | 3 | 1 | 命中 golden[device rows] 键4/4 |
| L2-order-status-dist | 2 | ✅ | 42.7s | 3 | 1 | 命中 golden[group by status] 键6/6 |
| L2-top-pages-7d | 2 | ✅ | 47.3s | 3 | 1 | 命中 golden[page_views 7d anchored] 10/10 |
| L2-top-liked-posts | 2 | ✅ | 68.5s | 4 | 2 | 命中 golden[join post_likes] 4/5 |
| L2-coupon-usage-rate | 2 | ✅ | 45.1s | 3 | 1 | 命中 golden[used/total]≈49.7 |
| L3-gmv-30d | 3 | ✅ | 25.9s | 0 | 0 | 命中 golden[actual_amount valid status]≈2841462.13 |
| L3-top-products-gmv | 3 | ✅ | 33.7s | 3 | 1 | 命中 golden[order_items joined valid orders] 10/10 |
| L3-churn-30d | 3 | ✅ | 47.9s | 3 | 1 | 命中 golden[no session in 30d]≈18.0 |
| L3-coupon-aov-compare | 3 | ✅ | 47.5s | 3 | 1 | 命中 golden[valid status] 两值均匹配 |
| L4-funnel | 4 | ✅ | 74.4s | 3 | 3 | 命中 golden[all-time distinct users] 4/4 步 |
| L4-arpu-by-channel | 4 | ✅ | 88.9s | 3 | 1 | 命中 golden[attribution join orders] 3/3 |
| L5-repurchase-rate | 5 | ✅ | 21.7s | 0 | 0 | 命中 golden[governed mart definition]≈62.4 |
| L5-wow-gmv | 5 | ✅ | 46.8s | 1 | 1 | 命中 golden[mart_daily_kpi wow] 两值均匹配 |