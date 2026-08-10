"""`/api/catalog` 的数据装配：把元数据的三个来源聚成一份 UI 能直接渲染的结构。

## 为什么要有这个接口

前端原来把表清单、表数、行数、字段全部写死在 HTML 和 i18n 字典里（39 张表、
「~19万行」、「PostgreSQL」）。数据涨到 8000 万行、表数到 48 之后这些数字全错了，
而且**错得看不出来**——它们是静态文本，不会因为库变了就报警。

上 Glue Data Catalog 的意义正在于元数据有了一份机器可读的实际态。所以 UI 不该再自己
抄一份，而应该读它。这个接口就是那条通道：

    Glue Data Catalog     → 表清单 + 字段（**实际态**：库里到底有什么）
    Redshift svv_*         → 行数估算 + 治理现状（GRANT 覆盖面、脱敏策略）
    knowledge/domains/     → 域分组（agent 读的就是这套目录结构，UI 与它同源）
    schema_manifest.yaml   → 派生层的 layer / status（dwd / dws / ads / noise）

四个来源各管一段，没有一段是前端硬编码的。改了库、加了表、挂了新脱敏策略，
UI 下次刷新自动跟上。

## 行数为什么用 svv_table_info 而不是 count(*)

`count(*)` 扫 8000 万行要几十秒，而且 48 张表串起来会把接口拖成分钟级。
`svv_table_info.tbl_rows` 是 Redshift 自己维护的统计值，2–3 秒返回全部表，
实测与生成器的精确值完全吻合（post_likes 15,231,627、page_views 12,894,870 …）。
它本质是估算，静态数据集上准确；如果库在持续写入，这里显示的就是近似值——
UI 上标了「约」。

## 降级行为

Glue 读不到时（没建 catalog、没配 `GLUE_CATALOG_ID`、或权限不足）**不报错**，
退回 `information_schema` 并在响应里把 `source` 标成 `information_schema`。
克隆本仓库但没做 Glue 那一步的人照样能跑起来看到正确数字，只是少了「元数据来自
统一目录」这个演示点。静默降级是有害的，所以 `source` 一定回传，UI 会显示出来。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import db

ROOT = Path(__file__).resolve().parent.parent

# 目录内容变化很慢（改表结构、加脱敏策略这种动作），但 UI 每次开页都会拉一次。
# 缓存把 Glue + 4 条 Redshift 查询压成偶发成本；演示时改了库想立刻看到，
# 用 /api/catalog?refresh=1 强制重算。
CACHE_TTL = float(os.getenv("CATALOG_CACHE_TTL", "300"))

GLUE_CATALOG_ID = os.getenv("GLUE_CATALOG_ID", "")
REGION = os.getenv("AWS_REGION", "ap-northeast-1")
# agent 实际使用的库角色，治理面板按它统计（与 04_governance.sql 保持一致）
AGENT_ROLE = os.getenv("AGENT_DB_ROLE", "analytics_agent_ro")

# 域的展示顺序。跟前端原有的卡片顺序一致，避免每次刷新顺序乱跳；
# 出现不在此列的新域时追加到末尾，不丢。
DOMAIN_ORDER = ["user", "behavior", "transaction", "product", "social",
                "marketing", "attribution", "experiment", "mart"]

_cache: dict | None = None
_cache_at = 0.0


# ------------------------------------------------------------------ 实际态

def _glue_tables() -> dict[str, list[str]]:
    """从 Glue Data Catalog 读表清单与字段。读不到就抛，由调用方降级。

    用 `GetTables`（列表）而不是逐表 `GetTable`：列表路径一次拿全并自带完整字段，
    而 `GetTable` 在挂了 DDM 的表上会报 `EntityNotFound`（见
    docs/architecture-v2-redshift-glue.md 第三节）。想要完整视图就得走列表路径。
    """
    if not GLUE_CATALOG_ID:
        raise RuntimeError("未配置 GLUE_CATALOG_ID")
    import boto3
    g = boto3.client("glue", region_name=REGION)

    # federated catalog 比普通 Glue catalog 多一层：Redshift 的 database 是子 catalog，
    # schema 才映射成 Glue 的 database。顶层直接 get_databases 取不到东西。
    leaves = [c["CatalogId"] for c in
              g.get_catalogs(ParentCatalogId=GLUE_CATALOG_ID).get("CatalogList", [])
              if not c["CatalogId"].endswith("/dev")] or [GLUE_CATALOG_ID]

    out: dict[str, list[str]] = {}
    for cat in leaves:
        for dbname in [d["Name"] for d in
                       g.get_databases(CatalogId=cat).get("DatabaseList", [])]:
            tok = None
            while True:
                kw = {"CatalogId": cat, "DatabaseName": dbname}
                if tok:
                    kw["NextToken"] = tok
                r = g.get_tables(**kw)
                for t in r.get("TableList", []):
                    out[t["Name"]] = [c["Name"] for c in
                                      t.get("StorageDescriptor", {}).get("Columns", [])]
                tok = r.get("NextToken")
                if not tok:
                    break
    if not out:
        raise RuntimeError("Glue 目录里没有表")
    return out


def _schema_tables() -> dict[str, list[str]]:
    """降级路径：直接读数据库的 information_schema。"""
    rows = db.system_query(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public' ORDER BY table_name, ordinal_position")
    out: dict[str, list[str]] = {}
    for t, c in rows:
        out.setdefault(t, []).append(c)
    return out


def _row_counts() -> dict[str, int]:
    """行数估算。取不到就返回空 dict，UI 不显示行数而不是显示 0。"""
    try:
        if db.BACKEND == "redshift":
            rows = db.system_query(
                'SELECT "table", tbl_rows FROM svv_table_info WHERE "schema"=\'public\'')
        else:
            rows = db.system_query(
                "SELECT relname, reltuples FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public' AND c.relkind='r'")
        return {t: int(float(n or 0)) for t, n in rows}
    except Exception:
        return {}


# ------------------------------------------------------------------ 治理现状

def _governance() -> dict:
    """agent 角色的授权面 + 脱敏策略。两者都从库里现查，不写死。

    这一段是 UI 治理面板的数据源。它回答的是客户最常问的那个问题：
    「AI 会不会不小心读到 PII」——答案不是"我们叮嘱它别读"，而是这里列出来的硬约束。
    """
    out = {"role": AGENT_ROLE, "granted": [], "masked": [], "available": False}
    if db.BACKEND != "redshift":
        return out                      # svv_* 是 Redshift 系统视图，Postgres 没有
    try:
        out["granted"] = sorted({
            r[0] for r in db.system_query(
                "SELECT relation_name FROM svv_relation_privileges "
                f"WHERE identity_name='{AGENT_ROLE}' AND privilege_type='SELECT'")})
        out["masked"] = [
            {"table": t, "columns": _parse_cols(cols), "policy": p}
            for p, t, cols in db.system_query(
                "SELECT policy_name, table_name, input_columns "
                "FROM svv_attached_masking_policy ORDER BY table_name, policy_name")]
        out["available"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _parse_cols(raw) -> list[str]:
    """`input_columns` 回来的是 `["email"]` 这种 JSON 文本，抽出列名即可。"""
    return re.findall(r'"([^"]+)"', str(raw or "")) or ([str(raw)] if raw else [])


# ------------------------------------------------------------------ 分组与分层

def _domain_map() -> dict[str, str]:
    """表 → 域。来源是 `knowledge/domains/<域>/<表>.md` 的目录结构。

    刻意与 agent 读的是同一套目录：UI 上的分组和 agent 的路由结构同源，
    演示时「左边看到的域」就是「它按域去翻文档」的那个域，不会各说一套。
    """
    out: dict[str, str] = {}
    base = ROOT / "knowledge" / "domains"
    if not base.is_dir():
        return out
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        for f in d.glob("*.md"):
            if not f.stem.startswith("_"):
                out[f.stem] = d.name
    return out


def _layer_map() -> dict[str, dict]:
    """表 → {layer, status, summary}。派生层读 schema_manifest.yaml，其余按名字推断。"""
    out: dict[str, dict] = {}
    mf = ROOT / "schema_manifest.yaml"
    if mf.is_file():
        try:
            import yaml
            spec = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
            for t in spec.get("tables", []) or []:
                out[t["name"]] = {
                    "layer": t.get("layer", "base"),
                    "status": t.get("status", "active"),
                    "summary": t.get("summary", ""),
                }
        except Exception:
            pass
    return out


def _infer_layer(name: str, declared: dict) -> dict:
    if name in declared:
        return declared[name]
    if name.startswith("mart_"):
        return {"layer": "mart", "status": "active", "summary": "口径已冻结的预聚合表"}
    if name == "meta_snapshot":
        return {"layer": "meta", "status": "active",
                "summary": "数据集「今天」的锚点（静态样本铁律）"}
    return {"layer": "base", "status": "active", "summary": ""}


# ------------------------------------------------------------------ 装配

def build(refresh: bool = False) -> dict:
    global _cache, _cache_at
    if _cache is not None and not refresh and (time.time() - _cache_at) < CACHE_TTL:
        return _cache

    source, warn = "glue", None
    try:
        tables = _glue_tables()
    except Exception as e:
        # 降级但不静默：source 会回传给前端并显示出来。
        source, warn = "information_schema", f"{type(e).__name__}: {e}"
        tables = _schema_tables()

    counts = _row_counts()
    gov = _governance()
    dom = _domain_map()
    declared = _layer_map()
    granted = set(gov.get("granted") or [])
    masked_by_table: dict[str, list[str]] = {}
    for m in gov.get("masked") or []:
        masked_by_table.setdefault(m["table"], []).extend(m["columns"])

    grouped: dict[str, list[dict]] = {}
    for name in sorted(tables):
        meta = _infer_layer(name, declared)
        # 没有域卡片的表默认落 "other"——那是"有表没文档"的信号，应该看得见。
        # 唯一的例外是 meta_snapshot：它是全局日期锚点，不属于任何业务域，
        # 成文在 knowledge/connection.md（与 reconcile.py 的 CARD_EXEMPT 同一处约定）。
        key = dom.get(name) or ("meta" if meta["layer"] == "meta" else "other")
        grouped.setdefault(key, []).append({
            "name": name,
            "columns": tables[name],
            "column_count": len(tables[name]),
            "rows": counts.get(name),
            "layer": meta["layer"],
            "status": meta["status"],
            "summary": meta["summary"],
            # granted 只在能查到治理信息时才有意义；查不到时给 None 让 UI 别显示，
            # 而不是显示成"未授权"（那是把"不知道"渲染成了"否"，比不显示更糟）。
            "granted": (name in granted) if gov.get("available") else None,
            "masked_columns": sorted(set(masked_by_table.get(name, []))),
        })

    order = DOMAIN_ORDER + [k for k in sorted(grouped) if k not in DOMAIN_ORDER]
    domains = [{"key": k, "tables": grouped[k], "table_count": len(grouped[k])}
               for k in order if k in grouped]

    # 行数分两个：headline 只算 base 层，因为**派生层是基表的副本或汇总**，
    # 加在一起会把同一批事实重复计。`dwd_events_app` 就是 `events` 的过滤版，
    # 两者行数几乎相同；48 张表硬加得 9649 万，而数据集真实规模是 7992 万。
    # 报大数字很诱人，但那个数字经不起「这些行是同一批事实吗」这一问。
    base_names = {t["name"] for tabs in grouped.values() for t in tabs
                  if t["layer"] == "base"}
    rows_base = sum(v for k, v in counts.items() if k in base_names and v)
    rows_all = sum(v for v in counts.values() if v)
    info = db.backend_info()
    out = {
        "source": source,                    # glue | information_schema
        "warning": warn,                     # 降级原因，前端会显示
        "catalog_id": GLUE_CATALOG_ID or None,
        "engine": info.get("engine"),
        "database": info.get("name"),
        "transport": info.get("transport"),
        "region": info.get("region"),
        "totals": {
            "tables": len(tables),
            "rows": rows_base,           # 明细事实规模（UI 顶栏/欢迎页用这个）
            "rows_all_layers": rows_all,  # 含派生层，会重复计同一批事实，仅供参考
            "domains": len(domains),
            "columns": sum(len(v) for v in tables.values()),
            # 分层计数：让 UI 能说清"48 张表里有几张是派生层/噪音表"
            "by_layer": _count_by(grouped, "layer"),
        },
        "governance": {
            "role": gov.get("role"),
            "available": gov.get("available"),
            "granted_tables": len(granted),
            "ungranted_tables": sorted(set(tables) - granted) if gov.get("available") else [],
            "masked": gov.get("masked") or [],
            "error": gov.get("error"),
        },
        "domains": domains,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _cache, _cache_at = out, time.time()
    return out


def _count_by(grouped: dict[str, list[dict]], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for tabs in grouped.values():
        for t in tabs:
            out[t[field]] = out.get(t[field], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


if __name__ == "__main__":
    import json
    d = build()
    print(json.dumps({k: v for k, v in d.items() if k != "domains"},
                     ensure_ascii=False, indent=2))
    for dm in d["domains"]:
        names = ", ".join(f"{t['name']}({t['layer']})" for t in dm["tables"])
        print(f"\n{dm['key']:<12} {dm['table_count']:>2} 张  {names}")
