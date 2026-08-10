"""向量化列填充器 —— 声明式 spec 里每种 kind 的实现。

## 确定性契约（最重要的一条）

每列的数据只由 `(seed, 表名, 列名, 块序号)` 决定，**与生成顺序、并行度无关**。
实现方式是 `rng_for()` 用 SeedSequence 按名字的 crc32 派生独立子流，而不是共用一个
可变 Generator。好处：
- 改某一列的 spec 不会扰动其他列的数据（共用 Generator 时会整条流错位）
- 分块生成、乃至将来并行分块，结果都一样
- 同 seed 重跑逐字节相同，mart 物化值才能当 oracle ground truth

## 业务信号在哪

放大规模最容易丢的就是分布形状：几千万行如果是均匀随机，所有分析题都答不出洞察。
带信号的填充器是 `ts_window`（周内波动 + 月度趋势 + 小时曲线）、`fk_skewed`
（少数用户贡献大头，帕累托）、`enum`（加权枚举）、`decimal_lognorm`（金额右偏长尾）。
"""
from __future__ import annotations

import zlib

import numpy as np

# ---------------------------------------------------------------- 确定性随机源

def _key(x) -> int:
    return zlib.crc32(str(x).encode("utf-8"))


def rng_for(seed: int, *keys) -> np.random.Generator:
    """由 (seed, keys...) 稳定派生一个独立 Generator。与调用顺序无关。"""
    return np.random.default_rng(np.random.SeedSequence([int(seed), *(_key(k) for k in keys)]))


# ---------------------------------------------------------------- 主键 / 外键

def pk(n: int, start: int = 1) -> np.ndarray:
    """连续 int64 主键区间。子表引用父表时只需要 (start, n)，不必持有父表数组。"""
    return np.arange(start, start + n, dtype=np.int64)


def fk_uniform(rng: np.random.Generator, n: int, parent_start: int, parent_n: int) -> np.ndarray:
    """均匀外键。"""
    return (parent_start + rng.integers(0, parent_n, size=n)).astype(np.int64)


# 集中度用**对数正态权重**，不用 Zipf(rank^-alpha)。
#
# 为什么不用 Zipf：alpha > 1 时级数收敛，排第一的父实体独占 1/ζ(alpha) 的子行
# （alpha=1.3 → 约 25%），top20% 冲到 96%、尾部空掉，复购率/分群/cohort 全部退化。
# 退到 alpha < 1 虽然 top20% 正常了，但头部仍然过重（N=5000 时 top-1 占 2.9%），
# 而且 top-1 份额随父实体数漂移，不是稳定的建模量。
#
# 对数正态更贴合真实的用户活跃度/商品热度：一样的长尾，头部不独占。
# 洛伦兹曲线有闭式解，top-p 份额 = Φ(Φ⁻¹(p) + σ)，与父实体数无关：
#     σ=1.15 → top20% ≈ 62%（电商真实量级，本默认值）
#     σ=0.80 → top20% ≈ 52%（偏平）
#     σ=1.60 → top20% ≈ 74%（偏陡）
#
# 注：genlib.rng.power_law_counts 用的正是 rank^-alpha 且默认 alpha=1.3，
# 照默认值调用会造出退化分布；本模块不复用它。
SIGMA_DEFAULT = 1.15


def _skew_weights(rng: np.random.Generator, parent_n: int, sigma: float) -> np.ndarray:
    """对数正态权重，已归一化。父实体的「受欢迎度」。"""
    w = rng.lognormal(mean=0.0, sigma=sigma, size=parent_n)
    return w / w.sum()


def fk_skewed(rng: np.random.Generator, n: int, parent_start: int, parent_n: int,
              sigma: float = SIGMA_DEFAULT) -> np.ndarray:
    """长尾外键：少数父实体拿到较多子行（用户活跃度、商品热度）。

    帕累托信号的来源。sigma 的取值含义见上方 SIGMA_DEFAULT 注释。
    """
    p = _skew_weights(rng, parent_n, sigma)
    idx = rng.choice(parent_n, size=n, replace=True, p=p)
    return (parent_start + idx).astype(np.int64)


def children_per_parent(rng: np.random.Generator, parent_n: int, total: int,
                        sigma: float = SIGMA_DEFAULT, min_count: int = 0) -> np.ndarray:
    """把 total 个子行按长尾分给 parent_n 个父行，返回每父的子数（和恰为 total）。

    配 `expand_ids` 用于订单行、帖子评论这类严格的父展子（每个子行必须属于一个父）。
    """
    p = _skew_weights(rng, parent_n, sigma)
    remaining = max(total - parent_n * min_count, 0)
    return rng.multinomial(remaining, p) + min_count


