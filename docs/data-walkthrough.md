# 数据结构说明书：从一张表走到全库

这份文档按由浅入深的顺序把库过一遍：48 张表、约 9,129 万行、8 个业务域加治理层。
每一步都配可以直接跑的 SQL，贴的输出全部抄自实跑结果（2026-08-06，数据修复重灌后）。

跟它配套的两份文档分工不同：`docs/data-audit.md` 回答「数据质量可不可信」（五层
审计，修复前后都有记录）；本文回答「数据长什么样、怎么用」。

## 第 0 步：连库与两条铁律

所有查询走 Redshift Data API，封装在 `scripts/redshift/rsql.py`：

```bash
cd ~/Desktop/Claude/sample-analytics-agent-progressive-disclosure
SEC=$(aws secretsmanager list-secrets --region ap-northeast-1 \
  --query "SecretList[?contains(Name,'analytics-agent-ns')].ARN" --output text)

backend/.venv/bin/python scripts/redshift/rsql.py "SELECT 1" --secret "$SEC"
```

**铁律一：这是静态快照，禁用 `current_date` / `now()`。** 数据止于 2026-01-24，
起于 2025-10-26，共 91 天。所有「最近 7 天」「本月」都要以 `max(dt)` 或
`meta_snapshot.as_of_date` 为今天，用系统时间会查出空集。

**铁律二：这是 Redshift，不是 Postgres。** `FILTER (WHERE ...)` 要写成
`CASE WHEN`，没有 `DISTINCT ON`，没有二级索引。方言差异清单在
`knowledge/connection.md`。

## 第 1 步：全貌，48 张表分四层

```sql
SELECT CASE
         WHEN "table" IN ('dwd_orders_valid','dwd_events_app','dws_user_daily',
                          'dws_channel_weekly','fin_daily_revenue','growth_daily_gmv',
                          'orders_backup_20251201','tmp_campaign_roi_analysis')
              THEN '2_derived'
         WHEN "table" LIKE 'mart_%'        THEN '3_mart'
         WHEN "table" =    'meta_snapshot' THEN '4_meta'
         ELSE '1_base'
       END AS tier, count(*) AS tbls, sum(tbl_rows)::bigint AS rows
FROM svv_table_info WHERE schema='public' GROUP BY 1 ORDER BY 1;
```

```
tier       tbls  rows
1_base     35    79921903
2_derived  8     11154023
3_mart     4     218129
4_meta     1     1
```

- **base（35 张）**：原始明细，按 8 个业务域组织，是本文的主体
- **derived（8 张）**：DWD 清洗 / DWS 汇总 / ADS 应用 / 历史遗留，**有坑**，见第 9 步
- **mart（4 张）**：治理层，口径冻结的预聚合表，写简单 SELECT 就能用，见第 10 步
- **meta（1 张 1 行）**：数据「今天」的锚点

8 个域和各自的表数：用户 5、商品 3、行为 4、社交 6、交易 4、归因 5、营销 5、实验 3。
每张表的字段卡片在 `knowledge/domains/<域>/<表>.md`，本文只讲骨架，字段表不复述。

## 第 2 步：时间锚点

```sql
SELECT * FROM meta_snapshot;
```

```
as_of_date  data_start
2026-01-24  2025-10-26
```

任何带时间的查询都以这一行为基准。「最近 30 天」是 `WHERE dt >= '2025-12-26'`，
不是 `>= current_date - 30`。

## 第 3 步：用户域，从 users 开始

### 3.1 先抽三行

```sql
SELECT user_id, username, email, phone, registered_at, registration_source,
       status, user_level, is_vip
FROM users WHERE user_id IN (42, 4242, 42424) ORDER BY user_id;
```

```
user_id  username    email               phone        registered_at        registration_source  status  user_level  is_vip
42       user_42     ***@masked.invalid  103****0042  2026-01-18T08:31:25  app                  active  5           True
4242     user_4242   ***@masked.invalid  103****4242  2026-01-05T19:26:52  mini_program         active  1           False
42424    user_42424  ***@masked.invalid  103****2424  2026-01-15T18:33:59  app                  active  1           False
```

这三行先讲清一个约束：**email 和 phone 是掩码**。不是数据坏了，是数仓层挂了动态
脱敏策略（`database/redshift/04_governance.sql`），对所有访问者生效，包括管理员。
`user_profiles.birth_date` 同理，只保留出生年。明文不可获取，这是治理层策略。

