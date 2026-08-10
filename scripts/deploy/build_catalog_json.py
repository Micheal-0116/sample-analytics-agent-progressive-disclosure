#!/usr/bin/env python3
"""把 /api/catalog 的响应固化成静态 web/catalog.json，供线上前端直接取。

## 为什么线上要用静态快照，而不是像本地那样打接口

线上前端由 CloudFront 托管:默认行为回源 S3,只有 `/ask` 这一条路径走 VPC origin →
内网 ALB → Fargate relay。而 relay(functions/ask-relay/server.mjs)只实现了
`/health` 与 `/ask`,`/api/catalog` 打过去落到 S3,拿到 404。

要让线上有实时接口,得在 relay 里用 JS 重写一遍 catalog.py(还要把 knowledge/domains/
与 schema_manifest.yaml 打进镜像)、给 task role 加 Glue + Redshift Data API 权限、
再走 CodeBuild → ECR → ECS 换版本。代价不只是工作量:组装逻辑会变成 Python 和 JS
两份,而**没有任何测试盯着这两份别跑偏**——元数据静默失真正是我们一直在修的毛病。

所以走这条:部署期用**同一个** backend/catalog.py 生成快照。数据仍然是真从 Glue
查的,只是时间点固定在部署那一刻;`snapshot: true` 会让 UI 明说"部署期快照"而不是
假装实时。想升级成真实时,补 relay 路由即可,前端已经是 /api/catalog 优先。

## 那道闸

默认**拒绝写出降级快照**。Glue 挂了时 catalog.py 会退到 information_schema——
本地开发无所谓,推上线就等于把错的表清单和 0 行数固化给所有访客看,而且不报警。
宁可让部署失败。确实要发降级版本时显式加 --allow-degraded。

用法:
    python3 scripts/deploy/build_catalog_json.py            # 写 web/catalog.json
    python3 scripts/deploy/build_catalog_json.py --print    # 只看摘要不落盘
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# .env.local（gitignored）:与三个 bash 脚本同一份本地覆盖。经 deploy_web.sh 调用时
# 值已 export 进环境,这里解析是为了**单独跑**时行为一致。已有的环境变量优先。
_envf = ROOT / ".env.local"
if _envf.exists():
    for _line in _envf.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# 这些必须在 import catalog / db 之前设好:两个模块在导入期就读环境变量。
DEFAULTS = {
    "DB_BACKEND": "redshift",
    "AWS_REGION": "ap-northeast-1",
    "REDSHIFT_WORKGROUP": "analytics-agent-wg",
    "REDSHIFT_DATABASE": "app_analytics",
}
for k, v in DEFAULTS.items():
    os.environ.setdefault(k, v)

# Glue catalog ID = <账号>:<catalog 名>。账号不硬编码,从当前凭证现算——
# 仓库里不放真实账号 ID,而克隆的人跑出来的本来就该是他们自己的账号。
# EXPECT_ACCOUNT 是可选守卫(通常在 .env.local):设了就核对,防多账号串号。
_expect = os.environ.get("EXPECT_ACCOUNT")
if _expect or not os.environ.get("GLUE_CATALOG_ID"):
    import boto3                                          # noqa: E402
    _acct = boto3.client("sts").get_caller_identity()["Account"]
    if _expect and _acct != _expect:
        raise SystemExit(f"✗ 当前凭证账号 {_acct},期望 {_expect}(.env.local 里钉的)")
    os.environ.setdefault("GLUE_CATALOG_ID", f"{_acct}:analytics_agent_rs")

sys.path.insert(0, str(ROOT / "backend"))

# 最低期望值:低于这些就说明查漏了,不该发布。48 张表 / 7992 万行是 v2 的实际规模,
# 这里留一点余量,防止以后加表就要改脚本;但要能挡住"只查到几张"这种明显残缺。
MIN_TABLES = 40
MIN_ROWS = 50_000_000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "web" / "catalog.json"))
    ap.add_argument("--print", action="store_true", help="只打印摘要，不写文件")
    ap.add_argument("--allow-degraded", action="store_true",
                    help="允许写出非 Glue 来源或规模不足的快照（默认拒绝）")
    args = ap.parse_args()

    try:
        import catalog
    except Exception as e:                                    # noqa: BLE001
        print(f"✗ 导入 backend/catalog.py 失败: {type(e).__name__}: {e}")
        print("  依赖装在 backend/.venv,用它跑:")
        print("  ./backend/.venv/bin/python scripts/deploy/build_catalog_json.py")
        return 1

    t0 = time.time()
    print(f"查 Glue catalog {os.environ['GLUE_CATALOG_ID']} "
          f"({os.environ['AWS_REGION']})…")
    try:
        data = catalog.build(refresh=True)
    except Exception as e:                                    # noqa: BLE001
        print(f"✗ 组装失败: {type(e).__name__}: {e}")
        return 1

    tot = data.get("totals") or {}
    tables, rows = tot.get("tables") or 0, tot.get("rows") or 0
    src = data.get("source")
    print(f"  来源={src} 引擎={data.get('engine')} "
          f"表={tables} 行={rows:,} 域={tot.get('domains')} "
          f"耗时 {time.time() - t0:.1f}s")
    if data.get("warning"):
        print(f"  降级原因: {data['warning']}")

    problems = []
    if src != "glue":
        problems.append(f"元数据来源是 {src},不是 glue(Glue 查询失败后的降级路径)")
    if tables < MIN_TABLES:
        problems.append(f"只查到 {tables} 张表,低于最低期望 {MIN_TABLES}")
    if rows < MIN_ROWS:
        problems.append(f"base 层合计 {rows:,} 行,低于最低期望 {MIN_ROWS:,}")

    if problems:
        print("\n✗ 快照不合格,拒绝写出:")
        for p in problems:
            print(f"   - {p}")
        if not args.allow_degraded:
            print("\n  推上线等于把错的元数据固化给所有访客,且界面不会报警。")
            print("  先修数据源;确实要发降级版本再加 --allow-degraded。")
            return 1
        print("\n  --allow-degraded:仍然写出。UI 会显示降级来源。")

    # snapshot 让前端能诚实区分"部署期快照"与"实时接口",别把快照说成实时。
    data["snapshot"] = True
    data["generated_at_utc"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + " UTC"

    # catalog_id 掐掉账号前缀:这个文件会提交进公开仓,而且线上是 CloudFront 直出的
    # 公开静态文件(静态路径不过 Cognito,任何人都能 GET /catalog.json)。
    # 前端不读这个字段(只用 totals/domains/governance),留 catalog 名纯粹是溯源。
    if data.get("catalog_id") and ":" in data["catalog_id"]:
        data["catalog_id"] = data["catalog_id"].split(":", 1)[1]

    if args.print:
        print("\n（--print:未写文件）")
        return 0

    out = Path(args.out)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n✓ 已写 {out.relative_to(ROOT)}  "
          f"{out.stat().st_size / 1024:.1f} KB  ({data['generated_at_utc']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
