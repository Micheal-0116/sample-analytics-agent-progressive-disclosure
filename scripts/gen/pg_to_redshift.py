#!/usr/bin/env python3
"""Postgres SQL → Redshift 方言的定向改写（只做已知必需的几处）。

## 定位

**不是通用转译器**，只处理本项目 SQL 里实际出现且 Redshift 不支持的构造。
每加一条规则都要在 `_selftest()` 里配一个用例。不认识的写法原样保留，
让 Redshift 自己报错——静默"猜着改"比报错危险得多。

## 已实现的规则

| Postgres | Redshift | 出现处 |
|---|---|---|
| `AGG(expr) FILTER (WHERE cond)` | `AGG(CASE WHEN cond THEN expr END)` | 10_derived.sql 5 处 |

`count(*) FILTER (...)` 要特殊处理：`count(CASE WHEN cond THEN 1 END)`，因为
`CASE ... THEN * END` 不合法。`count(DISTINCT x) FILTER (...)` 则要把 DISTINCT
留在外面：`count(DISTINCT CASE WHEN cond THEN x END)`。

## 已确认**不需要**改写的（Redshift 原生支持）

`::` 类型转换、`date_trunc('week', d)`、`date + 整数`、`NULLIF`、`COALESCE`、
`NULL::numeric(p,s)`、窗口函数、`UNION`。

## 用法

    python3 scripts/gen/pg_to_redshift.py database/10_derived.sql --out database/redshift/03_derived.sql
    python3 scripts/gen/pg_to_redshift.py --selftest
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_AGG_HEAD = re.compile(r"(?i)\b([a-z_][a-z0-9_]*)\s*\(")


def _match_paren(s: str, open_idx: int) -> int:
    """给定 s[open_idx] == '('，返回配对右括号的下标。跳过字符串字面量。"""
    depth, i, n = 0, open_idx, len(s)
    while i < n:
        ch = s[i]
        if ch == "'":
            i += 1
            while i < n:
                if s[i] == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"括号不配对，起点 {open_idx}")


def rewrite_filter(sql: str) -> tuple[str, int]:
    """把 `AGG(expr) FILTER (WHERE cond)` 改写成 `AGG(CASE WHEN cond THEN expr END)`。

    返回 (新 SQL, 改写次数)。
    """
    count = 0
    out = sql
    while True:
        m = re.search(r"(?i)\)\s*FILTER\s*\(\s*WHERE\b", out)
        if not m:
            break
        # 1. 定位 FILTER 前那个聚合调用的括号范围
        agg_close = m.start()                     # 指向聚合的 ')'
        # 向前找到与之配对的 '('
        depth, i = 0, agg_close
        while i >= 0:
            if out[i] == ")":
                depth += 1
            elif out[i] == "(":
                depth -= 1
                if depth == 0:
                    break
            i -= 1
        if i < 0:
            raise ValueError("找不到聚合函数的左括号")
        agg_open = i
        # 函数名
        j = agg_open - 1
        while j >= 0 and out[j].isspace():
            j -= 1
        k = j
        while k >= 0 and (out[k].isalnum() or out[k] == "_"):
            k -= 1
        fname = out[k + 1:j + 1]
        inner = out[agg_open + 1:agg_close].strip()

        # 2. FILTER 的条件范围
        filt_open = out.index("(", m.start() + 1)
        filt_close = _match_paren(out, filt_open)
        cond = out[filt_open + 1:filt_close].strip()
        cond = re.sub(r"(?i)^WHERE\s+", "", cond).strip()

        # 3. 组装
        distinct = ""
        dm = re.match(r"(?i)^DISTINCT\s+(.*)$", inner, flags=re.DOTALL)
        if dm:
            distinct, inner = "DISTINCT ", dm.group(1).strip()
        if inner == "*":
            inner = "1"
        new = f"{fname}({distinct}CASE WHEN {cond} THEN {inner} END)"

        out = out[:k + 1] + new + out[filt_close + 1:]
        count += 1
    return out, count


def convert(sql: str) -> tuple[str, dict]:
    sql, n_filter = rewrite_filter(sql)
    return sql, {"FILTER→CASE": n_filter}


def _selftest() -> int:
    cases = [
        ("sum(actual_amount) FILTER (WHERE status IN ('paid','shipped'))",
         "sum(CASE WHEN status IN ('paid','shipped') THEN actual_amount END)"),
        ("count(*) FILTER (WHERE x > 0)",
         "count(CASE WHEN x > 0 THEN 1 END)"),
        ("count(DISTINCT user_id) FILTER (WHERE status = 'paid')",
         "count(DISTINCT CASE WHEN status = 'paid' THEN user_id END)"),
        # 条件里含括号与分号字面量
        ("sum(a) FILTER (WHERE f(b) = ';' AND c IN (1,2))",
         "sum(CASE WHEN f(b) = ';' AND c IN (1,2) THEN a END)"),
        # 同一行两处
        ("sum(a) FILTER (WHERE p) , count(*) FILTER (WHERE q)",
         "sum(CASE WHEN p THEN a END) , count(CASE WHEN q THEN 1 END)"),
        # 无 FILTER 时原样
        ("sum(actual_amount)", "sum(actual_amount)"),
    ]
    bad = 0
    for src, want in cases:
        got, _ = rewrite_filter(src)
        ok = " ".join(got.split()) == " ".join(want.split())
        print(f"  {'ok  ' if ok else 'FAIL'} {src[:56]}")
        if not ok:
            bad += 1
            print(f"       得到: {got}")
            print(f"       期望: {want}")
    print("全部通过" if not bad else f"{bad} 项失败")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?")
    ap.add_argument("--out")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if not a.src:
        ap.error("需要 src 或 --selftest")
    sql, stats = convert(Path(a.src).read_text(encoding="utf-8"))
    header = ("-- ⚙️ 由 scripts/gen/pg_to_redshift.py 从 " + a.src + " 转换而来\n"
              "-- DO NOT EDIT —— 改逻辑请改上游（schema_manifest.yaml → render.py）后重跑转换。\n"
              f"-- 改写统计：{stats}\n\n")
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(header + sql)
        print(f"→ {a.out}   改写统计 {stats}")
    else:
        print(header + sql)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