枚举值：`registration_source` ∈ app/web/mini_program/h5，`status` ∈
active/inactive/banned。完整字段卡片见 `knowledge/domains/user/users.md`。

### 3.2 user_level 是从行为推导的，可直接用于筛人群

等级规则（优先级 5 > 4 > 3 > 1 > 2）：5 = 累计消费≥10000 或 VIP；4 = ≥1000；
3 = 近 30 天活跃≥10 天；1 = 注册<30 天；2 = 默认。验证：

```sql
WITH spend AS (
  SELECT u.user_id, u.user_level,
         COALESCE(SUM(CASE WHEN o.status IN ('paid','shipped','delivered')
                           THEN o.actual_amount END), 0) AS sp
  FROM users u LEFT JOIN orders o ON o.user_id = u.user_id GROUP BY 1, 2)
SELECT user_level, count(*) AS users, round(avg(sp),0) AS avg_spend,
       round(100.0*sum(CASE WHEN sp>=1000 THEN 1 ELSE 0 END)/count(*),1) AS pct_ge_1k
FROM spend GROUP BY 1 ORDER BY 1;
```

```
user_level  users  avg_spend  pct_ge_1k
1           63798  91.0       0.0
2           81002  262.0      0.0
3           7574   193.0      0.0
4           34490  2503.0     100.0
5           26656  1367.0     22.1
```

等级 4 内 100% 满足消费≥1000，等级 1-3 内 0%。等级 5 的均值反而比 4 低：VIP 直升 5 不看消费，低消费的 VIP 拉低了均值。

### 3.3 其余四张

`user_profiles`（1:1 画像：年龄、性别、城市、兴趣）、`user_devices`（1:N 设备）、
`user_segments` + `user_segment_members`（分群定义与成员）。关联键都是 `user_id`。

## 第 4 步：行为域，会话套事件

结构：`sessions` 是壳，`events` 和 `page_views` 都挂在会话下，**事件继承所属会话
的 user_id 和时间窗**。挑一个真实用户看生命周期（就用第 5 步那笔订单的买家）：

```sql
SELECT u.registered_at::date AS reg, u.user_level,
       count(DISTINCT s.session_id) AS sessions, count(e.event_id) AS events,
       min(e.event_time)::date AS first_ev, max(e.event_time)::date AS last_ev
FROM users u
LEFT JOIN sessions s ON s.user_id = u.user_id
LEFT JOIN events   e ON e.user_id = u.user_id
WHERE u.user_id = 205854 GROUP BY 1, 2;
```

```
reg         user_level  sessions  events  first_ev    last_ev
2025-12-21  5           2         44      2025-12-21  2026-01-23
```

首个事件不早于注册日，这条对全库 854 万事件成立（先注册后行为）。

`sessions.event_count` / `page_view_count` 是明细行数的精确回填，`is_bounce` =
页面浏览数≤1，`duration_seconds` = 起止时间差。这些计数器可直接使用，不必回明细重数（审计 L4.4 逐行验证过 0 差异）。

### 4.1 漏斗逐级递减

```sql
SELECT event_name, count(*) AS n, count(DISTINCT user_id) AS users
FROM events
WHERE event_name IN ('view_home','view_product','add_to_cart','begin_checkout','purchase')
GROUP BY 1 ORDER BY 2 DESC;
```

```
event_name      n        users
view_home       1393782  184115
view_product    1140783  177356
add_to_cart     928287   169613
begin_checkout  750146   160748
purchase        647265   125933
```

逐级递减，`purchase` 事件数与有效订单数**精确相等**（647,265 = 647,265），`use_coupon`
与核销券数、`register` 与用户数同样一一对应。已知边界：级间转化率偏高。行预算是 85 万订单配 850 万事件，漏斗被这个比例
天然压扁。**做漏斗形状和对比分析可以，别把绝对转化率当行业参考**。

### 4.2 留存是衰减的

