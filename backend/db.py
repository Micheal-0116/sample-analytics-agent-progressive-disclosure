"""只读数据库访问层。

所有 NL→SQL 产生的查询都经 run_query 执行，强制：单条语句、只读、超时、行数上限。
这是 agent 的 SQL 安全边界。

## 两种后端

`DB_BACKEND` 环境变量选择：

- `redshift`（默认）—— Redshift Serverless via Data API。**不需要 VPC、不需要连接池、
  不需要在容器里存密码**，是 HTTPS + IAM 的 AWS API 调用。代价是异步（提交→轮询→取结果），
  由 `scripts/redshift/rsql.py` 封装成同步。v2 的正式形态。
- `postgres` —— psycopg 直连。**LEGACY，不再维护**：Aurora 已退役，只剩本地容器 rig
  （docker-compose.cloud.yml，约 19 万行、无 Glue 目录）。代码保留、不随 v2 演进，
  测试套件也不覆盖它。见 docs/legacy.md。

只读边界（`validate()`）两个后端共用，与后端无关：单条语句、仅 SELECT/WITH、
禁写关键字。切后端不会削弱这道闸。

## 两个后端的差异（已知且刻意保留）

- `statement_timeout`：Postgres 侧用 `set_config` 下发到会话；Data API 每条语句
  各自成会话，SET 不跨语句生效，所以 Redshift 侧的超时靠客户端轮询超时把关。
- 行数上限：两边都在取回后截断并置 `truncated` 标志，契约一致。
"""
from __future__ import annotations

import os
import re
import sys
import time
import asyncio
import datetime as _dt
import decimal
import uuid
from pathlib import Path
from typing import Any

PG = {
    "host": os.getenv("PGHOST", "127.0.0.1"),
    "port": int(os.getenv("PGPORT", "5433")),
    "dbname": os.getenv("PGDATABASE", "app_analytics"),
    "user": os.getenv("PGUSER", "postgres"),
    "password": os.getenv("PGPASSWORD", ""),
}
MAX_ROWS = int(os.getenv("SQL_MAX_ROWS", "1000"))
STMT_TIMEOUT_MS = int(os.getenv("SQL_TIMEOUT_MS", "15000"))
# 默认 redshift：Aurora 已经全搬走（见 docs/architecture-v2-redshift-glue.md），
# 默认指向一个不再维护的本地 Postgres 会让人第一步就撞上
# `ModuleNotFoundError: No module named 'psycopg'`——而真正的库在云上好着。
# 还需要 v1 本地路径的地方显式设 DB_BACKEND=postgres（docker-compose.cloud.yml 已钉）。
BACKEND = os.getenv("DB_BACKEND", "redshift").lower()

if BACKEND == "postgres":
    import psycopg
else:
    psycopg = None                                   # Redshift 路径不需要它

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"copy|vacuum|reindex|comment|merge|call|do|set|begin|commit)\b",
    re.IGNORECASE,
)


class SqlError(Exception):
    pass


def _strip(sql: str) -> str:
    # 去掉行/块注释与首尾空白、尾分号
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql.strip().rstrip(";").strip()


def validate(sql: str) -> str:
    s = _strip(sql)
    if not s:
        raise SqlError("空查询")
    if ";" in s:
        raise SqlError("只允许单条语句")
    if not re.match(r"(?is)^\s*(select|with)\b", s):
        raise SqlError("只允许 SELECT / WITH 查询")
    if _FORBIDDEN.search(s):
        raise SqlError("检测到非只读关键字，已拒绝")
    return s


def _jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, (_dt.timedelta,)):
        return str(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, memoryview):
        return v.tobytes().decode("utf-8", "replace")
    return v


# ---------------------------------------------------------------- Redshift 后端

_rs_client = None


def _redshift():
    """惰性拿 Data API 客户端（复用 scripts/redshift/rsql.py，避免两份实现）。"""
    global _rs_client
    if _rs_client is None:
        root = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(root / "scripts" / "redshift"))
        import rsql
        _rs_client = rsql.Client(
            workgroup=os.getenv("REDSHIFT_WORKGROUP", rsql.WORKGROUP),
            database=os.getenv("REDSHIFT_DATABASE", rsql.DATABASE),
            secret_arn=os.getenv("REDSHIFT_SECRET_ARN", rsql.SECRET_ARN),
        )
    return _rs_client


def _run_query_redshift(sql: str) -> dict:
    clean = validate(sql)                      # 只读闸门与 Postgres 路径共用
    t0 = time.perf_counter()
    res = _redshift().execute(clean, timeout=STMT_TIMEOUT_MS / 1000.0 * 4)
    exec_ms = round((time.perf_counter() - t0) * 1000, 1)
    rows = res.get("rows", [])
    truncated = len(rows) > MAX_ROWS
    return {"columns": res.get("columns", []),
            "rows": [[_jsonable(c) for c in r] for r in rows[:MAX_ROWS]],
            "rowcount": min(len(rows), MAX_ROWS),
            "truncated": truncated,
            "exec_ms": res.get("elapsed_ms", exec_ms)}


