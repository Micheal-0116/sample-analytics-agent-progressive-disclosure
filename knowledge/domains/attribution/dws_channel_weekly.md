# dws_channel_weekly - 渠道周汇总

> ⚙️ 本文件由 `scripts/manifest/render.py` 从 `schema_manifest.yaml` 生成，**不要手改**。

**层级**：DWS 汇总层
**粒度**：一行 = 一个渠道 × 一个 ISO 周

渠道周汇总：每渠道每周的花费、新客数、周 CAC

## 何时用这张表

- ✅ 渠道效果的周级趋势、周报；周粒度环比
- ❌ 日粒度分析（用 mart_channel_daily）；单日异常定位

## 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| channel_id | INT | 渠道ID |
| channel_name | VARCHAR(100) | 渠道名 |
| week_start | DATE | ISO 周一 |
| cost | NUMERIC(14 | 当周投放花费。⚠️【虚高】归因表在聚合前 LEFT JOIN，一条成本行匹配 N 条归因就被数 N 遍；要准确花费查 channel_daily_costs 或 mart_channel_daily |
| installs | BIGINT | 当周安装数（渠道上报）。⚠️ 同 cost，被同一个 join 扇出重复计数 |
| new_users | BIGINT | 当周 last_touch 归因新客。这一列用了 count(DISTINCT)，【不受】扇出影响，可以放心用 |
| weekly_cac | NUMERIC(12 | 周 CAC = cost / new_users，无新客时为空。⚠️ 分子 cost 虚高，本列同样偏高 |

## 注意（口径与坑）

- 归因口径固定 last_touch，与 mart_channel_daily 一致
- 首尾是残周；周环比要掐头去尾，或用完整 ISO 周
- ⚠️ cost / installs / weekly_cac 因 join 扇出偏高（v1 照搬的既有缺陷，不是本表算错）。问"花费""预算""CAC""ROI"的准确值请改用 mart_channel_daily 或 channel_daily_costs；本表只适合看 new_users 的周趋势

## 构建口径（本表如何从基表算出）

> 方言为 Trino（Athena）。真源是 `schema_manifest.yaml` 里的 Postgres 写法，
> 由 `scripts/gen/pg_to_trino.py` 转换而来。

```sql
SELECT c.channel_id, c.channel_name,
       CAST(date_trunc('week', d.date) AS date) AS week_start,
       CAST(sum(d.cost) AS decimal(14,2))  AS cost,
       sum(d.installs)             AS installs,
       count(DISTINCT ua.user_id)  AS new_users,
       CAST((sum(d.cost) / NULLIF(count(DISTINCT ua.user_id), 0)) AS decimal(12,2)) AS weekly_cac
FROM channel_daily_costs d
JOIN channels c ON c.channel_id = d.channel_id
LEFT JOIN user_attributions ua
  ON ua.channel_id = d.channel_id
 AND ua.attribution_type = 'last_touch'
 AND CAST(ua.attributed_at AS date) >= CAST(date_trunc('week', d.date) AS date)
 AND CAST(ua.attributed_at AS date) <  CAST(date_trunc('week', d.date) AS date) + interval '7' day
GROUP BY 1, 2, 3
```
