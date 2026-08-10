#!/usr/bin/env python3
"""校验 DDL 声明态与 Redshift 实际建出来的表逐列一致（含列顺序）。

## 为什么列顺序也要比

Redshift 的 `COPY ... FORMAT AS PARQUET` **按位置映射，不按列名**。列顺序错了不报错，
数据会静默灌进相邻的列——比崩掉难查得多。所以这里不只比列集合，还比 `ordinal_position`
的顺序，把「顺序漂移」这个静默故障挡在灌数之前。

## 与 reconcile.py 的分工

- 本脚本：DDL ⟷ **Redshift `information_schema`**（数据库自己说它有什么）
- reconcile.py：DDL ⟷ **Glue Catalog** ⟷ 知识库卡片（三方，含语义层）

两条路径的"实际态"来源不同：一个直连数据库元数据，一个走 Glue 的投影。都对上了，
才说明「DDL → 建表 → 注册目录」这条链每一段都没走形。Glue 那一环出问题（注册滞后、
投影不全）不会被本脚本发现，反之数据库层的列顺序问题也不会被 reconcile 发现。

用法：

    python3 scripts/gen/verify_ddl_vs_redshift.py
    # exit 0 = 一致；exit 1 = 有差异（可挂 CI）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "gen"))
sys.path.insert(0, str(ROOT / "scripts" / "redshift"))


def main() -> int:
    import ddl
    import rsql

    declared = ddl.parse_all()
    rows = rsql.Client().execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public' ORDER BY table_name, ordinal_position"
    )["rows"]

    actual: dict[str, list[str]] = {}
    for table, col in rows:
        actual.setdefault(table, []).append(col)

    bad = 0
    for t, cols in sorted(declared.items()):
        want = [c for c, _ in cols]
        got = actual.get(t)
        if got is None:
            print(f"  ❌ 缺表 {t}：DDL 声明了但 Redshift 里没有")
            bad += 1
            continue
        if want == got:
            continue
        bad += 1
        only_ddl, only_rs = set(want) - set(got), set(got) - set(want)
        if only_ddl or only_rs:
            print(f"  ❌ {t} 列集合不一致：仅 DDL={sorted(only_ddl)} "
                  f"仅 Redshift={sorted(only_rs)}")
        else:
            # 集合相同但顺序不同——这正是 Parquet COPY 会静默灌错列的情形
            first = next(i for i, (a, b) in enumerate(zip(want, got)) if a != b)
            print(f"  ❌ {t} 列**顺序**不一致（COPY 会灌错列）：第 {first + 1} 列 "
                  f"DDL={want[first]} 实际={got[first]}")

    print(f"\nDDL 声明 {len(declared)} 张表 / Redshift public 共 {len(actual)} 张表"
          f"（差值是 mart / 派生层，不由 DDL 声明）")
    if bad:
        print(f"❌ {bad} 张表有差异")
        return 1
    print("一致 ✅  列名与顺序全等")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
