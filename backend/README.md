# App Analytics Agent — 后端

把 CLI 里的数据分析 Skill 产品化成一个**独立 Web 应用**:大脑用 **Claude Agent SDK** 自建 Agent,跑在 **Amazon Bedrock 的 Claude Opus 4.8** 上,脱离 Claude Code CLI;前端是 `../web` 那套渐进式披露 UI。

## 架构

```
浏览器 (../web/index.html)
   │  SSE
   ▼
FastAPI (server.py)
   │
   ▼
Claude Agent SDK  ←——  Amazon Bedrock (global.anthropic.claude-opus-4-8)
   │  进程内 MCP 工具(tools.py)
   ├─ read_doc         渐进式披露的核心:按路由逐层读数据字典 md 文档树
   ├─ run_sql          只读单条 SELECT(db.validate 强制安全)
   ├─ call_metric      治理层官方口径(metrics_def.py,指标即代码)
   ├─ compute_stats    统计计算(stats.py)
   └─ present_result   交付 KPI / 图表 spec / 洞察 / 追问
   │
   ▼
DB_BACKEND=redshift(默认):Redshift Serverless · Data API(HTTPS + IAM,无 host/port)
DB_BACKEND=postgres(v1 legacy):本地 backend/.pgdata:5433 或云上 db 容器 · psycopg
```

- `agent.py` —— 系统提示(强制"先读文档再写 SQL"的工作流 + chart 约定 + 时间/业务口径)、`ClaudeAgentOptions`、把 SDK 事件流解析成给前端的 UI 事件(`stage`/`sql`/`rows`/`doc_detail`/`result`/`done`)。
- `tools.py` —— 五个进程内 MCP 工具。
- `db.py` —— SQL 安全校验(仅 SELECT/WITH、单条语句、15s 超时、≤1000 行)+ 双后端分派:`redshift`(默认,Data API,复用 `scripts/redshift/rsql.py`)/ `postgres`(v1 legacy,psycopg 只读连接)。只读闸门两个后端共用。
- `catalog.py` —— `/api/catalog` 的装配逻辑:Glue Data Catalog + Redshift `svv_*` + `knowledge/domains/` + `schema_manifest.yaml` 聚成 UI 直接渲染的元数据(表清单/行数/分层/治理现状),Glue 读不到降级到 `information_schema` 并在 `source` 字段里说明。
- `server.py` —— FastAPI + SSE,托管 `../web` 静态页,处理 app 层 Cognito 鉴权。
- 数据字典 md 文档树在顶层 `../knowledge/`(与 `backend/` 同级,是 agent 的单一知识库),`read_doc` 就读这棵树;打镜像时 `COPY knowledge/` 进 `/app/knowledge`。见 [../knowledge/README.md](../knowledge/README.md)。

### read_doc 怎么体现渐进式披露

Agent 脑子里**没有**表结构,系统提示强制它每个问题都走一遍:

```
read_doc("domains/_index.md")          # L1:按关键词判断落在哪个业务域
   → read_doc("domains/<域>/_index.md") # L2:看这个域有哪些表、定位到要用的表
   → read_doc("domains/<域>/<表>.md")   # L3:拿准确字段、枚举值、示例 SQL
   (指标公式读 metrics/*.md,多表 JOIN 读 relationships.md)
→ run_sql(...)                          # 读够了再写 SQL
→ present_result(...)                   # 交付结论
```

`read_doc` 做了路径逃逸防护(只能读 `knowledge/` 内的 `.md`),文档不存在时回一份同目录可读清单帮 Agent 自我纠偏。前端把每次 `read_doc` 渲染成"⟳ 正在读取 <path>"步骤 + 文件查看器,这就是看得见的渐进式披露。

> 历史:早期工具版本用 `get_table_schema` 查 `information_schema` 拿结构(`db.py` 里还留着 `get_schema` 这个未用函数)。现已重构为上面的文档路由版——读 Skill 文档树而非系统表,过程才可演示。

## 本地启动

前置:
- venv:`backend/.venv`,依赖见 `requirements.txt`(`claude-agent-sdk`、`fastapi`、`uvicorn`、`boto3`、`PyJWT[crypto]`;`psycopg` 仅 v1 legacy 路径用到)。
- AWS 凭证:走标准链(`~/.aws` / 环境变量 / 实例角色)。默认后端连 Redshift Data API 靠 IAM,`run.sh` 启动时会 `aws sts get-caller-identity` 自检。
- 一个灌好数据的 Redshift Serverless workgroup(建法见 [../docs/deployment.md](../docs/deployment.md))。
- Bedrock:确保当前 AWS 账号已在目标区域开通所用模型的访问权限(本项目默认 `global.anthropic.claude-opus-4-8`,跨区推理 profile)。
- *(仅 v1 legacy 路径)* 本机 Postgres@16(brew);`DB_BACKEND=postgres` 时 `run.sh` 会在 `backend/.pgdata` 建集群(端口 5433)并灌入 35 表 / 全部 CSV。

