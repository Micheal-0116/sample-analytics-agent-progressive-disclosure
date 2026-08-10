#!/usr/bin/env python3
"""数据质量审计 runner —— 按层跑 SQL 并打印每一条的结果集。

## 为什么不直接用 rsql.py --file

那条路径用 `fetch=False`，只打「ok + 耗时」不打结果集：它是给 DDL 装载用的，
装载只需要知道语句成没成功。审计要看每条的输出，所以这里复用 rsql.py 的
`split_statements`（字符级扫描，注释和字符串里的分号不当分隔符）和 `Client`，
把 fetch 打开，并把每条语句开头的 `--` 注释当标题打出来。

## 用法

    python3 scripts/audit/run.py L1                 # 跑一层
    python3 scripts/audit/run.py L1 L3 L4           # 跑若干层
    python3 scripts/audit/run.py all                # 全跑
    python3 scripts/audit/run.py L4 --secret "$SEC" # 指定托管密钥

不带 `--secret` 走 IAM 临时凭证（见 knowledge/connection.md）。

## 边界

全部语句都是只读 `SELECT`，不建表、不改数据。层与层之间无依赖，可单独跑。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "redshift"))

from rsql import (  # noqa: E402
    DATABASE,
    SECRET_ARN,
    WORKGROUP,
    Client,
    RedshiftError,
    _print_table,
    split_statements,
)


def label_of(stmt: str) -> str:
    """取语句开头连续的 `--` 注释行作为标题；没有就用 SQL 前 60 字符。"""
    lines = []
    for line in stmt.splitlines():
        s = line.strip()
        if s.startswith("--"):
            lines.append(s.lstrip("-").strip())
        elif s:
            break
    if lines:
        return " / ".join(lines)
    return " ".join(stmt.split())[:60]


def layer_files(names: list[str]) -> list[pathlib.Path]:
    allf = sorted(HERE.glob("L[0-9]*.sql"))
    if not allf:
        sys.exit(f"没找到审计 SQL：{HERE}/L*.sql")
    if names == ["all"]:
        return allf
    out = []
    for n in names:
        hit = [f for f in allf if f.name.startswith(n.upper() + "_") or f.stem == n]
        if not hit:
            sys.exit(f"未知层 {n}，可选：{', '.join(f.stem for f in allf)} 或 all")
        out.extend(hit)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("layers", nargs="+", help="层名（L1/L2/...）或 all")
    ap.add_argument("--workgroup", default=WORKGROUP)
    ap.add_argument("--database", default=DATABASE)
    ap.add_argument("--secret", default=SECRET_ARN)
    ap.add_argument("--timeout", type=float, default=1800.0)
    a = ap.parse_args()

    c = Client(a.workgroup, a.database, a.secret)
    failed = 0

    for f in layer_files(a.layers):
        stmts = split_statements(f.read_text(encoding="utf-8"))
        print(f"\n{'=' * 78}\n{f.stem}  （{len(stmts)} 项）\n{'=' * 78}")
        for i, s in enumerate(stmts, 1):
            print(f"\n── [{i}/{len(stmts)}] {label_of(s)}")
            body = re.sub(r"^\s*(--[^\n]*\n)+", "", s).strip()
            if not body:
                continue
            try:
                _print_table(c.execute(body, timeout=a.timeout))
            except RedshiftError as e:
                print(f"   FAIL: {e}")
                failed += 1

    if failed:
        print(f"\n{failed} 条语句执行失败（是 SQL 或权限问题，不代表数据质量结论）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
