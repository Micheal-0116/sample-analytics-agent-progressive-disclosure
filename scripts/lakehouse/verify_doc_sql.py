#!/usr/bin/env python3
"""把 knowledge/**/*.md 里的 SQL 示例拿去 Athena 过一遍，看它们是不是真的还能跑。

## 为什么需要这个

progressive disclosure 的整个前提是：agent 读 `knowledge/` 里的文档来决定怎么写
SQL。那么文档里的 SQL 示例**就是代码**——它错了，agent 就照着错的写。但文档不会
被执行，所以它错了没有任何东西会响。这个脚本就是那个"响"。

迁移到 Trino 时这一点尤其要紧：`interval '2' week` 这种写法在 Postgres 下是对的、
读起来也像对的，只有真交给 Athena 才知道 Trino 没有 week 这个单位。

## 用 EXPLAIN 而不是真执行

`EXPLAIN <query>` 走完整的语法 + 目录 + 列名 + 类型解析，但**扫描 0 字节**。
它能抓到方言错、列名错、类型不匹配——也就是文档腐烂的绝大多数形态。

它抓不到的是**结果对不对**（口径、JOIN 基数、时间锚点），那不是本脚本的职责：
口径由 `backend/metrics_def.py` + `scripts/lakehouse/verify_mart_parity.py` 管。

## 故意跳过的

- 非 SELECT/WITH 的块（建表 DDL、bash、输出示例）
- 含参数占位符的（`?` 和 `:name`）——文档里故意留的坑位，不是完整语句
- `knowledge/connection.md` 的方言对照表：左列是**反例**，本来就不该能跑

用法：
    backend/.venv/bin/python scripts/lakehouse/verify_doc_sql.py
    backend/.venv/bin/python scripts/lakehouse/verify_doc_sql.py --only mart
退出码非 0 表示有文档里的 SQL 已经跑不动了。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from athena import Client  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
KNOWLEDGE = ROOT / "knowledge"
FENCE = re.compile(r"```sql\n(.*?)```", re.S)
# 参数占位符：`WHERE user_id = ?` / `= :target_user_id`。文档里的示范写法。
PLACEHOLDER = re.compile(r"(?<![:\w]):[A-Za-z_]\w*|\?")
WORKERS = int(os.getenv("DOC_SQL_WORKERS", "12"))


def collect(only: str | None = None) -> tuple[list[tuple[str, str]], int]:
    """把 md 里的 ```sql 块拆成单条 SELECT/WITH 语句。"""
    jobs: list[tuple[str, str]] = []
    skipped = 0
    for p in sorted(KNOWLEDGE.rglob("*.md")):
        rel = str(p.relative_to(ROOT))
        if only and only not in rel:
            continue
        for blk in FENCE.findall(p.read_text(encoding="utf-8")):
            for stmt in blk.split(";"):
                s = "\n".join(l for l in stmt.splitlines()
                              if not l.strip().startswith("--")).strip()
                if not s or not re.match(r"(?is)^\s*(select|with)\b", s):
                    continue
                if PLACEHOLDER.search(s):
                    skipped += 1
                    continue
                jobs.append((rel, s))
    return jobs, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="校验 knowledge/ 文档里的 SQL 能否被 Athena 接受")
    ap.add_argument("--only", help="只查路径里含该子串的文档")
    ap.add_argument("--verbose", action="store_true", help="打印每条失败语句的全文")
    a = ap.parse_args()

    jobs, skipped = collect(a.only)
    if not jobs:
        print("没有可校验的语句")
        return 0
    files = len({f for f, _ in jobs})
    print(f"共 {len(jobs)} 条 SELECT/WITH，来自 {files} 个文档"
          f"（跳过 {skipped} 条含参数占位符的）")

    cli = Client()

    def check(job: tuple[str, str]):
        f, s = job
        try:
            cli.execute("EXPLAIN " + s, fetch=False)
            return f, s, None
        except Exception as e:                       # noqa: BLE001
            return f, s, str(e).replace("\n", " ")

    with ThreadPoolExecutor(min(len(jobs), WORKERS)) as ex:
        results = list(ex.map(check, jobs))

    bad = [r for r in results if r[2]]
    print(f"通过 {len(results) - len(bad)} / {len(results)}")
    for f, s, err in bad:
        head = next((l for l in s.splitlines() if l.strip()), "")[:100]
        print(f"\n  ✗ {f}\n    {head}\n    {err[:240]}")
        if a.verbose:
            print("    ---\n" + "\n".join("    " + l for l in s.splitlines()))
    if bad:
        print(f"\n{len(bad)} 条文档 SQL 跑不动 ❌")
        return 1
    print("\n全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
