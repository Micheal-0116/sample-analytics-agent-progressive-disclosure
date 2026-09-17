# Eval Report

- 运行时间: 2026-09-17 11:10:11  · 模型: global.anthropic.claude-opus-4-8
- 通过率: **26/27** (96%)
- 平均耗时: 41.6s/题 · 平均读文档 2.3 次 · 平均 SQL 0.8 条

| Level | 通过 | 总数 |
|---|---|---|
| L1 | 7 | 7 |
| L2 | 6 | 6 |
| L3 | 7 | 7 |
| L4 | 4 | 4 |
| L5 | 2 | 3 |

## 逐题明细

| # | L | 结果 | 耗时 | 文档 | SQL | 说明 |
|---|---|---|---|---|---|---|
| L1-users-count | 1 | ✅ | 31.8s | 3 | 1 | 命中 golden[all users]≈213535.0 |
| L1-order-count | 1 | ✅ | 46.0s | 3 | 1 | 命中 golden[all orders]≈854140.0 |
| L1-post-count | 1 | ✅ | 32.5s | 3 | 1 | 命中 golden[all posts]≈427070.0 |
| L1-campaign-count | 1 | ✅ | 38.5s | 3 | 1 | 命中 golden[all campaigns]≈50.0 |
| L1-dau-latest | 1 | ✅ | 35.7s | 0 | 0 | 命中 golden[events last day distinct users]≈30159.0 |
| L1-ab-running | 1 | ✅ | 34.3s | 3 | 1 | 命中 golden[status=running] 键4/4 |
| L1-channel-list | 1 | ✅ | 30.4s | 3 | 1 | 命中 golden[all channels] 键14/14 |
| L2-gender-dist | 2 | ✅ | 38.9s | 4 | 1 | 命中 golden[group by gender] 键2/2 |
| L2-device-dist | 2 | ✅ | 30.2s | 3 | 1 | 命中 golden[device rows] 键4/4 |
| L2-order-status-dist | 2 | ✅ | 37.9s | 3 | 1 | 命中 golden[group by status] 键6/6 |
| L2-top-pages-7d | 2 | ✅ | 46.8s | 3 | 1 | 命中 golden[page_views 7d anchored] 10/10 |
| L2-top-liked-posts | 2 | ✅ | 31.1s | 3 | 1 | 命中 golden[join post_likes] 5/5 |
| L2-coupon-usage-rate | 2 | ✅ | 37.9s | 3 | 1 | 命中 golden[used/total]≈3.343345586705551 |
| L3-gmv-30d | 3 | ✅ | 22.5s | 0 | 0 | 命中 golden[actual_amount valid status]≈57673919.78 |
| L3-top-products-gmv | 3 | ✅ | 33.8s | 4 | 1 | 命中 golden[order_items joined valid orders] 10/10 |
| L3-churn-30d | 3 | ✅ | 51.8s | 5 | 2 | 命中 golden[no session in 30d]≈93536.0 |
| L3-coupon-aov-compare | 3 | ✅ | 54.1s | 3 | 1 | 命中 golden[valid status] 两值均匹配 |
| L4-funnel | 4 | ✅ | 88.8s | 4 | 2 | 命中 golden[all-time subset funnel] 4/4 步 |
| L4-arpu-by-channel | 4 | ✅ | 50.8s | 3 | 1 | 命中 golden[attribution join orders] 3/3 |
| L5-retention-cohort | 5 | ❌ | 91.4s | 2 | 1 | 结论里没有声明「这份数据算不出留存」：曲线平坦是活跃度与注册生命周期独立抽样的结果（项目 P0），把它答成「留得住/粘性好」就是把数据缺陷报成了业务发现。右删失 |
| L5-repurchase-rate | 5 | ✅ | 37.0s | 1 | 0 | 命中 golden[governed mart definition]≈63.1 |
| L5-wow-gmv | 5 | ✅ | 41.2s | 0 | 1 | 命中 golden[mart_daily_kpi wow] 两值均匹配 |
| L3-refund-total | 3 | ✅ | 37.1s | 0 | 0 | 命中 golden[全量退款 (fin_daily_revenue, 按 refunded_at 建轴)]≈9897495.82 |
| L3-channel-cost-total | 3 | ✅ | 45.3s | 4 | 1 | 命中 golden[真实花费 (channel_daily_costs 明细)]≈4351988.47 |
| L4-cac-lowest-channel | 4 | ✅ | 30.3s | 0 | 0 | 命中 golden[CAC 升序 (成本限制在业务日历轴内)] 2/2 |
| L4-roi-cac-by-channel | 4 | ✅ | 34.4s | 0 | 0 | 命中 golden[抖音搜索 近30天的 ROI 与 CAC (同锚点、带上界)] 两值均匹配 |
| L3-cac-overall | 3 | ✅ | 32.8s | 0 | 0 | 命中 golden[全量 CAC (成本限制在业务日历轴内)]≈17.07 |

## 失败详情

### L5-retention-cohort — 按注册周做留存 cohort 分析，最近 8 个注册周的周留存矩阵是什么样？
- 结论里没有声明「这份数据算不出留存」：曲线平坦是活跃度与注册生命周期独立抽样的结果（项目 P0），把它答成「留得住/粘性好」就是把数据缺陷报成了业务发现。右删失说明不能替代这一条，见 knowledge/analysis/retention_curve.md 顶部那节
- agent SQL:
```sql
WITH a AS (SELECT max(as_of_date) AS d FROM meta_snapshot),
cohort AS (
  SELECT user_id, DATE_TRUNC('week', CAST(registered_at AS date)) AS cohort_week
  FROM users
),
weeks AS (
  SELECT cohort_week FROM (SELECT DISTINCT cohort_week FROM cohort) t
  ORDER BY cohort_week DESC LIMIT 8
),
activity AS (
  SELECT DISTINCT user_id, DATE_TRUNC('week', CAST(event_time AS date)) AS activity_week
  FROM events
)
SELECT
  c.cohort_week,
  COUNT(DISTINCT c.user_id) AS cohort_size,
  COUNT(DISTINCT CASE WHEN date_diff('week', c.cohort_week, act.activity_week)=0 THEN c.user_id END) AS w0,
  COUNT(DISTINCT CASE WHEN date_diff('week', c.cohort_week, act.activity_week)=1 THEN c.user_id END) AS w1,
  COUNT(DISTINCT CASE WHEN date_diff('week', c.cohort_week, act.activity_week)=2 THEN c.user_id END) AS w2,
  COUNT(DISTINCT CASE WHEN date_diff('week', c.cohort_week, act.activity_week)=3 THEN c.user_id END) AS w3,
  COUNT(DISTINCT CASE WHEN date_diff('week', c.cohort_week, act.activity_week)=4 THEN c.user_id END) AS w4,
  COUNT(DISTINCT CASE WHEN date_diff('week', c.cohort_week, act.activity_week)=5 THEN c.user_id END) AS w5,
  COUNT(DISTINCT CASE WHEN date_diff('week', c.cohort_week, act.activity_week)=6 THEN c.user_id END) AS w6,
  COUNT(DISTINCT CASE WHEN date_diff('week', c.cohort_week, act.activity_week)=7 THEN c.user_id END) AS w7
FROM cohort c
JOIN weeks w ON c.cohort_week = w.cohort_week
LEFT JOIN activity act ON act.user_id = c.user_id
GROUP BY c.cohort_week
ORDER BY c.cohort_week
```
