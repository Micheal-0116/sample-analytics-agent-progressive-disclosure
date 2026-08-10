#!/bin/bash
# 启动 App Analytics Agent 后端。
#
# 两种数据后端，用 DB_BACKEND 切：
#
#   redshift（默认）—— Redshift Serverless via Data API。v2 的正式形态：8000 万行、
#                      元数据在 Glue Data Catalog、治理（GRANT + 脱敏）在数仓里。
#   postgres        —— 本地 Postgres rig。**LEGACY，不再维护**：约 19 万行，没有 Glue
#                      目录，UI 元数据会降级到 information_schema，治理也不在数仓里。
#                      保留可跑，但不随 v2 演进，测试套件不覆盖。见 docs/legacy.md。
#
# 之所以把默认值改成 redshift：Aurora 已经全搬走了（见 docs/architecture-v2-redshift-glue.md），
# 默认指向一个不再维护的本地库会让克隆仓库的人第一步就卡住。
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(cd "$HERE/.." && pwd)"

# 本地覆盖（gitignored，见 .env.local.example）。仓库里不放真实账号/资源 ID。
# set -a：uvicorn 是子进程，不 export 它看不到这些值。
[[ -f "$PROJ/.env.local" ]] && { set -a; source "$PROJ/.env.local"; set +a; }

export DB_BACKEND="${DB_BACKEND:-redshift}"

# —— Bedrock / 模型 ——
export CLAUDE_CODE_USE_BEDROCK=1
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-global.anthropic.claude-opus-4-8}"

if [[ "$DB_BACKEND" == "redshift" ]]; then
  # Redshift Serverless 在东京。Data API 走 HTTPS + IAM：没有 host/port，
  # 不进 VPC，不需要连接池，也没有密码落地。
  export AWS_REGION="${AWS_REGION:-ap-northeast-1}"
  export REDSHIFT_WORKGROUP="${REDSHIFT_WORKGROUP:-analytics-agent-wg}"
  export REDSHIFT_DATABASE="${REDSHIFT_DATABASE:-app_analytics}"

  ACCT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)
  if [[ ! "$ACCT" =~ ^[0-9]{12}$ ]]; then
    echo "[run] ✗ 没有可用的 AWS 凭证。Data API 靠 IAM 认证，先登录：" >&2
    echo "        aws sso login   （或设好 AWS_PROFILE）" >&2
    exit 1
  fi
  # UI 的元数据来源，ID = <账号>:<catalog 名>，账号刚从 sts 拿到，不写死。
  # 不配也能跑，只是 /api/catalog 会降级到 information_schema
  # 并在界面上标出来（少了「元数据来自统一目录」这个演示点）。
  export GLUE_CATALOG_ID="${GLUE_CATALOG_ID:-$ACCT:analytics_agent_rs}"
  echo "[run] backend=redshift  workgroup=$REDSHIFT_WORKGROUP  db=$REDSHIFT_DATABASE"
  echo "[run] region=$AWS_REGION  glue=$GLUE_CATALOG_ID"
else
  PGBIN=/opt/homebrew/opt/postgresql@16/bin
  export AWS_REGION="${AWS_REGION:-us-east-1}"
  export PGHOST=127.0.0.1
  export PGPORT="${PGPORT:-5433}"
  export PGDATABASE=app_analytics
  export PGUSER=postgres
  if ! "$PGBIN/pg_ctl" -D "$HERE/.pgdata" status >/dev/null 2>&1; then
    echo "[run] 启动本地 Postgres ..."
    "$PGBIN/pg_ctl" -D "$HERE/.pgdata" \
      -o "-p $PGPORT -k /tmp -c listen_addresses=127.0.0.1" \
      -l "$HERE/.pgdata/server.log" -w start
  fi
  echo "[run] backend=postgres  db=$PGHOST:$PGPORT  （LEGACY 路径，约 19 万行，不再维护）"
fi

echo "[run] model=$ANTHROPIC_MODEL"
echo "[run] 打开 http://127.0.0.1:${PORT:-8000}/"
cd "$HERE"
exec "$HERE/.venv/bin/python" -m uvicorn server:app --host 127.0.0.1 --port "${PORT:-8000}"
