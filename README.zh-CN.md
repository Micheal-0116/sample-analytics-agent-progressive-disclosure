[English](README.md) | **中文**

# Analytics Agent · Progressive Disclosure

[![License: MIT-0](https://img.shields.io/badge/License-MIT--0-blue.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/Python-3.11-3776AB.svg?logo=python&logoColor=white)](backend/requirements.txt)
[![Claude Agent SDK](https://img.shields.io/badge/Claude_Agent_SDK-Opus_4.8-cc785c.svg?logo=anthropic&logoColor=white)](https://docs.anthropic.com)
[![Amazon Bedrock](https://img.shields.io/badge/Amazon_Bedrock-FF9900.svg?logo=amazonaws&logoColor=white)](https://aws.amazon.com/bedrock/)

一个"问数"Demo:用大白话提问,Agent 自己定位表、写对 SQL、查数、出图给结论。

它真正想证明的是一件事——**把数据库的表结构写成一棵可按需翻阅的"数据字典"md 文档树,让 Agent 顺着路由一层层读出来再写 SQL,会比"一股脑把全库 schema 塞进上下文"或"每次从头探索数据库"更准、更省**。这套机制就是 Agent Skill 的渐进式披露(progressive disclosure)。

数据集故意做得很杂:一个内容+电商混合型 APP(类似小红书/得物),35 张原始表、8 个业务域,加上派生层(dwd/dws/ads)、治理集市和两张故意埋的陷阱表,**Glue Data Catalog 里共 48 张表、原始表约 8000 万行**,跑在 **Amazon Redshift Serverless** 上。表一多,口径陷阱就多(GMV 算 total 还是实付?优惠券核销在模板表还是领取表?A/B 该不该掐时间窗?),"先读对文档再写 SQL"的价值才显出来。

![数据分析 Agent 架构图(动态)](docs/architecture.svg)

> 这张图在渲染后的 README 里会动(GitHub 把 SVG 当图片嵌入)。蓝色 = 进行中的请求,青色 = 流式返回,绿色 = 冷启动时从 S3 同步知识树,天蓝 = `run_sql` 走 Data API 打到 Redshift Serverless(HTTPS + IAM,不进 VPC、不要连接池、容器里没有密码)。`/ask` 这一跳走 CloudFront VPC origin 到内网 ALB 和 Fargate 中继,由中继校验 Cognito JWT 并透传 AgentCore Runtime 的 SSE 流。

## v2 变了什么(Redshift + Glue)

v1 的数据在本地/Aurora PostgreSQL(35 张表、约 19 万行),元数据是一棵手写的 md 树。v2 把两头都升级了,完整过程和踩坑见 [docs/architecture-v2-redshift-glue.md](docs/architecture-v2-redshift-glue.md):

- **数据**:搬到 **Redshift Serverless**(约 8000 万行,经 Data API 查询——HTTPS + IAM,`publiclyAccessible=false`,没有任何入站端口)。生成器(`scripts/gen/`)按 v1 数据的 427 倍等比放大,所有业务比例原封不动,知识库里写的口径陷阱在新数据上依然成立。
- **元数据**:拆成「声明态」(`schema_manifest.yaml` + DDL,进 git)/「实际态」(**Glue Data Catalog**,生成的,不可手改)/「语义层」(`knowledge/` 表卡片:何时用、坑、指标口径)三方,并加对账(`scripts/glue/reconcile.py`)。第一次跑就抓出 5 处真实文档漂移。
- **治理下沉到数仓**:最小权限角色 `analytics_agent_ro`(48 张表只授了 45 张,私信表连表都不授权)+ `users.email` / `users.phone` / `user_profiles.birth_date` 挂动态脱敏——两层都在查询时生效,独立于应用层 SQL 闸门。
- **UI 读目录**:新增 `GET /api/catalog`,把 Glue + Redshift `svv_*` + `knowledge/` + manifest 聚成前端直接渲染的结构,替掉写死在 HTML 里的数字。
- **评测基线**:21 条金标用例在 Redshift 上 21/21 全过(`eval/`);金标 SQL 保持 Postgres 方言,运行时改写(`scripts/gen/pg_to_redshift.py`)。

v1 的 PostgreSQL 路径(本地容器)还能跑,但已是 **legacy——保留、不再维护**,边界见 [docs/legacy.md](docs/legacy.md)。

## 它长什么样

打开后左侧有 6 个示例题(由易到难:30 天 GMV → 订阅套餐 → 转化漏斗 → 优惠券核销 → 渠道 CAC → A/B 实验),Agent 会把每一步"正在读哪份文档"实时铺开,看得见渐进式披露的全过程。前端右上角有 **EN / 中** 语言切换。大脑是 **Claude Agent SDK** 自建的 Agent,跑在 **Amazon Bedrock**(默认 `global.anthropic.claude-opus-4-8`,`global.` 跨区推理)上,不依赖 Claude Code CLI。单题端到端约 25~70 秒(Opus 要多读几份 md、多几次工具往返)。注意:UI **外壳**可切中英,但**分析内容**(洞察 / SQL / 结果)仍以中文返回,因为 Agent 读的知识库是中文。

本地跑起来见下面「[本地跑起来](#本地跑起来)」;部署到自己的 AWS 账号见 [docs/deployment.md](docs/deployment.md)。

## 项目的三层结构

这个项目其实是同一个想法在三个层面的落地,看文档前先有个整体认识:

```
┌──────────────────────────────────────────────────────────────┐
│  3) Web App(独立产品化)  backend/ + web/                      │
│     Claude Agent SDK + Bedrock + FastAPI,自建 5 个 MCP 工具:  │
│     read_doc/run_sql/call_metric/compute_stats/present_result │
│     前端 SSE 实时披露 —— 谁都能打开的网页问数工具             │
├──────────────────────────────────────────────────────────────┤
│  2) 知识库(数据字典 + 路由规则)  knowledge/                   │
│     一棵 md 文档树:domains/_index.md(L1 路由)               │
│       → domains/<域>/_index.md(L2 选表)                      │
│       → domains/<域>/<表>.md(L3 拿字段/枚举/示例)            │
│     + metrics/(指标口径) + relationships.md(表间关系)       │
├──────────────────────────────────────────────────────────────┤
│  1) 数据层(可复现的数仓)  database/ + scripts/                │
│     Redshift Serverless —— Glue 目录 48 张表,原始表约 8000 万行│
│     (生成器 scripts/gen/;v1 本地 PostgreSQL 路径留作 legacy)  │
└──────────────────────────────────────────────────────────────┘
```

- **数据层**是地基:`database/redshift/` 是 v2 的 DDL(原始 + mart + 派生 + 治理),`scripts/gen/` 生成约 8000 万行 Parquet,`scripts/redshift/load_from_s3.py` COPY 进仓,`scripts/glue/` 注册并对账 Glue Data Catalog。
- **知识库** 是核心资产:`knowledge/` 那棵 md 文档树就是"数据字典",由 Web App 的 `read_doc` 工具按路由逐层读取、并打包进镜像,是 Agent 唯一的知识来源。
- **Web App** 把这套能力产品化:做成一个谁都能打开的网页问数工具,并把"读文档→写 SQL→出图"的全过程可视化。

## 知识库(数据字典)

`knowledge/` 是 Agent 的单一知识库,一棵 md 文档树、8 域齐全 + 治理层 mart:

- `domains/`：三层路由(`_index.md` 总索引 → 域索引 → 单表卡片),表结构、字段枚举、示例 SQL 都在这。
- `metrics/`：指标口径,其中 `governed_metrics.md` 是治理层官方口径,与 `backend/metrics_def.py` 的 `call_metric` 工具一一对应(口径以代码为准)。
- `analysis/`：5 个深度分析方法的 SOP + 统计公式。
- `relationships.md`：跨表 JOIN 的关联键。

细节见 [knowledge/README.md](knowledge/README.md)。知识库文档树保持中文。

> 早期这棵树曾放在 `.claude/skills/` 下当 Claude Code CLI skill(还做过「快捷模板」与「纯路由」两套变体对照）。产品化后统一收敛为顶层 `knowledge/`,CLI skill 那套已移除。

## 两种范式:取数(text-to-ETL)与洞察(text-to-insight)

同一套数据做了两层,一个 demo 讲两件事:

- **原始数据 · 问数(text-to-ETL)**:35 张原始明细表。问"30 天 GMV""转化漏斗""渠道 CAC"这类取数题,Agent 现场 join 多张表、自己定口径、写复杂 SQL,难点在把逻辑构造对、别踩口径陷阱。
- **治理层 · 洞察(text-to-insight)**:在原始表之上用 `database/redshift/02_mart.sql` 叠了 4 张"治理后"的预聚合表(`mart_*`),GMV/新客/归因/复购等口径**冻结**在表里和 `metrics/governed_metrics.md`。问"复盘月度 GMV、谁带动的""这周业务咋样""复购率"这类判断题,Agent 对干净表写简单 SELECT,把力气花在多角度切片、归因、下结论上。

这对应真实世界的两种场景:治理之前,AI 帮你把脏数据算成对的数;治理之后,脏活管道做完了,AI 帮你分析、归因、下判断。前端左侧预设按这两组分开摆,点一题就看出区别。Agent 怎么在两层间选,写在 `backend/agent.py` 的系统提示里(取数走原始域,诊断/复盘/综合判断走治理层)。

## 前置条件

- **AWS 账号 + 已开通 Amazon Bedrock 模型访问**:在 Bedrock 控制台为你的 Region 申请所用模型(默认 Claude Opus 4.8)的访问权限。本项目通过跨区推理 profile(`global.anthropic.claude-opus-4-8`)调用。
- **AWS 凭证**:走标准链(`~/.aws`、环境变量或 EC2 实例角色)。默认后端(`redshift`)对 Data API 用 IAM 认证,本机不落数据库密码。
- **一个灌好数据的 Redshift Serverless workgroup**:DDL 在 `database/redshift/`,生成与装载脚本在 `scripts/gen/` + `scripts/redshift/`(默认 workgroup 名 `analytics-agent-wg`,基础容量 4 RPU;**务必显式设小基础容量和每月 RPU 用量上限**——服务默认是 128 RPU)。
- **Python 3.11**:后端运行。
- **Node.js 20+ 与 Claude Code CLI**:`npm install -g @anthropic-ai/claude-code`。Claude Agent SDK 会把 `claude` CLI 作为子进程拉起,必须在 `PATH` 上。(Docker 镜像已内置;本地 `run.sh` 路径需你自己装。)
- *(仅 v1 legacy 路径)* **Docker + Docker Compose** 或 **PostgreSQL 16**:本地容器库(`DB_BACKEND=postgres`,见 [docs/legacy.md](docs/legacy.md))。

> **成本**:跑它不是免费的。每个问题都会调用 Amazon Bedrock 上的 Claude Opus(数十秒推理 + 多轮工具往返),云上部署还额外跑一台 EC2 和一个 CloudFront 分发——按标准 Bedrock token 与基础设施计费。用完记得拆除云上资源(见 [清理](#清理))。

## 本地跑起来

后端用 `DB_BACKEND` 选数据后端:**默认 `redshift`**(经 Data API 连 Redshift Serverless);`postgres` 是 v1 的本地容器 legacy 路径。

**默认路径(Redshift):**

```bash
# 前置:Python venv、可用的 AWS 凭证(aws sso login / AWS_PROFILE)、
# Bedrock 模型访问权限、一个灌好数据的 Redshift Serverless workgroup(见 database/redshift/)
cd backend
./run.sh                            # 自检凭证后起 uvicorn(8000)
# 打开 http://127.0.0.1:8000/
```

`run.sh` 启动时用 `aws sts get-caller-identity` 现算账号——仓库里不放任何账号相关的值。要覆盖资源名(workgroup、Glue 目录、区域),把 `.env.local.example` 复制成 `.env.local`。手动探数用 `scripts/redshift/rsql.py "SELECT ..."`。

**Legacy 路径(v1 本地 Postgres,约 19 万行,不再维护):**`docker compose up -d` 起纯数据库容器(35 表自动灌数),`docker-compose.cloud.yml` 跑 v1 完整两容器栈(已钉 `DB_BACKEND=postgres`)。哪些还维护、哪些不维护的确切边界见 [docs/legacy.md](docs/legacy.md)。

后端架构、环境变量、自测命令见 [backend/README.md](backend/README.md)。
后端不可达时,`web/index.html` 会自动回退到离线演示(内置模拟数据,冻结在 v1),单独双击也能看 UI。完整测试套件是 `bash scripts/test_all.sh`(L0–L6:对账、一致性、评测、端点与渲染契约,见 [docs/test-plan-v2.md](docs/test-plan-v2.md))。

## 项目结构

```
sample-analytics-agent-progressive-disclosure/
├── README.md                    # 英文门面
├── README.zh-CN.md              # 本文件:中文门面
├── PROJECT_STATUS.md            # 演进记录(CLI Skill → Web App → 上云 → Redshift+Glue)
├── docs/
│   ├── architecture-v2-redshift-glue.md  # v2 架构:改了什么、为什么、踩了哪些坑
│   ├── legacy.md                 # 哪些代码保留但不再维护(及其边界)
│   ├── test-plan-v2.md           # L0–L8 验收分层(scripts/test_all.sh 跑 L0–L6)
│   └── deployment.md             # 部署指南
├── schema_manifest.yaml         # 派生层的声明态(单一真源)
│
├── database/                    # ① 数据层 · DDL
│   ├── redshift/                 #   v2 DDL:01_tables / 02_mart / 03_derived / 04_governance
│   └── 0*_*.sql · 09_mart.sql    #   v1 PostgreSQL DDL(legacy,留作记录)
├── data/csv/                    # ① v1 种子数据(35 个 CSV,维度表仍复用它)
├── scripts/                     # ① 生成 / 装载 / 验证工具链
│   ├── gen/                      #   向量化生成器 → Parquet(约 8000 万行,budget.py 按 v1 等比 ×427)
│   ├── redshift/                 #   rsql.py(Data API 客户端)+ load_from_s3.py(COPY)
│   ├── glue/                     #   register_catalog.py + reconcile.py(三方漂移对账)
│   ├── audit/ · consistency/     #   数据质量审计 + 搬迁损耗快照
│   ├── deploy/                   #   build_catalog_json.py + deploy_web.sh(前端 + 元数据快照)
│   ├── ui/                       #   渲染契约测试(不需要浏览器)
│   └── test_all.sh               #   总闸:L0–L6
│
├── knowledge/                   # ② 知识库 · 数据字典 md 文档树(read_doc 读它,保持中文)
│   ├── README.md                 #   知识库说明 + 单一真源约定
│   ├── domains/_index.md         #   3 层:总索引→域索引→单表卡片
│   ├── domains/<域>/<表>.md      #   原始明细表(8 域)
│   ├── domains/mart/             #   治理层 4 张表卡片(text-to-insight)
│   ├── metrics/                  #   指标口径(含 governed_metrics.md 官方字典)
│   ├── analysis/                 #   5 个深度分析方法 SOP + 统计公式
│   └── relationships.md          #   表间关系
│
├── eval/                        # ② 评测 harness:21 条金标用例 + 基线(run_eval.py)
│
├── backend/                     # ③ Web App · 大脑(Agent SDK + Bedrock)
│   ├── agent.py                  #   系统提示 + 事件流解析
│   ├── tools.py                  #   MCP 工具:read_doc/run_sql/call_metric/compute_stats/present_result
│   ├── metrics_def.py · metric_layer.py · stats.py  # 指标即代码 + 统计计算
│   ├── db.py                     #   只读 SQL 安全边界(DB_BACKEND:redshift | postgres)
│   ├── catalog.py                #   /api/catalog:Glue + svv_* + knowledge/ + manifest → UI 元数据
│   ├── server.py                 #   FastAPI + SSE,托管前端
│   └── run.sh · Dockerfile · requirements.txt
├── web/index.html               # ③ Web App · 前端(渐进式披露 UI,含 EN/中 切换)
│
├── docker-compose.yml           # legacy v1:仅数据库容器
└── docker-compose.cloud.yml     # legacy v1:数据库 + FastAPI 应用两容器
```

## 数据概览

| 业务域 | 表数 | 代表表 |
|--------|------|--------|
| 用户域 | 5 | users, user_profiles, user_devices, user_segments, user_segment_members |
| 行为域 | 4 | events, sessions, page_views, event_definitions |
| 交易域 | 4 | orders, order_items, payments, subscriptions |
| 商品域 | 3 | products, categories, product_tags |
| 社交域 | 6 | posts, post_likes, post_comments, post_shares, user_follows, user_messages |
| 营销域 | 5 | campaigns, coupons, user_coupons, banners, push_notifications |
| 归因域 | 5 | channels, ad_campaigns, ad_creatives, channel_daily_costs, user_attributions |
| 实验域 | 3 | ab_tests, ab_test_variants, ab_test_assignments |

**35 张原始表、约 8000 万行**(最大几张:`post_likes` 1523 万、`page_views` 1289 万、`user_coupons` 971 万、`events` 854 万)。Glue Data Catalog 里合计 **48 张表**:再加治理集市(4 张 `mart_*`,口径冻结,见上面「两种范式」)、派生层(dwd/dws/ads)、一张 `meta_snapshot` 锚点表,以及两张故意埋的陷阱表(`orders_backup_20251201`、`tmp_campaign_roi_analysis`——目录里可见但不授权给 agent 的角色,考它会不会误用过期副本)。

生成器用**相对 v1 种子数据的倍数**表达规模(`scripts/gen/budget.py`),所以人均事件数、人均订单、归因覆盖缺口这些业务比例在 427 倍放大后原封不动,`knowledge/` 里写的口径陷阱在新数据上依然成立。v1 的完整 schema 记录:[database/00_schema_overview.md](database/00_schema_overview.md)。

> **时间口径提醒**:这是一份静态样本,数据落在 2025-10-26 ~ 2026-01-24。问"最近 N 天/近期"时,要以表自身时间列的 `max()` 作为"今天"锚点,**别用 `current_date`/`now()`**(会落在数据区间外查出空结果)。Web App 的系统提示已强制这一点。

## 能问什么

用户分析(DAU/MAU、留存、画像、分群)、交易分析(GMV、客单价、转化漏斗、订阅)、商品分析(销量排行、品类分布)、社交分析(内容互动、KOL、关系链)、营销分析(活动效果、优惠券核销、推送)、渠道分析(归因、CAC、ROI)、实验分析(A/B 变体对比)。

一份按难度分级的问题清单见 [test_questions.md](test_questions.md)。

## 安全

- **只读 SQL 边界**:所有生成的查询都经 `backend/db.py`——强制单条只读 `SELECT`/`WITH`(禁写关键字、语句超时、行数上限)。这道闸与后端无关,切 `DB_BACKEND` 不会削弱它。
- **治理下沉到数仓,两层独立防护**(`database/redshift/04_governance.sql`):SQL 校验挡得住写,但挡不住「用过高权限去读」,所以 v2 加了应用层被绕过也仍然生效的数据层约束——(1) 最小权限角色 `analytics_agent_ro`,只在需要的表上有 SELECT,私信表 `user_messages` 连表都不授权;(2) `users.email` / `users.phone` / `user_profiles.birth_date` 挂动态脱敏,以管理员身份实测查回 `***@masked.invalid`。两层都在查询时生效。一条实测出来的边界,写明而不藏着:Glue 目录是 **schema 投影,不是权限投影**——未授权的表在列表里照样可见,保证的是「发现了也读不到明文」,不是隐身。
- **没有数据库入站端口**:workgroup 保持 `publiclyAccessible=false`,查询全走 Redshift Data API(HTTPS + IAM)。容器里不存密码——运维操作用 Secrets Manager 托管密钥,agent 运行时用 IAM 临时凭证。
- **模型输出按不可信处理**:模型生成的文本、SQL 结果的列名和单元格值,进 `innerHTML` 前一律经 `esc()` 转义(`web/index.html`)。列名尤其要注意:提问可以把 agent 引导成 `SELECT 1 AS "<img src=x onerror=…>"`,所以表头和单元格一样转义。
- **可选 app 层认证**:云上部署前置 Amazon Cognito(SRP 登录 + JWKS 校验);本地默认不开(`AUTH_ENABLED` 不设)。
- **静态扫描抑制**:少量已知误报用 inline 注释抑制(`# nosec` / `# nosemgrep`):数据生成器用 `random` 造演示数据(非加密用途)、指标编译器从可信注册表拼 SQL(filter 值经转义)。均为经审阅的误报。

上报安全问题请按 [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications) 的指引,**不要**开公开 issue。

## 清理

默认路径连的是你自己建的 AWS 资源,用完请拆除:删 **Redshift Serverless workgroup 和 namespace**、**Glue federated catalog**、数据 S3 桶。Redshift Serverless 空闲不计费(只按实际消耗的 RPU-秒算),但仍要设 `base-capacity 4` 和每月 RPU 用量上限——服务默认基础容量是 128 RPU。**云上 Web 部署**还要额外拆:AgentCore Runtime、Fargate 中继 + ALB、Cognito 用户池、站点 S3 桶,并 disable + delete CloudFront 分发。步骤见 [docs/deployment.md](docs/deployment.md)。

## License

本项目采用 MIT-0(MIT No Attribution)许可,见 [LICENSE](LICENSE)。