```sql
WITH coh AS (SELECT user_id, registered_at::date AS d0 FROM users
             WHERE registered_at::date BETWEEN '2025-11-03' AND '2025-11-09'),
     act AS (SELECT DISTINCT e.user_id, e.event_time::date AS d
             FROM events e JOIN coh c2 ON c2.user_id = e.user_id)
SELECT DATEDIFF(day, c.d0, a.d) AS lag_days, count(DISTINCT a.user_id) AS active
FROM coh c JOIN act a ON a.user_id = c.user_id
WHERE DATEDIFF(day, c.d0, a.d) BETWEEN 0 AND 14 GROUP BY 1 ORDER BY 1;
```

```
lag_days  active        lag_days  active
0         13705         7         3922
1         8803          10        3187
2         7250          14        2564
```

D1≈64%、D7≈29%、D14≈19%，单调衰减。用户活跃按注册后的半衰期混合建模
（四档人群：1/5/20/60 天）。算次日留存、流失预警这类题，直接查 events 就够。

### 4.3 时间形状

订单的周内分布（0=周日）：周六 17.6%、周日 16.4%、周二谷底 12.2%，周末效应明显；
小时分布凌晨 3 点谷底、12 点午间小高峰、20 点主峰。日订单量变异系数 0.172。

## 第 5 步：交易域，追一笔订单走全链路

这一步追一笔真实订单，看表与表怎么咬合。取 `order_id = 2`：

```sql
SELECT order_id, user_id, status, total_amount, discount_amount, shipping_fee,
       actual_amount, item_count, coupon_id, placed_at::date AS placed
FROM orders WHERE order_id = 2;
```

```
order_id  user_id  status     total_amount  discount_amount  shipping_fee  actual_amount  item_count  coupon_id  placed
2         205854   delivered  469.41        23.47            0.0           445.94         2           147        2026-01-06
```

表内公式：`actual = total − discount + shipping`（469.41 − 23.47 + 0 = 445.94）。

**跟到明细**：

```sql
SELECT item_id, product_name, quantity, unit_price, discount_amount, actual_amount
FROM order_items WHERE order_id = 2;
```

```
item_id  product_name   quantity  unit_price  discount_amount  actual_amount
2        限定格力 油烟机  1         215.79      0.0              215.79
3        优质耐克 拉力带  1         253.62      0.0              253.62
```

215.79 + 253.62 = 469.41，**明细之和精确等于订单头 total_amount**，行数等于
`item_count`。这对全部 85.4 万单成立（精确到分，审计 L4.1/L4.2）。所以商品维度、
品类维度的 GMV 和订单头口径是同一套数，可以互相加总。

**跟到支付**：

```sql
SELECT payment_no, user_id, amount, payment_method, status, paid_at
FROM payments WHERE order_id = 2;
```

```
payment_no       user_id  amount  payment_method  status   paid_at
PAY000000000001  205854   445.94  wechat          success  2026-01-06T15:06:27
```

金额 = 订单 actual_amount，用户与订单同人。规则：**每个付过钱的订单恰好一条支付**，
valid（paid/shipped/delivered）对应 success，refunded 对应 refunded。支付状态只有
success / refunded 两种取值；一单一支付的设计下不存在 failed / pending 记录。

**跟到券**：

```sql
SELECT user_id, coupon_id, status, received_at::date AS recv,
       used_at::date AS used, order_id
FROM user_coupons WHERE order_id = 2;
```

```
user_id  coupon_id  status  recv        used        order_id
205854   147        used    2025-12-23  2026-01-06  2
```

(user, coupon, order) 三元与订单完全对齐，`used_at` 就是下单日。全库规则：
**used 券数 = 带 coupon_id 的订单数（324,596，1:1）**；没用掉的券只有 unused /
expired 两态，expired 由 `expire_at` 是否已过窗末决定。注意 973 万张券里只有
3.3% 被核销：核销数被用券订单数锁死，发券量又大，比例天然低。

**订单状态机**：pending → paid → shipped → delivered，旁路 cancelled / refunded。
状态与时间戳严格一致（refunded 必有 refunded_at，以此类推），窗末订单不会出现
「来不及走完」的状态。实测状态分布：paid 27.2%、delivered 25.5%、shipped 23.1%、
pending 10.5%、cancelled 8.8%、refunded 4.9%（refunded 和 delivered 因窗末下调
比生成权重略低，份额挪给了 paid）。有效单（valid）= paid + shipped + delivered
= 75.8%。