def expand_ids(parent_ids: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """按子计数展开父 id（np.repeat，O(N)）。counts.sum() 即结果长度。"""
    return np.repeat(parent_ids, counts)


def unique_pairs(rng: np.random.Generator, n: int, a_start: int, a_n: int,
                 b_start: int, b_n: int, sigma: float = SIGMA_DEFAULT
                 ) -> tuple[np.ndarray, np.ndarray]:
    """生成 n 对互不相同、且两端不相等的 (a, b)。用于关注关系这类无向/有向唯一边。

    做法：超量抽样 → 复合键去重 → 去自环 → 截断到 n。超采样系数按经验 1.6 起，
    不够就加倍重试，避免稀疏图上死循环。
    """
    need = n
    factor = 1.6
    for _ in range(8):
        m = int(need * factor)
        a = fk_skewed(rng, m, a_start, a_n, sigma)
        b = fk_uniform(rng, m, b_start, b_n)
        keep = a != b
        a, b = a[keep], b[keep]
        composite = a.astype(np.int64) * (b_n + 1) + (b - b_start)
        _, uidx = np.unique(composite, return_index=True)
        a, b = a[np.sort(uidx)], b[np.sort(uidx)]
        if len(a) >= need:
            return a[:need], b[:need]
        factor *= 2
    return a, b          # 图太稀疏，返回能拿到的最大唯一集


# ---------------------------------------------------------------- 枚举 / 标量

def enum(rng: np.random.Generator, n: int, values: list, weights: list | None = None
         ) -> np.ndarray:
    """加权枚举。weights 不必归一化；省略则均匀。"""
    vals = np.asarray(values, dtype=object)
    if weights is None:
        return vals[rng.integers(0, len(vals), size=n)]
    w = np.asarray(weights, dtype=np.float64)
    return vals[rng.choice(len(vals), size=n, replace=True, p=w / w.sum())]


def int_uniform(rng: np.random.Generator, n: int, lo: int, hi: int) -> np.ndarray:
    """[lo, hi] 闭区间整数。"""
    return rng.integers(lo, hi + 1, size=n).astype(np.int64)


def int_weighted(rng: np.random.Generator, n: int, values: list[int],
                 weights: list[float]) -> np.ndarray:
    w = np.asarray(weights, dtype=np.float64)
    vals = np.asarray(values, dtype=np.int64)
    return vals[rng.choice(len(vals), size=n, replace=True, p=w / w.sum())]


def bool_p(rng: np.random.Generator, n: int, p: float) -> np.ndarray:
    return rng.random(n) < p


def decimal_lognorm(rng: np.random.Generator, n: int, median: float, sigma: float = 0.7,
                    lo: float = 0.01, hi: float = 1e9, dp: int = 2) -> np.ndarray:
    """对数正态金额：右偏长尾，符合真实消费金额分布（均匀分布会让客单价分析变平）。

    median 是中位数（不是均值）；sigma 控制长尾厚度。
    """
    v = rng.lognormal(mean=np.log(median), sigma=sigma, size=n)
    return np.round(np.clip(v, lo, hi), dp)


# ---------------------------------------------------------------- 时间

_SEC_PER_DAY = 86_400

# 周内波动：周末活跃度高一些（索引 0=周一 … 6=周日）
DOW_WEIGHTS = np.array([0.92, 0.90, 0.94, 0.98, 1.12, 1.28, 1.22])
# 小时曲线：早高峰、午间、晚高峰三峰
HOUR_WEIGHTS = np.array([
    0.25, 0.15, 0.10, 0.08, 0.08, 0.15, 0.40, 0.85,
    1.10, 1.05, 1.00, 1.15, 1.35, 1.10, 0.95, 0.95,
    1.05, 1.20, 1.45, 1.70, 1.85, 1.60, 1.05, 0.55,
])


def ts_window(rng: np.random.Generator, n: int, start: np.datetime64, days: int,
              trend: float = 0.35, dow: np.ndarray | None = None,
              hour: np.ndarray | None = None) -> np.ndarray:
    """在 [start, start+days) 内生成带信号的时间戳（datetime64[s]）。

    三层信号叠加：
      trend —— 全窗线性增长幅度（0.35 = 末期日均比初期高 35%），让趋势题有东西可分析
      dow   —— 周内波动，让「周环比」「残周」有意义
      hour  —— 小时三峰，让按小时切片不是平的

    刻意**不**读系统时间：start 由调用方从 budget.DATA_START 传入。窗口末端是
    AS_OF，首尾都落在月中，天然形成知识库里写的「残周残月」。
    """
    dow = DOW_WEIGHTS if dow is None else dow
    hour = HOUR_WEIGHTS if hour is None else hour

    d = np.arange(days)
    start_dow = int((start.astype("datetime64[D]").astype(int) + 3) % 7)   # 1970-01-01 是周四
    w = (1.0 + trend * d / max(days - 1, 1)) * dow[(start_dow + d) % 7]
    day_idx = rng.choice(days, size=n, replace=True, p=w / w.sum())

    hr = rng.choice(24, size=n, replace=True, p=hour / hour.sum())
    sec_in_hour = rng.integers(0, 3600, size=n)

    offs = day_idx.astype(np.int64) * _SEC_PER_DAY + hr.astype(np.int64) * 3600 + sec_in_hour
    return (start.astype("datetime64[s]") + offs.astype("timedelta64[s]"))


def ts_offset(rng: np.random.Generator, base: np.ndarray, min_min: int, max_min: int,
              cap: np.datetime64 | None = None, null_p: float = 0.0) -> np.ndarray:
    """在 base 之后偏移 [min_min, max_min] 分钟。cap 之后的置空（静态样本不能穿越 AS_OF）。"""
    off = rng.integers(min_min * 60, max_min * 60 + 1, size=len(base))
    out = base.astype("datetime64[s]") + off.astype("timedelta64[s]")
    out = out.astype("datetime64[s]").astype(object)
    arr = np.array(out, dtype=object)
    if cap is not None:
        over = np.array([x is not None and np.datetime64(x, "s") > cap for x in arr])
        arr[over] = None
    if null_p > 0:
        arr[rng.random(len(arr)) < null_p] = None
    return arr


def date_of(ts: np.ndarray) -> np.ndarray:
    """时间戳 → 日期（datetime64[D]）。"""
    return ts.astype("datetime64[D]")


# ---------------------------------------------------------------- 文本

def from_pool(rng: np.random.Generator, n: int, pool: np.ndarray) -> np.ndarray:
    """从预生成池里取（整型索引，O(1)/行，替代 per-row Faker 调用）。"""
    return pool[rng.integers(0, len(pool), size=n)]


def serial_text(prefix: str, ids: np.ndarray, width: int = 12) -> np.ndarray:
    """形如 NO000000000123 的业务单号。向量化：np.char 而非逐行 f-string。"""
    body = np.char.zfill(ids.astype("U20"), width)
    return np.char.add(prefix, body)


def token_text(rng: np.random.Generator, n: int, prefix: str, width: int = 16) -> np.ndarray:
    """伪随机 token（push_token / transaction_id 这类）。"""
    v = rng.integers(0, 16 ** 8, size=n, dtype=np.int64)
    hexed = np.array([f"{x:08x}" for x in v], dtype=object)   # n 通常不大的列才用
    return np.char.add(prefix, hexed.astype("U32"))


def null_out(rng: np.random.Generator, arr: np.ndarray, p: float) -> np.ndarray:
    """按比例置空。整型列会被转成 object（CSV/Parquet 里表现为 NULL）。"""
    if p <= 0:
        return arr
    out = arr.astype(object)
    out[rng.random(len(out)) < p] = None
    return out


def const(n: int, v) -> np.ndarray:
    """常量列。

    不能用 `np.full(n, v, dtype=object)`：v 是 list/dict 时 numpy 会尝试把它当作
    要广播的数组，空 list 直接报 shape (0,) 无法广播到 (n,)。这里对容器类型改成
    逐位置赋**同一引用**（不是拷贝，所以 850 万行也只是一个指针数组）。
    """
    a = np.empty(n, dtype=object)
    if isinstance(v, (list, dict, tuple, set)):
        a[:] = [v] * n
    else:
        a.fill(v)
    return a


# ---------------------------------------------------------------- 类型兜底

def by_type(rng: np.random.Generator, n: int, ctype: str, colname: str = "") -> np.ndarray:
    """spec 没声明的列按类型给一个合理默认值。

    刻意保守：兜底列不产生业务信号，只保证类型合法、可 COPY。真正参与分析的列
    必须在 spec 里显式声明，否则就是「默默生成了没意义的数」——那比报错更糟。
    """
    if ctype == "INT":
        return int_uniform(rng, n, 1, 100)
    if ctype == "DECIMAL":
        return decimal_lognorm(rng, n, 50.0)
    if ctype == "BOOL":
        return bool_p(rng, n, 0.5)
    if ctype in ("TIMESTAMP", "DATE"):
        raise ValueError(f"时间列必须在 spec 里显式声明（列 {colname}），不能兜底")
    if ctype == "JSON":
        return const(n, {})
    if ctype == "ARRAY":
        return const(n, [])
    return from_pool(rng, n, np.array([f"{colname or 'v'}_{i}" for i in range(64)], dtype=object))
