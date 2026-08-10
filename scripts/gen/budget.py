"""规模预算 —— scale 因子与各表目标行数的单一定义处。

## 设计原则：以「当前已提交数据」为基准的倍数，而不是各表拍绝对值

`BASE` 里的行数是当前 `data/csv` 灌进库后实测出来的（见
`eval/baseline/consistency.postgres.json`）。放大时按表的性质分三类缩放，
好处是**所有业务比例原封不动**：人均 40 事件 / 60 页面浏览 / 4 订单 / 71 点赞、
归因覆盖缺口、社交互动比例，全都等比保持。知识库里写的那些口径特征
（「约六成 GMV 未归因」「残周残月别直接比」）因此在放大后自然重现，不用逐个复刻。

## 三类缩放

| kind    | 规则          | 为什么 |
|---------|---------------|--------|
| `fact`  | 线性 × scale  | 每用户/每订单/每帖子的事实行。业务体量翻倍它就翻倍。 |
| `sub`   | × sqrt(scale) | 商品、活动、优惠券、广告素材。真实业务里它们随体量增长但远慢于用户数。 |
| `fixed` | 不变          | 类目树、渠道、埋点定义、用户分群、AB 实验。这些在真实公司里也就几十几百行，跟用户数无关。 |

行数几乎全落在 `fact` 上（当前 187,280 / 189,672 行），所以 `sub` 和 `fixed`
的表可以继续用旧的逐行生成器（那个量级又快又已验证），**只有 fact 表需要
genlib 向量化重写**。这是本次工作量的主要节省点。

## 时间窗刻意不随 scale 变

数据窗固定 2025-10-26 → 2026-01-24（91 天），放大只让每天更密，不拉长历史。
理由：拉长会改掉知识库里已声明的 `data_start`、mart 表行数、以及首尾残周残月的
具体形状，那些都是 eval 金标和文档依赖的事实。要拉长历史是另一个独立决策。
"""
from __future__ import annotations

FACT, SUB, FIXED = "fact", "sub", "fixed"

# 表名 → (当前实测行数, 缩放类别)
# 实测来源：eval/baseline/consistency.postgres.json（35 张原始表，不含 mart/派生层）
BASE: dict[str, tuple[int, str]] = {
    # ---------------- 用户域 ----------------
    "users":                (500,   FACT),
    "user_profiles":        (500,   FACT),
    "user_devices":         (744,   FACT),
    "user_segments":        (10,    FIXED),
    "user_segment_members": (1298,  FACT),
    # ---------------- 商品域 ----------------
    "categories":           (166,   FIXED),
    "products":             (200,   SUB),
    "product_tags":         (512,   SUB),
    # ---------------- 行为域 ----------------
    "event_definitions":    (25,    FIXED),
    "sessions":             (5000,  FACT),
    "events":               (20000, FACT),
    "page_views":           (30196, FACT),
    # ---------------- 社交域 ----------------
    "posts":                (1000,  FACT),
    "post_likes":           (35668, FACT),
    "post_comments":        (14000, FACT),
    "post_shares":          (7000,  FACT),
    "user_follows":         (19165, FACT),
    "user_messages":        (9253,  FACT),
    # ---------------- 交易域 ----------------
    "orders":               (2000,  FACT),
    "order_items":          (4225,  FACT),
    "payments":             (1746,  FACT),
    "subscriptions":        (50,    FACT),
    # ---------------- 归因域 ----------------
    "channels":             (14,    FIXED),
    "ad_campaigns":         (50,    SUB),
    "ad_creatives":         (144,   SUB),
    "channel_daily_costs":  (910,   FIXED),   # = 渠道数 × 天数，两者都固定
    "user_attributions":    (350,   FACT),
    # ---------------- 营销域 ----------------
    "campaigns":            (50,    SUB),
    "coupons":              (150,   SUB),
    "user_coupons":         (22735, FACT),
    "banners":              (119,   FIXED),
    "push_notifications":   (10000, FACT),
    # ---------------- 实验域 ----------------
    "ab_tests":             (12,    FIXED),
    "ab_test_variants":     (30,    FIXED),
    "ab_test_assignments":  (1850,  FACT),
}

# 数据时间窗（钉死，不读系统时间；见模块 docstring）
AS_OF = "2026-01-24"
DATA_START = "2025-10-26"
WINDOW_DAYS = 91

# 分片：每个 CSV shard 的目标行数。作用有两个——
#  1. 把生成峰值内存与表大小解耦（按块生成、写完即释放）
#  2. Redshift COPY 单个大文件是单线程的，必须多文件才能并行加载
SHARD_ROWS = 5_000_000

DEFAULT_TARGET_ROWS = 80_000_000


def table_rows(scale: float) -> dict[str, int]:
    """给定 scale，返回每张表的目标行数。"""
    out: dict[str, int] = {}
    for name, (base, kind) in BASE.items():
        if kind == FACT:
            n = base * scale
        elif kind == SUB:
            n = base * (scale ** 0.5)
        else:  # FIXED
            n = base
        out[name] = max(1, int(round(n)))
    return out


def total_rows(scale: float) -> int:
    return sum(table_rows(scale).values())


def solve_scale(target_rows: int) -> float:
    """反解出让总行数≈target_rows 的 scale。总行数对 scale 单调递增，二分即可。"""
    if target_rows <= total_rows(1.0):
        return 1.0
    lo, hi = 1.0, 2.0
    while total_rows(hi) < target_rows:
        hi *= 2
        if hi > 1e9:
            raise ValueError(f"target_rows={target_rows} 超出可解范围")
    for _ in range(200):
        mid = (lo + hi) / 2
        if total_rows(mid) < target_rows:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 3)


def fact_tables() -> list[str]:
    """需要 genlib 向量化的表。"""
    return [n for n, (_, k) in BASE.items() if k == FACT]


def small_tables() -> list[str]:
    """量级小、可继续用逐行生成器的表（sub + fixed）。"""
    return [n for n, (_, k) in BASE.items() if k != FACT]


def summary(scale: float) -> str:
    rows = table_rows(scale)
    facts = {n: rows[n] for n in fact_tables()}
    smalls = {n: rows[n] for n in small_tables()}
    lines = [
        f"scale = {scale}   总行数 = {sum(rows.values()):,}",
        f"  fact 表 {len(facts)} 张：{sum(facts.values()):,} 行（线性）",
        f"  sub/fixed 表 {len(smalls)} 张：{sum(smalls.values()):,} 行",
        "",
        f"{'表':<24}{'当前':>12}{'目标':>16}{'类别':>8}",
    ]
    for name, (base, kind) in sorted(BASE.items(), key=lambda kv: -table_rows(scale)[kv[0]]):
        lines.append(f"{name:<24}{base:>12,}{rows[name]:>16,}{kind:>8}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="规模预算查看器")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--scale", type=float, help="直接指定 scale")
    g.add_argument("--target-rows", type=int, default=DEFAULT_TARGET_ROWS,
                   help=f"给目标总行数，反解 scale（默认 {DEFAULT_TARGET_ROWS:,}）")
    a = ap.parse_args()
    s = a.scale if a.scale else solve_scale(a.target_rows)
    print(summary(s))