消费集中度也在这个域：top 1% 用户占 GMV 14.0%，top 10% 占 48.4%，比真实电商
（50-70%）略平，二八分析的结论会温和一些。

## 第 6 步：商品域，跨域 JOIN 不丢行

商品域三张：`categories`（多级分类）、`products`（200 个 SKU，反范式冗余了销量
评分）、`product_tags`。`order_items.product_name` 是下单时冗余的真实商品名，
与 `products.product_name` 一致，不 join 也能直接用。

四表跨域示例（品类 GMV Top3）：

```sql
SELECT p.product_name, c.category_name, round(sum(i.actual_amount),0) AS gmv
FROM order_items i
JOIN orders o     ON o.order_id = i.order_id
JOIN products p   ON p.product_id = i.product_id
JOIN categories c ON c.category_id = p.category_id
WHERE o.status IN ('paid','shipped','delivered')
GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 3;
```

```
product_name      category_name  gmv
三只松鼠 坚果      坚果           5379326
热卖蒙牛 咖啡      咖啡           5304535
限定联想 老人机    老人机         4498781
```

Redshift 不强制外键，但全库 28 个外键关系零悬空（审计 L3），INNER JOIN 不会
静默丢行。

## 第 7 步：营销域与归因域，两个「合法的缺口」

营销域五张：`campaigns`（运营活动）、`push_notifications`（427 万条推送，18% 的
`campaign_id` 为 NULL，那是事务型推送，不挂活动，**不是孤儿行**）、`coupons` /
`user_coupons`（见第 5 步）、`banners`。

归因域五张：`channels`（14 个渠道）、`ad_campaigns` / `ad_creatives`、
`user_attributions`（归因记录）、`channel_daily_costs`（投放成本，算 CAC/ROI 用）。

归因有个**刻意留的缺口**：

```sql
SELECT COALESCE(c.channel_name, '(未归因)') AS channel,
       round(sum(o.actual_amount),0) AS gmv,
       round(100.0*sum(o.actual_amount)/sum(sum(o.actual_amount)) OVER (),1) AS pct
FROM orders o
LEFT JOIN user_attributions ua ON ua.user_id = o.user_id
                              AND ua.attribution_type = 'last_touch'
LEFT JOIN channels c ON c.channel_id = ua.channel_id
WHERE o.status IN ('paid','shipped','delivered')
GROUP BY 1 ORDER BY 2 DESC LIMIT 4;
```

```
channel     gmv       pct
(未归因)    78008427  51.6
抖音搜索    5647437   3.7
百度搜索    5413735   3.6
微信公众号  5350535   3.5
```

`user_attributions` 只覆盖约 70% 用户，于是约一半 GMV 落在未归因。这是治理场景的
素材（知识库明确写了），做渠道分析时必须带上这一行，别当它是 bug 修掉。
注意 `attribution_type` 有 last_touch / first_touch / linear 三种，算渠道归因先过滤。

## 第 8 步：社交域与实验域

社交域六张：`posts`（42.7 万帖）、`post_likes`（1523 万，全库最大表）、
`post_comments`、`post_shares`、`user_follows`（818 万关注边）、`user_messages`
（**私信，agent 角色未授予 SELECT**：通信内容不是分析素材，治理层第 1 层
直接不授权）。

`posts` 上的 `like_count` / `comment_count` / `share_count` 是明细行数的精确回填，
`view_count ≥ like_count` 恒成立。注意帖子间的互动分布**接近均匀**（top 10% 帖子
只占 13.1% 点赞），帖子维度做不了帕累托；二八类分析的素材在用户消费侧。

实验域三张：`ab_tests`（12 个实验）→ `ab_test_variants`（30 个变体）→
`ab_test_assignments`（79 万分组记录），分组记录里的 variant 一定属于所分配的 test。
这两个域在日常分析里出场少，两句带过；要留意的只有上面帖子互动均匀那一条，
拿这份数据做「爆款内容分析」demo 会得出「没有爆款」的结论，选题前先看附表。

## 第 9 步：派生层，故意造的干扰项

8 张派生表是清洗副本、部门口径表和废弃遗留，名字都与基础表近似，
不查路由直接按名字选表就会选错。三个例子：

**口径打架**。`growth_daily_gmv`（增长部口径：按下单日）和 `fin_daily_revenue`
（财务口径：按支付日、扣运费），两张表口径不同，逐日必然有差：

