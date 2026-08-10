# 数据仓库连接信息

> 本文面向**人**（排查、手工查数），不是 agent 运行时读的。agent 走 `run_sql` /
> `call_metric` 工具，连接细节由 `backend/db.py` 封装。

## 当前形态：Amazon Redshift Serverless（ap-northeast-1）

| 项 | 值 |
|---|---|
| Workgroup | `analytics-agent-wg`（4 RPU 基础容量，`publiclyAccessible=false`） |
| Namespace | `analytics-agent-ns` |
| 数据库 | `app_analytics` |
| 管理员 | `awsuser`，密码由 Secrets Manager 托管（`redshift!analytics-agent-ns-awsuser-*`） |
| 区域 | `ap-northeast-1`（与 AgentCore Runtime、S3 数据桶同区） |
| 数据桶 | `s3://analytics-agent-data-<账号>-ap-northeast-1/raw/`（初始装载来源，查询不经过它） |

### 为什么走 Data API 而不是直连

Redshift Data API 是 HTTPS + IAM 的 AWS API，不是数据库连接。因此：

- **AgentCore Runtime 不需要进 VPC**，也不需要连接池
- 容器里**不存数据库密码**（用 Secrets Manager 托管密钥或 IAM 临时凭证）
- workgroup 保持不公开访问，**没有任何入站端口**

代价是它异步（提交 → 轮询 → 取结果），已由 `scripts/redshift/rsql.py` 包成同步调用。

## 手工查数

```bash
# 取托管密钥 ARN
SEC=$(aws secretsmanager list-secrets --region ap-northeast-1 \
  --query "SecretList[?contains(Name,'analytics-agent-ns')].ARN" --output text)

# 执行任意 SQL
backend/.venv/bin/python scripts/redshift/rsql.py \
  "SELECT count(*) FROM orders" --secret "$SEC"

# 批量执行一个 .sql 文件（按语句拆分，尊重字符串里的分号）
backend/.venv/bin/python scripts/redshift/rsql.py \
  --file database/redshift/02_mart.sql --secret "$SEC"
```

不带 `--secret` 时走 IAM 临时凭证，库用户由调用者的 IAM 身份派生（形如 `IAM:alice`）。

## 数据规模与静态样本铁律

当前样本全库 **约 9,129 万行** = 35 张原始表 7,992 万 + 8 张派生表 1,115 万
+ 4 张 mart 21.8 万（口语里的「8000 万」只指原始表那一层）：

| 表 | 行数 |
|---|---|
| post_likes | 15,231,627 |
| page_views | 12,894,870 |
| user_coupons | 9,708,732 |
| events | 8,540,780 |
| user_follows | 8,184,202 |
| orders | 854,078 |
| users | 213,520 |

**数据止于 2026-01-24**（`meta_snapshot.as_of_date`），起于 2025-10-26。
「最近 / 上周 / 本月」一律以 `max(dt)` 为今天，**禁用 `current_date` / `now()`**，
否则查空。

## Redshift 与 Postgres 的方言差异（写 SQL 时注意）

本库的 SQL 从 Postgres 迁过来，以下构造 Redshift **不支持**，已在迁移时改写：

| 不支持 | 替代写法 |
|---|---|
| `AGG(x) FILTER (WHERE cond)` | `AGG(CASE WHEN cond THEN x END)` |
| `DISTINCT ON (col)` | `ROW_NUMBER() OVER (PARTITION BY col ORDER BY ...) = 1` |
| `generate_series(...)`（引用用户表时） | 从够长的表上 `ROW_NUMBER()` 造数列 + `DATEADD` |
| `(array_agg(x ORDER BY x))[2]` | `ROW_NUMBER() OVER (...) = 2` |
| `JSONB` / `TEXT[]` | `SUPER`（JSON 文本） |
| `CREATE INDEX` | 无二级索引，靠建表时的 `SORTKEY` |

原生支持、无需改写的：`::` 类型转换、`date_trunc`、`interval` 运算、`NULLIF`、
`COALESCE`、窗口函数、`UNION`。

转换工具：`scripts/gen/pg_to_redshift.py`（带自测）。

## 本地 Postgres（v1 遗留，v2 不再验证）

`scripts/localpg/` 和 `docker-compose.yml` 是 v1 的本地 rig，仅用于小规模离线校验
（`scripts/gen/main.py --scale 1 --format csv` 加 `scripts/gen/load_local.sh`）。
v2 的方言、8000 万行规模和 Glue 集成都只在 Redshift 上验证，本地路径不再跟进。
