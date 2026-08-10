# 部署指南

> **现行架构(v2)**:数据在 **Redshift Serverless**(Data API,HTTPS + IAM),元数据在 **Glue Data Catalog**,应用层是 AgentCore Runtime + S3 知识库 + Fargate /ask 中继 + CloudFront VPC origin(对应 `infra/` 三个 CloudFormation 栈与 `analyticsagent/` 的 `@aws/agentcore` CDK CLI)。v2 改了什么、为什么见 [architecture-v2-redshift-glue.md](architecture-v2-redshift-glue.md);Aurora 已退役,哪些旧路径保留但不再维护见 [legacy.md](legacy.md)。本文的「A. 本地数据库」「C. 云上部署(EC2)」都是 v1 形态,留作参考。

本项目的几种跑法,按需要选:

| 场景 | 跑什么 | 怎么跑 |
|------|--------|--------|
| **B. 本地 Web App(现行)** | Agent SDK + FastAPI + 前端跑在本机,数据连 Redshift Serverless | `backend/run.sh` |
| **D. 云上部署(AgentCore,现行)** | `infra/` 三栈 + `analyticsagent/` Runtime + Redshift | 见 [PROJECT_STATUS.md](../PROJECT_STATUS.md) 阶段五;前端与元数据快照用 `scripts/deploy/deploy_web.sh` 发布 |
| **A. 本地数据库(v1,legacy)** | 只起容器版 Postgres(35 表、约 19 万行) | `docker compose up -d` |
| **C. 云上部署(EC2,v1,legacy)** | 部署到你自己的 EC2 + CloudFront | 见下文「云上部署」 |

> **建 Redshift 数据层**(现行路径的前置,只做一次):DDL 在 `database/redshift/`(01 建表 → 02 mart → 03 派生层 → 04 治理),数据用 `scripts/gen/main.py` 生成 Parquet 传 S3、`scripts/redshift/load_from_s3.py` COPY 进仓,最后 `scripts/glue/register_catalog.py` 注册 Glue federated catalog、`scripts/glue/reconcile.py` 对账。workgroup 记得 `base-capacity 4` + 每月 RPU 用量上限(服务默认 128 RPU)。验收跑 `bash scripts/test_all.sh`(见 [test-plan-v2.md](test-plan-v2.md))。

---

## A. 本地数据库(Docker,v1 legacy)

### 前置
- Docker / Docker Compose,约 500MB 磁盘。

### 步骤
```bash
# 1. 启动 PostgreSQL(首启自动建表 + 灌数据,约 30 秒)
docker compose up -d
docker compose logs -f db        # 看到灌数完成即可

# 2. 验证
docker compose exec db psql -U postgres -d app_analytics -c "SELECT count(*) FROM users;"
```

`docker-compose.yml` 把 `database/`(DDL)、`data/csv/`(数据)、`scripts/docker-init.sh`(显式建表+灌数+重置序列)挂进容器首启脚本里。

| 连接项 | 值 |
|--------|-----|
| Host / Port | localhost / **5432** |
| Database | app_analytics |
| User / Password | postgres / postgres |

手动探数用 `scripts/dbquery.sh`(`docker exec` 进本地库):
```bash
./scripts/dbquery.sh "SELECT count(*) FROM events;"
```

---

## B. 本地 Web App(现行)

跑那套网页问数(Agent SDK + Bedrock + FastAPI + 前端)。后端默认 `DB_BACKEND=redshift`,经 Data API 连 Redshift Serverless(需要可用的 AWS 凭证;`run.sh` 启动时会 `aws sts get-caller-identity` 自检)。资源名不走默认命名时,复制 `.env.local.example` 成 `.env.local` 覆盖。

```bash
cd backend
./run.sh                          # 凭证自检 → uvicorn(8000)
# 打开 http://127.0.0.1:8000/
```

要走 v1 本地库(legacy):`DB_BACKEND=postgres ./run.sh`,会拉起本机 brew Postgres(端口 5433,`backend/.pgdata`)。

环境变量、Bedrock 配置、自测命令见 [../backend/README.md](../backend/README.md)。本地默认不开认证(`AUTH_ENABLED` 不设)。

---

## C. 云上部署(EC2,v1 legacy)