```bash
cd backend
./run.sh                      # 凭证自检 → uvicorn(8000);DB_BACKEND=postgres ./run.sh 走 v1 路径
# 打开 http://127.0.0.1:8000/
```

环境变量(`run.sh` 已设默认值,可覆盖;本地覆盖建议写 `.env.local`,见 `.env.local.example`):

| 变量 | 默认 | 说明 |
|------|------|------|
| `DB_BACKEND` | `redshift` | `redshift`(现行)/ `postgres`(v1 legacy) |
| `CLAUDE_CODE_USE_BEDROCK` | `1` | 走 Bedrock |
| `AWS_REGION` | `ap-northeast-1`(redshift)/ `us-east-1`(postgres) | redshift 后端与数仓同区 |
| `ANTHROPIC_MODEL` | `global.anthropic.claude-opus-4-8` | 全局跨区推理 profile(禁裸 ID / `us.` / `eu.` 前缀) |
| `REDSHIFT_WORKGROUP` | `analytics-agent-wg` | Redshift Serverless workgroup |
| `REDSHIFT_DATABASE` | `app_analytics` | 数据库名 |
| `GLUE_CATALOG_ID` | `<账号>:analytics_agent_rs` | UI 元数据来源;账号启动时从 sts 现算。不配则 `/api/catalog` 降级到 `information_schema` |
| `PGPORT` | `5433` | (仅 postgres 后端)本地库端口 |
| `PORT` | `8000` | 服务端口 |
| `AUTH_ENABLED` | 不设=关 | 设 `1` 开 app 层 Cognito 校验(本地默认关) |

## 认证(app 层 Cognito)

为了让 CloudFront 能正常缓存静态资源,认证不放在边缘,而放在应用层:

- 前端 `amazon-cognito-identity-js` 做 SRP 登录,拿到 idToken 存 localStorage,调 `/ask` 时带 `Authorization: Bearer <idToken>`。
- 后端 `server.py` 用 `PyJWKClient` 拉 JWKS 校验 ID token(`AUTH_ENABLED=1` + `COGNITO_REGION`/`COGNITO_USER_POOL_ID`/`COGNITO_CLIENT_ID` 经 compose env 注入)。
- `/ask` 需鉴权;`/health`、`/api/config`、静态资源公开。`/api/config` 只回公开值(pool id / client id),前端据此初始化登录浮层。
- 本地开发不设 `AUTH_ENABLED` 即关闭认证,直接用。

## 路由一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | DB 与配置自检(公开);`db` 字段按后端给出身份信息(engine/workgroup 或 host/port) |
| GET | `/api/catalog` | UI 的元数据来源:表清单/字段/行数/分层/治理现状(缓存 5 分钟,`?refresh=1` 跳过) |
| GET | `/api/config` | 前端初始化 Cognito 登录用(公开,只含公开值) |
| POST | `/ask` | body `{question, session_id?}`,返回 `text/event-stream`;开认证时需 Bearer ID token |
| GET | `/` | 重定向到 `/app/index.html` |
| | `/app/*` | 托管 `../web` 静态资源 |

`/app/vendor/*`(echarts/字体/cognito-sdk 等)发长缓存头,其余 `no-store`。

## 自测

```bash
# 大脑最小全链路(不开服务)
cd backend && .venv/bin/python test_agent.py "各商品品类的销量排行"

# 健康检查
curl -s http://127.0.0.1:8000/health
```

## 上云部署

现行形态是 AgentCore Runtime + Fargate 中继 + CloudFront(见 [../PROJECT_STATUS.md](../PROJECT_STATUS.md) 阶段五/六);前端与元数据快照用 `../scripts/deploy/deploy_web.sh` 发布。v1 的 EC2 两容器形态(`../docker-compose.cloud.yml`)留作 legacy。完整步骤见 [../docs/deployment.md](../docs/deployment.md)。

## 已知事项

- 前端在后端不可达时**自动回退**离线演示(模拟数据,冻结在 v1),`web/index.html` 单独双击也能看 UI。
- 多轮上下文:当前每次提问是独立会话(已捕获 `session_id`,如需续接可在 `/ask` 传回)。
- SQL 安全边界全在 `db.py`:仅 `SELECT`/`WITH`、单条语句、15s 超时、最多 1000 行;与后端无关,切 `DB_BACKEND` 不削弱。
- 本地这个 FastAPI 不只是 demo,还是测试与构建设施(`scripts/test_all.sh` L6、eval、`build_catalog_json.py` 都依赖它),别当 legacy 砍——边界见 [../docs/legacy.md](../docs/legacy.md)。
