#!/usr/bin/env python3
"""从 S3 的 Parquet 分片 COPY 进 Redshift，并核对行数。

## COPY 的两个坑（都已处理）

1. **按位置映射**。官方文档：「COPY 按数据文件里列出现的顺序插入目标表的列」，
   列数必须相等。所以 Parquet 的列序必须等于 DDL 列序——生成侧由
   `scripts/gen/main.py: align_to_ddl()` 强制保证。
2. **不支持 REGION 参数**。Parquet COPY 走 Spectrum，桶必须与 Redshift 同区，
   显式写 REGION 反而会报 `REGION argument is not supported for PARQUET based COPY`。

## 用法

    python3 scripts/redshift/load_from_s3.py --secret <ARN> \\
        --s3 s3://analytics-agent-data-.../raw --role <IAM_ROLE_ARN>
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rsql  # noqa: E402

# 加载顺序无关紧要（Redshift 不强制外键），但按域分组便于阅读日志
TABLES = [
    "categories", "products", "product_tags", "channels", "event_definitions",
    "user_segments", "campaigns", "coupons", "banners", "ab_tests",
    "ab_test_variants", "ad_campaigns", "ad_creatives", "channel_daily_costs",
    "users", "user_profiles", "user_devices", "user_segment_members",
    "sessions", "events", "page_views",
    "posts", "post_likes", "post_comments", "post_shares", "user_follows",
    "user_messages",
    "orders", "order_items", "payments", "subscriptions",
    "user_attributions", "user_coupons", "push_notifications",
    "ab_test_assignments",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--s3", required=True, help="Parquet 根前缀，其下每表一个同名目录")
    ap.add_argument("--role", required=True, help="Redshift 用于读 S3 的 IAM role ARN")
    ap.add_argument("--secret", default=rsql.SECRET_ARN)
    ap.add_argument("--workgroup", default=rsql.WORKGROUP)
    ap.add_argument("--database", default=rsql.DATABASE)
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gen"))
    import arrow_types
    import ddl
    raw_types = ddl.parse_all_raw()

    c = rsql.Client(a.workgroup, a.database, a.secret)
    base = a.s3.rstrip("/")
    todo = a.only or TABLES
    t0, total = time.time(), 0
    failed = []

    for t in todo:
        # SUPER 列（JSONB / 数组）必须加 SERIALIZETOJSON，否则报
        # "SUPER column in COPY query requires SERIALIZETOJSON option"
        extra = (" SERIALIZETOJSON"
                 if arrow_types.needs_serializetojson(raw_types.get(t, [])) else "")
        sql = (f"COPY {t} FROM '{base}/{t}/' "
               f"IAM_ROLE '{a.role}' FORMAT AS PARQUET{extra}")
        t1 = time.time()
        try:
            # 幂等：重跑不叠加。TRUNCATE 在 Redshift 里是 DDL，会隐式提交。
            c.execute(f"TRUNCATE TABLE {t}", timeout=600, fetch=False)
            c.execute(sql, timeout=3600, fetch=False)
        except rsql.RedshiftError as e:
            print(f"  {t:<24} FAIL  {str(e)[:300]}")
            failed.append(t)
            continue
        n = c.execute(f"SELECT count(*) FROM {t}")["rows"][0][0]
        total += n
        print(f"  {t:<24} {n:>12,} 行  {time.time()-t1:>6.1f}s", flush=True)

    print(f"\n合计 {total:,} 行，耗时 {time.time()-t0:.1f}s")
    if failed:
        print(f"失败 {len(failed)} 张：{failed}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