```sql
SELECT g.dt, g.gmv AS growth_gmv, f.gross_revenue AS fin_gross,
       round(g.gmv - f.gross_revenue, 2) AS diff
FROM growth_daily_gmv g JOIN fin_daily_revenue f USING (dt)
ORDER BY dt DESC LIMIT 3;
```

```
dt          growth_gmv  fin_gross   diff
2026-01-24  1907520.24  2092332.56  -184812.32
2026-01-23  2163949.94  2112654.58  51295.36
2026-01-22  1958918.80  1928295.32  30623.48
```

问「昨天 GMV 多少」，先问清楚是谁的口径。

**假清洗层**。`dwd_events_app` 定义是 `events WHERE user_id IS NOT NULL`，但
`events` 里没有空 user_id，所以它就是 `events` 的整表复制（8,540,780 行，一行不差）。
`dwd_orders_valid` 确实做了筛选（647,265 = 有效单数）。

**废表**。`orders_backup_20251201`（只有 12-01 前的旧备份）和
`tmp_campaign_roi_analysis`（7 行半成品，`roi` 列全 NULL）。遇到名字相似的表，
先读 `knowledge/domains/_derived_overview.md` 和各域的 `_index.derived.md` 再选。

## 第 10 步：mart 层，能抄近路就抄

四张口径冻结的治理表：`mart_daily_kpi`（日核心指标）、`mart_daily_revenue`
（日×渠道×新老客收入）、`mart_channel_daily`（渠道成本与获客）、
`mart_user_summary`（用户一人一行汇总）。

```sql
SELECT dt, dau, new_users, orders, paying_users, gmv
FROM mart_daily_kpi ORDER BY dt DESC LIMIT 3;
```

```
dt          dau    new_users  orders  paying_users  gmv
2026-01-24  30162  3518       8100    7612          1907520.24
2026-01-23  28143  2904       9339    8682          2163949.94
2026-01-22  26647  2607       8368    7828          1958918.80
```

mart 的 GMV 口径 = 订单头 `actual_amount`、valid 状态、按下单日，与明细逐日对账
0 差异（91 天全对）。诊断、复盘、趋势、周报类问题直接查 mart，别回明细现算。
全库 GMV 总额（91 天，valid 口径）：**151,238,025**。

## 第 11 步：这本说明书和 knowledge/ 的关系

上面这条路线，就是 agent 每次分析走的路线。`knowledge/` 那棵 md 树是同一套
结构的机读版，三层递进：

1. `domains/_index.md`：9 行域路由表 + 关键词规则（本文第 1 步的角色）
2. `domains/<域>/_index.md`：单域的表清单、关系图、场景组合（第 3-8 步每步的开头）
3. `domains/<域>/<表>.md`：字段、枚举、示例 SQL（每步的细节层，本文未收录）

外加 `metrics/`（指标口径）、`analysis/`（分析方法 SOP）、`relationships.md`
（跨表 JOIN 键）、`_derived_overview.md`（第 9 步的选表指南）。人读本文建立地图，
查细节时按同一条路由读对应卡片，和 agent 的 `read_doc` 是一条路。

## 附：已知边界一览

正文各步讲过的边界，集中成一张查询前的对照表（详细论证在 `docs/data-audit.md`
的复审附录）：

| 边界 | 数字 | 影响 |
|---|---|---|
| 漏斗转化率偏高 | purchase 占事件 7.6%（真实 APP 0.5-2%） | 看形状和对比，别看绝对转化率 |
| 消费重尾略平 | top 10% 用户占 GMV 48.4%（真实 50-70%） | 二八分析结论偏温和 |
| 帖子互动近均匀 | top 10% 帖子只占 13.1% 点赞 | 帖子维度做不了帕累托 |
| 支付无失败态 | 状态只有 success / refunded | 算不了支付成功率 |
| 券核销率低 | 3.3%（核销数被用券订单数锁死） | used 券数 = 用券订单数，比例由结构决定 |
| 未归因 GMV | 51.6% | 治理场景素材，知识库写明保留 |
| PII 脱敏 | email 全掩码、phone 中段、生日只留年 | 对全部访问者生效，取不到明文 |
| 静态快照 | 止于 2026-01-24 | 一切「最近」以 max(dt) 为今天 |