把 Demo 部署到你自己的 AWS 账号(EC2 + CloudFront + Cognito)。前置:一台能跑 Docker 的 EC2、一个 Cognito 用户池 + app 客户端(公共客户端,用 SRP 登录)、一个 CloudFront 分发指向 EC2。EC2 实例角色需有调用 Bedrock 所用模型的权限。

### 架构
```
浏览器 ─HTTPS→ CloudFront(默认证书,转发 POST/SSE,源读超时 60s)
                  │ HTTP:80
                  ▼
              EC2(docker-compose.cloud.yml)
                ├─ analytics-app   FastAPI + Agent SDK + claude CLI,代码烤进镜像(无挂载,uvicorn 无 reload)
                └─ analytics-db    postgres:16,带 pgdata 持久卷,首启灌 35 表 19 万行
```
Bedrock 经 EC2 实例角色走 IMDS 取凭证(hop limit=2),模型 `global.anthropic.claude-opus-4-8`。

### 认证(app 层 Cognito)
前端 `amazon-cognito-identity-js` SRP 登录拿 idToken → 后端 `server.py` 用 JWKS 校验。CloudFront / 边缘不碰认证,所以静态资源能正常缓存、登录后刷新快。`/app/vendor/*` 走长缓存行为,其余 `no-store`。

> `docker-compose.cloud.yml` 从环境变量注入 `AUTH_ENABLED` 和 `COGNITO_*`。把你自己的 `COGNITO_USER_POOL_ID` / `COGNITO_CLIENT_ID` 写进项目根的 `.env`(见 `.env.example`),compose 会自动读取。本地开发不设 `AUTH_ENABLED` 即关闭认证。

### 部署(在 EC2 上)
把代码拉到 EC2,在项目根:
```bash
# 先把 Cognito 的 pool / client id 填进 .env(见 .env.example)
docker compose -f docker-compose.cloud.yml up -d --build
```
db 容器首启会自动建表灌数;app 容器构建镜像时把 `backend/` + `web/` + `knowledge/` 一起打进去。改代码后重跑 `up -d --build` 即可生效(app 镜像无挂载、uvicorn 无 reload,靠重建;db 有持久卷,不会重灌)。

### 安全组
EC2 入站 80 只对 CloudFront 的托管前缀列表(`com.amazonaws.global.cloudfront.origin-facing`)开放,**不要用 `0.0.0.0/0`**。

### 拆除
terminate EC2 → 删 SG → 删 instance-profile / role → 删 Cognito 用户池(含域名) → 清空并删 S3 桶 → disable & delete CloudFront 分发。

### 踩过的坑
1. `claude` CLI 拒绝以 root 跑 `--dangerously-skip-permissions`(SDK 的 bypassPermissions 会下发该 flag)→ Dockerfile 必须建非 root 用户(appuser uid 10001)跑。
2. CloudFront `DefaultRootObject=app/index.html`,`/` 直接服务 app 页 → `index.html` 里资源引用必须用**绝对** `/app/vendor/...`,相对 `./vendor` 会解析成 `/vendor` 404。
3. 前端 API 地址判断按 `location.protocol`,别用 `location.port`(HTTPS 默认端口下会误判退回本地 8000)。

---

## 数据说明

- **时间范围**:静态样本,数据落在 2025-10-26 ~ 2026-01-24。查"最近 N 天"时以表自身时间列的 `max()` 为锚点,**别用 `current_date`/`now()`**。
- **规模(v2 / Redshift)**:Glue 目录 48 张表,35 张原始表约 8000 万行;v1 本地库是 35 张表、约 19 万行。
- **重新生成数据(v2)**:`scripts/gen/main.py --target-rows 80000000 --out <目录> --s3 s3://<你的数据桶>/raw/`,再 `scripts/redshift/load_from_s3.py` COPY 进仓;v1 的 CSV 生成器是 `cd scripts && python generate_data.py`。

## 常见问题

**Q:Docker 启动失败 / 端口被占**
`lsof -i :5432` 看占用,改 `docker-compose.yml` 端口映射。

**Q:CSV 导入失败**
确保 CSV 列顺序与表结构一致:`head -1 data/csv/<表>.csv` 对比 `psql -c "\d <表>"`。

**Q:Web App 查询超时 / 很慢**
Opus 走文档路由每题要多读几份 md、多几次工具往返,端到端约 25~70 秒,属正常。