def _get_schema_redshift(tables: list[str]) -> str:
    safe = [t for t in tables if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", t)]
    if not safe:
        return "（无合法表名）"
    names = ", ".join("'" + t + "'" for t in safe)
    res = _redshift().execute(
        "SELECT table_name, column_name, data_type, is_nullable "
        "FROM information_schema.columns "
        f"WHERE table_schema='public' AND table_name IN ({names}) "
        "ORDER BY table_name, ordinal_position")
    by: dict[str, list[str]] = {}
    for t, col, dtype, nullable in res.get("rows", []):
        null = "" if nullable == "YES" else " NOT NULL"
        by.setdefault(t, []).append(f"  - {col}: {dtype}{null}")
    out = []
    for t in safe:
        out.append(f"## {t}\n" + "\n".join(by[t]) if t in by
                   else f"## {t}\n  （表不存在）")
    return "\n\n".join(out)


# ---------------------------------------------------------------- Postgres 后端

def _run_query_sync(sql: str) -> dict:
    if BACKEND == "redshift":
        return _run_query_redshift(sql)
    clean = validate(sql)
    with psycopg.connect(**PG, connect_timeout=8) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            # statement_timeout 用 set_config 参数化下发（STMT_TIMEOUT_MS 本就是内部 int
            # 常量、非用户输入，但参数化可彻底消除 SQL 字符串拼接的静态告警）。
            cur.execute("SELECT set_config('statement_timeout', %s, false)",
                        (str(int(STMT_TIMEOUT_MS)),))
            # 只计 execute+fetch 的纯 DB 耗时（不含建连/校验），用于前端把"SQL 真正
            # 执行时间"与"模型推理时间"拆开显示——这俩以前被混计在一个 stage 里。
            t0 = time.perf_counter()
            cur.execute(clean)
            cols = [d.name for d in cur.description] if cur.description else []
            fetched = cur.fetchmany(MAX_ROWS + 1)
            exec_ms = round((time.perf_counter() - t0) * 1000, 1)
            truncated = len(fetched) > MAX_ROWS
            rows = [[_jsonable(c) for c in r] for r in fetched[:MAX_ROWS]]
    return {"columns": cols, "rows": rows, "rowcount": len(rows),
            "truncated": truncated, "exec_ms": exec_ms}


async def run_query(sql: str) -> dict:
    return await asyncio.to_thread(_run_query_sync, sql)


def system_query(sql: str) -> list[tuple]:
    """服务端自己的元数据查询，返回原始行。**不给 agent 用。**

    与 `run_query` 的两点区别，都是刻意的：

    1. **不做 MAX_ROWS 截断。** `run_query` 截到 1000 行是为了保护前端和 token 预算，
       但元数据查询要的是完整集合——48 张表 × 平均 12 列已经 576 行，别人加几张表就会
       静默越界，然后 UI 少显示几个字段而不报错。这种"静默少一点"的 bug 最难发现。
    2. **不返回耗时/截断等展示字段**，调用方要的是数据本身。

    仍然过 `validate()`：这些 SQL 都写死在本仓库里、不含用户输入，但过闸门几乎零成本，
    而它保证「服务端也不会对数据库发起写操作」这条不变量没有例外通道。
    """
    clean = validate(sql)
    if BACKEND == "redshift":
        return [tuple(r) for r in _redshift().execute(clean).get("rows", [])]
    with psycopg.connect(**PG, connect_timeout=8) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(clean)
            return cur.fetchall()


def backend_info() -> dict:
    """当前后端的身份信息，给 /health 与 UI 顶栏用。

    原来 /health 无条件读 `PG` 这个字典，于是 `DB_BACKEND=redshift` 时前端顶栏会显示
    `127.0.0.1:5433`——库明明在东京的 Redshift 上。**坐标错了比不显示更糟**，因为它
    看起来是对的。这里按后端分派，并额外给出 `engine`，让 UI 不必自己拼引擎名
    （拼死的那份就是"PostgreSQL（实时）"一直挂在顶栏的由来）。
    """
    if BACKEND == "redshift":
        _redshift()                                  # 确保 rsql 已进 sys.path
        import rsql
        return {
            "engine": "Redshift Serverless",
            "name": os.getenv("REDSHIFT_DATABASE", rsql.DATABASE),
            "workgroup": os.getenv("REDSHIFT_WORKGROUP", rsql.WORKGROUP),
            "region": os.getenv("AWS_REGION", rsql.REGION),
            "transport": "Data API",                 # 没有 host/port：HTTPS + IAM
        }
    return {
        "engine": "PostgreSQL",
        "name": PG["dbname"],
        "host": PG["host"],
        "port": PG["port"],
        "transport": "psycopg",
    }


def _get_schema_sync(tables: list[str]) -> str:
    if not tables:
        return "（未指定表名）"
    if BACKEND == "redshift":
        return _get_schema_redshift(tables)
    safe = [t for t in tables if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", t)]
    if not safe:
        return "（无合法表名）"
    out: list[str] = []
    with psycopg.connect(**PG, connect_timeout=8) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            for t in safe:
                cur.execute(
                    """
                    SELECT column_name, data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name=%s
                    ORDER BY ordinal_position
                    """,
                    (t,),
                )
                rows = cur.fetchall()
                if not rows:
                    out.append(f"## {t}\n  （表不存在）")
                    continue
                lines = [f"## {t}"]
                for name, dtype, nullable in rows:
                    null = "" if nullable == "YES" else " NOT NULL"
                    lines.append(f"  - {name}: {dtype}{null}")
                out.append("\n".join(lines))
    return "\n\n".join(out)


async def get_schema(tables: list[str]) -> str:
    return await asyncio.to_thread(_get_schema_sync, tables)


def ping() -> bool:
    if BACKEND == "redshift":
        try:
            _redshift().execute("SELECT 1")
            return True
        except Exception:
            return False
    try:
        with psycopg.connect(**PG, connect_timeout=4) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:
        return False
