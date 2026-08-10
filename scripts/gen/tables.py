"""21 张事实表的向量化构建函数。

## 约定

每个 `build_xxx(ctx, offset, n)` 返回 `dict[列名 -> ndarray]`，只负责 [offset, offset+n)
这一段行。分块的意义有两层：把峰值内存与表大小解耦，以及产出多个 Parquet 分片让
Redshift COPY 能并行加载（单文件 COPY 是单线程的）。

引用完整性靠「父表 id 是连续 int64 区间」这一条撑住：子表引用父表只需要 (start, n)，
不必在内存里持有父表数组。这也是 genlib 用 `id_range` 而非随机 id 的原因。

## 唯一对表的例外

`post_likes` / `user_follows` / `user_segment_members` / `ab_test_assignments` 的主键是
复合唯一对，没法分块生成（跨块会撞）。这些表在 ctx 里一次性预生成全局 int64 对数组
（15M 对 × 2 × 8B ≈ 240MB，内存可接受），builder 只做切片。

## 业务信号的落点

- `fk_skewed` → 用户活跃度、商品热度、帖子热度的长尾（帕累托题的素材）
- `ts_window` → 周内波动 + 月度趋势 + 小时三峰（趋势/异常/周环比题的素材）
- `orders.status` 权重 → paid+shipped+delivered 恰好 75%，复现 `dwd_orders_valid` 的行数比
- `user_attributions` 只覆盖约 70% 用户 → mart LEFT JOIN 后形成「约六成 GMV 未归因」
  这个知识库明确写了的治理发现。**这个缺口是刻意的，别"修好"它。**
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

import fillers as F

# ---------------------------------------------------------------- 文本池

CITIES = [
    ("北京", "北京", 12), ("上海", "上海", 12), ("广州", "广东", 8), ("深圳", "广东", 9),
    ("杭州", "浙江", 6), ("成都", "四川", 6), ("南京", "江苏", 5), ("武汉", "湖北", 5),
    ("西安", "陕西", 4), ("重庆", "重庆", 4), ("苏州", "江苏", 4), ("天津", "天津", 3),
    ("长沙", "湖南", 3), ("郑州", "河南", 3), ("青岛", "山东", 3), ("合肥", "安徽", 2),
    ("福州", "福建", 2), ("昆明", "云南", 2), ("沈阳", "辽宁", 2), ("哈尔滨", "黑龙江", 1),
]
PAGES = ["home", "category", "search", "product_detail", "cart", "checkout",
         "order_confirm", "my_orders", "profile", "feed", "post_detail", "coupon_center",
         "activity", "settings", "login"]
TRAFFIC = ["organic", "paid", "direct", "referral", "kol"]
OCCUPATIONS = ["学生", "工程师", "教师", "医生", "销售", "设计师", "运营", "财务",
               "公务员", "自由职业", "管理者", "其他"]
INTEREST_TAGS = ["数码", "美妆", "母婴", "运动", "美食", "旅行", "家居", "服饰",
                 "图书", "宠物", "汽车", "健康"]
BRANDS = ["Apple", "HUAWEI", "Xiaomi", "OPPO", "vivo", "Samsung", "honor", "OnePlus"]


def _pool(items, n=None):
    return np.array(items, dtype=object)


# ---------------------------------------------------------------- 上下文

@dataclass
class Ctx:
    """一次生成任务的全局参数与父表区间。"""
    seed: int
    rows: dict[str, int]                  # 表 → 目标行数（来自 budget.table_rows）
    start: np.datetime64                  # 数据窗起点
    days: int
    as_of_end: np.datetime64              # 窗末（含），静态样本的硬上限
    dim_ids: dict[str, np.ndarray]        # 维度表实际 id 列表（从现有 CSV 读）
    # 唯一对表的全局预生成结果
    pairs: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    # 派生依赖：orders 的状态/金额要给 payments、user_coupons 复用
    cache: dict = field(default_factory=dict)

    def n(self, table: str) -> int:
        return self.rows[table]

    def rng(self, *keys):
        return F.rng_for(self.seed, *keys)


# orders 状态权重：paid+shipped+delivered 约 75%，复现 dwd_orders_valid ≈ 75% 的行数比。
# 窗末订单会按剩余时间下调状态（见 _prep_orders），所以实际略高于 75%（约 75.8%）。
ORDER_STATUS = ["pending", "paid", "shipped", "delivered", "cancelled", "refunded"]
ORDER_STATUS_W = [10, 25, 22, 28, 9, 6]
VALID_STATUS = {"paid", "shipped", "delivered"}

# 事件名 → 漏斗权重。权重顺序保证 view_product > add_to_cart > begin_checkout。
# 权重 0 的三个事件不做泛化抽样，只从事实表反向保底生成（purchase 每有效单一条、
# use_coupon 每核销券一条、register 每用户一条）——否则「purchase 事件数 < 订单数」
# 这类方向性矛盾修不干净（审计 L4.8）。
# 注意：行预算天然压扁漏斗——85 万订单配 850 万事件意味着 purchase 占比约 7.5%，
# 真实 APP 是 0.5%~2%。这里只保证各级**单调递减**，不追真实转化率。
# 权重刻度：purchase 保底量 ≈ 泛化事件池的 8.7%（64 万有效单 / 736 万泛化事件），
# 所以链上每级的份额必须压着它往上排（begin_checkout ≈ 10.2% > 8.7%），
# 否则「漏斗单调」在保底量面前会翻车——这是算出来的，不是拍的。
EVENT_FUNNEL_W = {
    "view_home": 195, "view_product": 160, "add_to_cart": 130,
    "begin_checkout": 105, "app_open": 120, "app_close": 85,
    "view_post": 40, "login": 35, "search": 30, "view_category": 26,
    "logout": 22, "like_post": 20, "receive_push": 16, "add_favorite": 10,
    "click_banner": 8, "remove_from_cart": 7, "comment_post": 6,
    "view_profile": 5, "share": 4, "follow_user": 3, "click_push": 2,
    "edit_profile": 1,
    "register": 0, "purchase": 0, "use_coupon": 0,
}

# 用户活跃半衰期（天）与人群占比：会话落在注册后第 k 天的概率 ∝ exp(-k/h)。
# 混合出的留存曲线约 D1≈0.68、D7≈0.30、D14≈0.19——单调衰减，这是审计 L5.8
# 抓出的 P0（旧版 D14 比 D0 还高，留存类分析全部不可用）。
ENGAGEMENT_HALFLIFE = [1.0, 5.0, 20.0, 60.0]
ENGAGEMENT_W = [40, 30, 20, 10]


def prepare_globals(ctx: Ctx) -> None:
    """预生成跨块共享的状态。

    v2 之前只有 orders 一份全局切片（payments 复用它，所以支付金额从来是对的）。
    数据审计（docs/data-audit.md）证明了这个机制是对的、没走它的列全错了：
    凡是 builder 撇开 cache 自己另抽的声明列（item_count、like_count、event_count、
    user_level……），与明细必然对不上。所以 v2 把所有「一列的真值由另一张表决定」
    的东西全部提到这里：**先算事实，再把声明列从事实回填**，builder 只做切片。

    内存量级（80M 行规模）：orders 切片几十 MB；events/page_views 的归属与时间数组
    约 (8.5M + 12.9M) × 24B ≈ 530MB。生成机内存按 8GB 起步。
    """
    prepare_pairs(ctx)
    _prep_users(ctx)
    _prep_orders(ctx)
    _prep_order_items(ctx)
    _prep_payments(ctx)
    _prep_user_coupons(ctx)
    _prep_behavior(ctx)
    _prep_social(ctx)
    _prep_user_level(ctx)

    # user_attributions：刻意只覆盖部分用户。mart LEFT JOIN 后形成知识库写明的
    # 「约六成 GMV 落在未归因」——这个缺口是治理发现的素材，不是缺陷。
    nu = ctx.n("users")
    ar = ctx.rng("user_attributions", "user_pick")
    natt = min(ctx.n("user_attributions"), nu)
    ctx.cache["attributed_users"] = np.sort(ar.choice(np.arange(1, nu + 1), size=natt,
                                                      replace=False))
    ctx.rows["user_attributions"] = natt


def _prep_users(ctx: Ctx) -> None:
    """注册时间 / VIP 全局化。orders、sessions 都要满足「先注册后行为」，
    user_level 要读 VIP 和注册期，所以这几列不能分块抽。"""
    nu = ctx.n("users")
    reg = F.ts_window(ctx.rng("users", "registered_at", "global"), nu,
                      ctx.start, ctx.days, trend=0.55)
    ctx.cache["users_registered_at"] = reg
    ctx.cache["users_reg_day"] = (
        reg.astype("datetime64[D]") - ctx.start.astype("datetime64[D]")
    ).astype(np.int64)
    ctx.cache["users_is_vip"] = F.bool_p(ctx.rng("users", "is_vip", "global"), nu, 0.12)


def _pick_registered_user(ctx: Ctx, rng, event_day: np.ndarray) -> np.ndarray:
    """按「行为日不得早于注册日」采样 user_id，保留长尾偏斜。

    做法：用户按注册日排序，行为日 d 的候选集是排序数组的前缀；在前缀的偏斜权重
    累积和上做逆变换采样，全程向量化。旧版 fk_skewed 无视注册日，会造出
    「注册前就下单」的行（审计前尚未查这条，但代码上必错）。
    """
    if "_users_by_reg" not in ctx.cache:
        regday = ctx.cache["users_reg_day"]
        order = np.argsort(regday, kind="stable")
        w = F._skew_weights(ctx.rng("users", "activity_weight"), len(regday), 1.15)
        ctx.cache["_users_by_reg"] = (
            (order + 1).astype(np.int64), regday[order], np.cumsum(w[order]))
    uid_sorted, regday_sorted, cw = ctx.cache["_users_by_reg"]
    elig = np.searchsorted(regday_sorted, event_day, side="right")
    elig = np.maximum(elig, 1)          # 第 0 天也有注册用户（trend 抽样量级保证）
    target = rng.random(len(event_day)) * cw[elig - 1]
    idx = np.minimum(np.searchsorted(cw, target, side="right"), elig - 1)
    return uid_sorted[idx]


def _prep_orders(ctx: Ctx) -> None:
    no = ctx.n("orders")
    placed = F.ts_window(ctx.rng("orders", "placed_at", "global"), no, ctx.start,
                         ctx.days, trend=0.42)
    placed_s = placed.astype("datetime64[s]")
    remain = (ctx.as_of_end - placed_s).astype("timedelta64[s]").astype(np.int64)

    status = F.enum(ctx.rng("orders", "status", "global"), no, ORDER_STATUS,
                    ORDER_STATUS_W)
    # 状态按剩余窗口下调：窗末订单来不及走完生命周期。阈值与 _cond_ts 的 hi_min
    # 一致（refunded 14 天、delivered 7 天、shipped 2 天、cancelled 1 天、paid 4 小时）。
    # 不下调的话 status 与时间戳必然打架——审计 L4.6 抓到 5052 行 status=refunded
    # 而 refunded_at 为空，就是旧版「超窗置空但状态不回退」造成的。
    day = 86400
    status = np.where((status == "refunded") & (remain < 14 * day), "paid", status)
    status = np.where((status == "delivered") & (remain < 7 * day), "shipped", status)
    status = np.where((status == "shipped") & (remain < 2 * day), "paid", status)
    status = np.where((status == "cancelled") & (remain < 1 * day), "pending", status)
    status = np.where((status == "paid") & (remain < 4 * 3600), "pending", status)

    total = F.decimal_lognorm(ctx.rng("orders", "total_amount", "global"), no, 168.0,
                              sigma=0.85, lo=9.9, hi=99999)
    disc = np.round(total * F.enum(ctx.rng("orders", "disc_rate", "global"), no,
                                   [0.0, 0.05, 0.10, 0.20], [55, 20, 15, 10]).astype(float), 2)
    ship = F.enum(ctx.rng("orders", "shipping_fee", "global"), no,
                  [0.0, 6.0, 12.0], [62, 26, 12]).astype(float)
    # user_id 全局算，且必须尊重注册日：payments 要与所属订单同一用户，
    # 「注册前下单」是真实库不可能出现的行。
    placed_day = (placed.astype("datetime64[D]")
                  - ctx.start.astype("datetime64[D]")).astype(np.int64)
    uid = _pick_registered_user(ctx, ctx.rng("orders", "user_id", "global"), placed_day)
    ctx.cache["orders_user_id"] = uid
    # 注册当天的单可能抽在注册时刻之前的小时——同日内后移到注册后 60 秒
    # （不改日期，日级 GMV 不受影响），上限压在窗内防止注册在窗末最后一分钟的用户越窗
    reg_s = ctx.cache["users_registered_at"][uid - 1].astype("datetime64[s]")
    win_last = (ctx.start.astype("datetime64[s]")
                + np.timedelta64(ctx.days * 86400 - 1, "s"))
    placed = np.maximum(placed.astype("datetime64[s]"),
                        np.minimum(reg_s + np.timedelta64(60, "s"), win_last))
    ctx.cache["orders"] = {
        "status": status,
        "placed_at": placed,
        "total_amount": total,
        "discount_amount": disc,
        "shipping_fee": ship,
        "actual_amount": np.round(total - disc + ship, 2),
        "is_valid": np.isin(status, list(VALID_STATUS)),
    }

    # 每单件数：长尾、至少 1 行。orders.item_count 必须从这里读——
    # 旧版 builder 另抽了一份 int_weighted，70% 的单与明细行数不符（审计 L4.2）。
    cnt = F.children_per_parent(ctx.rng("order_items", "counts"), no,
                                ctx.n("order_items"), min_count=1)
    ctx.cache["orders_item_count"] = cnt
    ctx.cache["order_items_order_id"] = F.expand_ids(F.pk(no, 1), cnt)

    # 用券订单全局定下来：user_coupons 的核销行要反向对齐到 (user, coupon, order)
    has_cp = ctx.rng("orders", "coupon_id", "global").random(no) < 0.38
    cp_pick = F.from_pool(ctx.rng("orders", "cid", "global"), no,
                          ctx.dim_ids["coupons"])
    coupon = cp_pick.astype(object)
    coupon[~has_cp] = None
    ctx.cache["orders_coupon_id"] = coupon
    ctx.cache["orders_coupon_rows"] = np.flatnonzero(has_cp)      # 0-based


def _prep_order_items(ctx: Ctx) -> None:
    """明细金额全局生成并按单缩放：sum(items.actual_amount) 精确等于订单头 total_amount。

    审计 L4.1：85.4 万单里 99.99% 头合计对不上明细（差值双向随机），根因是两侧各自
    独立抽样。对齐方向选「明细缩放到头」而不是「头改成明细和」：mart 口径、eval 金标、
    知识卡片引用的全是头口径，反向会把 GMV 抬 41%、作废所有已冻结数字。

    金额一律走整数分（int64 cents）：浮点 round 在 .005 边界不稳定，且逐行 round 后
    按单求和有 ±0.005×件数 的漂移，审计阈值 0.01 会抓出来。每单末行吸收舍入差，
    行内公式 actual = unit×qty − discount 通过 ceil 分定价精确成立。
    """
    no, ni = ctx.n("orders"), ctx.n("order_items")
    cnt = ctx.cache["orders_item_count"]
    starts = np.concatenate(([0], np.cumsum(cnt)[:-1]))
    ends = np.cumsum(cnt) - 1

    qty = F.int_weighted(ctx.rng("order_items", "quantity", "global"), ni,
                         [1, 2, 3, 5], [68, 20, 8, 4])
    unit = F.decimal_lognorm(ctx.rng("order_items", "unit_price", "global"), ni,
                             79.0, sigma=0.8, lo=4.9, hi=9999)
    rate = F.enum(ctx.rng("order_items", "disc", "global"), ni,
                  [0.0, 0.05, 0.15], [70, 20, 10]).astype(float)

    raw = unit * qty * (1.0 - rate)                     # 未缩放的行净额
    scale = np.repeat(ctx.cache["orders"]["total_amount"]
                      / np.add.reduceat(raw, starts), cnt)

    head_c = np.round(ctx.cache["orders"]["total_amount"] * 100).astype(np.int64)
    act_c = np.round(raw * scale * 100).astype(np.int64)
    act_c[ends] += head_c - np.add.reduceat(act_c, starts)
    # 末行吸收的舍入差 < 件数×0.5 分，而缩放后行净额至少若干分，不该出现非正金额；
    # 万一行预算/件数分布改到极端值，这里要炸出来而不是灌进库
    assert (act_c > 0).all(), "明细缩放出现非正金额，检查件数分布与订单头下限"

    disc_c = np.clip(np.round(unit * qty * rate * scale * 100), 0, None).astype(np.int64)
    unit_c = -(-(act_c + disc_c) // qty)                # ceil 到分，保证行折扣非负
    ctx.cache["items"] = {
        "quantity": qty,
        "unit_price": unit_c / 100.0,
        "discount_amount": (unit_c * qty - act_c) / 100.0,   # 公式精确成立
        "actual_amount": act_c / 100.0,
        "created_at": ctx.cache["orders"]["placed_at"][
            F.expand_ids(np.arange(no), cnt)],
    }


def _prep_payments(ctx: Ctx) -> None:
    """支付行 = 每个付过钱的订单恰好一条：valid → success，refunded → refunded。

    旧版从可付款订单里无放回抽 min(预算, 可付款数) 个、再叠 10% failed/pending，
    结果 64,214 个有效单查无 success 支付（审计 L4.6，占有效单 10%）。行预算本来
    就是按 (75%+6%)×订单数 定的，直接一一对应，行数差异用 ctx.rows 回写。
    """
    st = ctx.cache["orders"]["status"]
    payable = np.flatnonzero(np.isin(st, list(VALID_STATUS | {"refunded"})))
    ctx.cache["payments_order_idx"] = payable           # 0-based
    ctx.rows["payments"] = len(payable)


def _prep_user_coupons(ctx: Ctx) -> None:
    """核销闭环：orders 里带 coupon_id 的每一单，反向生成一行 status='used' 的券，
    (user_id, coupon_id, order_id) 三元对齐、used_at = 下单时刻；其余行只有
    unused / expired，且 expired 由 expire_at 是否已过窗末决定，不再随机抽。

    旧版三个数字互相打架（审计 L4.7）：orders 说 32.4 万单用券、user_coupons 说
    485 万张已用、还挂到了 85 万个不同订单上（平均 5.7 张/单）。
    """
    n_used = len(ctx.cache["orders_coupon_rows"])
    if n_used > ctx.n("user_coupons"):
        raise ValueError(f"用券订单 {n_used} 超过 user_coupons 行预算 "
                         f"{ctx.n('user_coupons')}，先调 budget")
    ctx.cache["uc_n_used"] = n_used


def _prep_behavior(ctx: Ctx) -> None:
    """行为域全局化：会话挂用户生命周期，事件/页面浏览挂会话。

    修的是两个 P0（审计 L5.6/L5.8）：旧版 events 的 user_id、event_time、event_name
    三者各自独立抽——事件名等权（25 类 max/min=1.006，漏斗恒 100% 转化）、活跃与
    注册无关（留存曲线不衰减）、事件所属 session 的 user 与事件的 user 互相矛盾。

    结构：每用户至少 1 个会话（保底事件要挂到本人会话上）→ 会话落在注册后第 k 天，
    k ~ 按活跃半衰期截断指数 → 事件继承会话的 user 与时间窗，事件名按漏斗权重 →
    sessions 的 event_count / page_view_count / is_bounce 从事实回填。
    """
    nu, ns = ctx.n("users"), ctx.n("sessions")
    ne, npv = ctx.n("events"), ctx.n("page_views")
    reg_day = ctx.cache["users_reg_day"]
    win_end = (ctx.start.astype("datetime64[s]")
               + np.timedelta64(ctx.days * 86400, "s"))

    # --- 会话 ---
    per_user = F.children_per_parent(ctx.rng("sessions", "counts"), nu, ns, min_count=1)
    sess_user = F.expand_ids(np.arange(1, nu + 1, dtype=np.int64), per_user)
    first_sess = np.zeros(nu + 1, np.int64)
    first_sess[1:] = np.concatenate(([0], np.cumsum(per_user)[:-1])) + 1

    r = ctx.rng("sessions", "lifecycle")
    h = np.repeat(F.enum(ctx.rng("users", "halflife"), nu,
                         ENGAGEMENT_HALFLIFE, ENGAGEMENT_W).astype(float), per_user)
    horizon = np.repeat((ctx.days - 1 - reg_day).astype(float), per_user)
    u = r.random(ns)
    k = np.floor(-h * np.log1p(-u * (1.0 - np.exp(-(horizon + 1.0) / h))))
    day = np.repeat(reg_day, per_user) + np.clip(k, 0, horizon).astype(np.int64)
    hr = r.choice(24, size=ns, p=F.HOUR_WEIGHTS / F.HOUR_WEIGHTS.sum())
    start_ts = (ctx.start.astype("datetime64[s]")
                + (day * 86400 + hr * 3600 + r.integers(0, 3600, ns))
                .astype("timedelta64[s]"))
    # 注册当天的会话（k=0）小时可能抽在注册时刻之前——按天对齐挡不住小时倒挂，
    # 钳到注册后 60 秒（selftest_closures 抓过这个）
    start_ts = np.maximum(
        start_ts, np.repeat(ctx.cache["users_registered_at"], per_user)
        .astype("datetime64[s]") + np.timedelta64(60, "s"))
    dur = F.int_weighted(ctx.rng("sessions", "duration_seconds", "global"), ns,
                         [12, 45, 120, 300, 900, 2400], [18, 22, 24, 20, 12, 4])
    dur = np.minimum(dur, np.maximum(
        (win_end - start_ts).astype("timedelta64[s]").astype(np.int64) - 1, 12))

    # --- 事件：泛化段（漏斗权重）+ 保底段（从事实表反向） ---
    names = ctx.dim_ids["event_definitions"]
    w = np.array([EVENT_FUNNEL_W.get(str(x), 5) for x in names], dtype=float)

    og = ctx.cache["orders"]
    valid_idx = np.flatnonzero(og["is_valid"])
    cp_rows = ctx.cache["orders_coupon_rows"]
    n_reserved = nu + len(valid_idx) + len(cp_rows)
    n_generic = ne - n_reserved
    assert n_generic >= ns, (f"事件预算 {ne} 扣掉保底 {n_reserved} 后不足以给 "
                             f"{ns} 个会话每个至少一条")

    er = ctx.rng("events", "global")
    per_sess = F.children_per_parent(ctx.rng("events", "counts"), ns, n_generic,
                                     min_count=1)
    g_sess = F.expand_ids(np.arange(1, ns + 1, dtype=np.int64), per_sess)
    g_time = (start_ts[g_sess - 1]
              + (er.random(n_generic) * np.maximum(dur[g_sess - 1], 1))
              .astype("timedelta64[s]"))
    g_name = er.choice(len(names), size=n_generic, p=w / w.sum()).astype(np.int16)

    def _idx_of(name: str) -> int:
        pos = np.flatnonzero(names == name)
        assert len(pos) == 1, f"event_definitions 里找不到 {name}"
        return int(pos[0])

    reg_ts = ctx.cache["users_registered_at"].astype("datetime64[s]")
    rr = ctx.rng("events", "reserved")
    # register：每用户一条，挂本人首会话，时刻 = 注册时刻
    res_uid = [np.arange(1, nu + 1, dtype=np.int64)]
    res_sess = [first_sess[1:]]
    res_time = [reg_ts]
    res_name = [np.full(nu, _idx_of("register"), np.int16)]
    # purchase：每有效单一条，挂下单人首会话，时刻 ≈ 支付窗口内
    o_uid = ctx.cache["orders_user_id"][valid_idx]
    res_uid.append(o_uid)
    res_sess.append(first_sess[o_uid])
    res_time.append(og["placed_at"][valid_idx].astype("datetime64[s]")
                    + rr.integers(60, 4 * 3600, len(valid_idx))
                    .astype("timedelta64[s]"))
    res_name.append(np.full(len(valid_idx), _idx_of("purchase"), np.int16))
    # use_coupon：每核销券一条（= 每个用券订单），时刻 = 下单时刻
    c_uid = ctx.cache["orders_user_id"][cp_rows]
    res_uid.append(c_uid)
    res_sess.append(first_sess[c_uid])
    res_time.append(og["placed_at"][cp_rows].astype("datetime64[s]"))
    res_name.append(np.full(len(cp_rows), _idx_of("use_coupon"), np.int16))

    ev_sess = np.concatenate([g_sess] + res_sess)
    ev_time = np.concatenate([g_time.astype("datetime64[s]")]
                             + [t.astype("datetime64[s]") for t in res_time])
    ev_time = np.minimum(ev_time, win_end - np.timedelta64(1, "s"))
    ctx.cache["events"] = {
        "session_id": ev_sess,
        "user_id": np.concatenate([sess_user[g_sess - 1]] + res_uid),
        "event_time": ev_time,
        "name_idx": np.concatenate([g_name] + res_name),
    }

    # --- 页面浏览：同样挂会话 ---
    pr = ctx.rng("page_views", "global")
    per_sess_pv = F.children_per_parent(ctx.rng("page_views", "counts"), ns, npv,
                                        min_count=1)
    pv_sess = F.expand_ids(np.arange(1, ns + 1, dtype=np.int64), per_sess_pv)
    pv_time = np.minimum(
        (start_ts[pv_sess - 1]
         + (pr.random(npv) * np.maximum(dur[pv_sess - 1], 1))
         .astype("timedelta64[s]")).astype("datetime64[s]"),
        win_end - np.timedelta64(1, "s"))
    ctx.cache["page_views"] = {"session_id": pv_sess,
                               "user_id": sess_user[pv_sess - 1],
                               "view_time": pv_time}

    # --- 会话计数从事实回填（旧版 event_count 拍脑袋，虚高 5.75 倍，审计 L4.4）---
    ev_cnt = np.bincount(ev_sess, minlength=ns + 1)[1:]
    pv_cnt = np.bincount(pv_sess, minlength=ns + 1)[1:]
    ctx.cache["sessions"] = {
        "user_id": sess_user, "start_time": start_ts,
        "duration_seconds": dur, "event_count": ev_cnt,
        "page_view_count": pv_cnt, "is_bounce": pv_cnt <= 1,
    }


def _prep_social(ctx: Ctx) -> None:
    """帖子三计数器从明细回填（旧版 like_count = views×0.06，虚高 3.3 倍，审计 L4.3）。

    likes 的对表本来就是全局生成的（prepare_pairs），bincount 一下就是真值；
    comments / shares 的 post_id 原先分块抽，这里挪到全局，builder 只切片。
    view_count 反过来从 like 数上推（保证 view ≥ like，点赞率 4.5%~8%）。
    """
    npost = ctx.n("posts")
    _, like_post = ctx.pairs["post_likes"]
    cmt_post = F.fk_skewed(ctx.rng("post_comments", "post_id", "global"),
                           ctx.n("post_comments"), 1, npost)
    shr_post = F.fk_skewed(ctx.rng("post_shares", "post_id", "global"),
                           ctx.n("post_shares"), 1, npost)
    likes = np.bincount(like_post, minlength=npost + 1)[1:]
    r = ctx.rng("posts", "views")
    ctx.cache["post_comments_post_id"] = cmt_post
    ctx.cache["post_shares_post_id"] = shr_post
    ctx.cache["posts_counters"] = {
        "like_count": likes,
        "comment_count": np.bincount(cmt_post, minlength=npost + 1)[1:],
        "share_count": np.bincount(shr_post, minlength=npost + 1)[1:],
        "view_count": (likes * r.uniform(12, 22, npost)
                       + r.integers(30, 800, npost)).astype(np.int64),
    }


def _prep_user_level(ctx: Ctx) -> None:
    """user_level 按 knowledge/domains/user/users.md 的规则从事实推导，
    让卡片写的规则在数据里真实成立（审计 L4.5：旧版独立抽样，五档「消费≥1000
    占比」全是 21% 上下，agent 按等级筛高价值用户会拿到与消费无关的人群）。

    优先级：5（累计消费≥10000 或 VIP）> 4（≥1000）> 3（近 30 天活跃≥10 天）
    > 1（注册<30 天）> 2（默认）。last_active_at 同步从事件真值取 max。
    """
    nu = ctx.n("users")
    og = ctx.cache["orders"]
    valid = og["is_valid"]
    spend = np.bincount(ctx.cache["orders_user_id"][valid],
                        weights=og["actual_amount"][valid], minlength=nu + 1)[1:]

    ev = ctx.cache["events"]
    ev_day = (ev["event_time"].astype("datetime64[D]")
              - ctx.start.astype("datetime64[D]")).astype(np.int64)
    recent = ev_day >= ctx.days - 30
    key = ev["user_id"][recent] * 32 + (ev_day[recent] - (ctx.days - 30))
    act30 = np.bincount(np.unique(key) // 32, minlength=nu + 1)[1:]

    is_vip = ctx.cache["users_is_vip"]
    reg_day = ctx.cache["users_reg_day"]
    ctx.cache["users_level"] = np.select(
        [(spend >= 10000) | is_vip, spend >= 1000, act30 >= 10,
         reg_day >= ctx.days - 30],
        [5, 4, 3, 1], default=2).astype(np.int64)

    sec = (ev["event_time"]
           - ctx.start.astype("datetime64[s]")).astype("timedelta64[s]").astype(np.int64)
    la = np.zeros(nu + 1, np.int64)
    np.maximum.at(la, ev["user_id"], sec)
    ctx.cache["users_last_active"] = (ctx.start.astype("datetime64[s]")
                                      + la[1:].astype("timedelta64[s]"))


def prepare_pairs(ctx: Ctx) -> None:
    """一次性生成所有唯一对表的复合主键（无法分块，见模块 docstring）。"""
    nu, npost = ctx.n("users"), ctx.n("posts")

    a, b = F.unique_pairs(ctx.rng("post_likes", "pk"), ctx.n("post_likes"),
                          1, nu, 1, npost)
    ctx.pairs["post_likes"] = (a, b)

    a, b = F.unique_pairs(ctx.rng("user_follows", "pk"), ctx.n("user_follows"),
                          1, nu, 1, nu)
    ctx.pairs["user_follows"] = (a, b)

    segs = ctx.dim_ids["user_segments"]
    a, b = F.unique_pairs(ctx.rng("user_segment_members", "pk"),
                          ctx.n("user_segment_members"), 1, nu, 1, len(segs))
    ctx.pairs["user_segment_members"] = (a, segs[b - 1])

    tests = ctx.dim_ids["ab_tests"]
    a, b = F.unique_pairs(ctx.rng("ab_test_assignments", "pk"),
                          ctx.n("ab_test_assignments"), 1, nu, 1, len(tests))
    ctx.pairs["ab_test_assignments"] = (a, tests[b - 1])


# ---------------------------------------------------------------- 用户域

def build_users(ctx: Ctx, off: int, n: int) -> dict:
    # 注册时间 / VIP / 等级 / 最后活跃全部来自全局 cache：等级由消费+活跃+注册期
    # 推导（见 _prep_user_level），最后活跃 = 该用户事件时刻的真实 max。
    sl = slice(off, off + n)
    uid = F.pk(n, off + 1)
    uname = np.char.add("user_", uid.astype("U12"))
    reg = ctx.cache["users_registered_at"][sl]
    return {
        "user_id": uid,
        "username": uname,
        "email": np.char.add(uname, "@example.com"),
        "phone": np.char.add("1", np.char.zfill((3_0000_0000 + uid % 6_0000_0000).astype("U12"), 10)),
        "registered_at": reg,
        "registration_source": F.enum(ctx.rng("users", "registration_source", off), n,
                                      ["app", "web", "mini_program"], [55, 30, 15]),
        "status": F.enum(ctx.rng("users", "status", off), n,
                         ["active", "inactive", "banned"], [85, 12, 3]),
        "user_level": ctx.cache["users_level"][sl],
        "is_vip": ctx.cache["users_is_vip"][sl],
        "last_active_at": ctx.cache["users_last_active"][sl],
        "created_at": reg,
        "updated_at": reg,
    }


def build_user_profiles(ctx: Ctx, off: int, n: int) -> dict:
    uid = F.pk(n, off + 1)
    ci = ctx.rng("user_profiles", "city", off).choice(
        len(CITIES), size=n, replace=True,
        p=np.array([c[2] for c in CITIES], float) / sum(c[2] for c in CITIES))
    cities = np.array([c[0] for c in CITIES], dtype=object)[ci]
    provs = np.array([c[1] for c in CITIES], dtype=object)[ci]
    age = F.int_uniform(ctx.rng("user_profiles", "age", off), n, 18, 60)
    # 生日由年龄反推，锚在 AS_OF 而不是系统时间
    birth = (ctx.as_of_end.astype("datetime64[D]")
             - (age.astype("timedelta64[D]") * 365))
    ir = ctx.rng("user_profiles", "interests", off)
    tags = np.array(INTEREST_TAGS, dtype=object)
    k = F.int_weighted(ir, n, [1, 2, 3, 4], [30, 35, 25, 10])
    interests = np.array([list(tags[ir.choice(len(tags), size=kk, replace=False)])
                          for kk in k], dtype=object)
    return {
        "user_id": uid,
        "age": age,
        "gender": F.enum(ctx.rng("user_profiles", "gender", off), n,
                         ["male", "female", "unknown"], [48, 47, 5]),
        "birth_date": birth,
        "city": cities,
        "province": provs,
        "country": F.const(n, "China"),
        "interests": interests,
        "occupation": F.from_pool(ctx.rng("user_profiles", "occupation", off), n,
                                  _pool(OCCUPATIONS)),
        "income_level": F.enum(ctx.rng("user_profiles", "income_level", off), n,
                               ["low", "medium", "high"], [30, 50, 20]),
        "created_at": F.ts_window(ctx.rng("user_profiles", "created_at", off), n,
                                  ctx.start, ctx.days),
        "updated_at": F.ts_window(ctx.rng("user_profiles", "updated_at", off), n,
                                  ctx.start, ctx.days),
    }


def build_user_devices(ctx: Ctx, off: int, n: int) -> dict:
    did = np.char.add("dev_", F.pk(n, off + 1).astype("U14"))
    uid = F.fk_skewed(ctx.rng("user_devices", "user_id", off), n, 1, ctx.n("users"))
    first = F.ts_window(ctx.rng("user_devices", "first_seen_at", off), n, ctx.start, ctx.days)
    return {
        "device_id": did,
        "user_id": uid,
        "device_type": F.enum(ctx.rng("user_devices", "device_type", off), n,
                              ["ios", "android", "web"], [35, 55, 10]),
        "os_version": F.enum(ctx.rng("user_devices", "os_version", off), n,
                             ["15.0", "16.2", "17.1", "13", "14"], [15, 25, 30, 15, 15]),
        "device_model": F.from_pool(ctx.rng("user_devices", "device_model", off), n,
                                    _pool([f"model-{i}" for i in range(40)])),
        "device_brand": F.from_pool(ctx.rng("user_devices", "device_brand", off), n,
                                    _pool(BRANDS)),
        "app_version": F.enum(ctx.rng("user_devices", "app_version", off), n,
                              ["5.1.0", "5.2.0", "5.3.1", "6.0.0"], [10, 20, 40, 30]),
        "push_token": np.char.add("tok_", F.pk(n, off + 1).astype("U14")),
        "is_primary": F.bool_p(ctx.rng("user_devices", "is_primary", off), n, 0.62),
        "first_seen_at": first,
        "last_seen_at": F.ts_offset(ctx.rng("user_devices", "last_seen_at", off), first,
                                    60, 80 * 1440, cap=ctx.as_of_end),
        "created_at": first,
    }


def build_user_segment_members(ctx: Ctx, off: int, n: int) -> dict:
    a, b = ctx.pairs["user_segment_members"]
    ent = F.ts_window(ctx.rng("user_segment_members", "entered_at", off), n,
                      ctx.start, ctx.days)
    return {
        "user_id": a[off:off + n],
        "segment_id": b[off:off + n],
        "entered_at": ent,
        "exited_at": F.ts_offset(ctx.rng("user_segment_members", "exited_at", off), ent,
                                 1440, 60 * 1440, cap=ctx.as_of_end, null_p=0.78),
    }


# ---------------------------------------------------------------- 行为域

def build_sessions(ctx: Ctx, off: int, n: int) -> dict:
    # user/start/dur 来自生命周期模型，event_count/page_view_count/is_bounce
    # 从事实回填（旧版 event_count 拍脑袋，与 events 明细 93% 不符，审计 L4.4）。
    sl = slice(off, off + n)
    g = ctx.cache["sessions"]
    sid = F.pk(n, off + 1)
    uid = g["user_id"][sl]
    st = g["start_time"][sl]
    dur = g["duration_seconds"][sl]
    pv = g["page_view_count"][sl]
    end = st.astype("datetime64[s]") + dur.astype("timedelta64[s]")
    return {
        "session_id": sid,
        "user_id": uid,
        "device_id": np.char.add("dev_", uid.astype("U14")),
        "start_time": st,
        "end_time": end,
        "duration_seconds": dur,
        "event_count": g["event_count"][sl],
        "page_view_count": pv,
        "is_bounce": g["is_bounce"][sl],
        "entry_page": F.from_pool(ctx.rng("sessions", "entry_page", off), n, _pool(PAGES)),
        "exit_page": F.from_pool(ctx.rng("sessions", "exit_page", off), n, _pool(PAGES)),
        "traffic_source": F.enum(ctx.rng("sessions", "traffic_source", off), n,
                                 TRAFFIC, [40, 25, 20, 10, 5]),
        "utm_source": F.enum(ctx.rng("sessions", "utm_source", off), n,
                             ["douyin", "weixin", "xiaohongshu", "baidu", "none"],
                             [22, 25, 18, 15, 20]),
        "utm_medium": F.enum(ctx.rng("sessions", "utm_medium", off), n,
                             ["cpc", "social", "organic", "email"], [30, 30, 30, 10]),
        "utm_campaign": F.from_pool(ctx.rng("sessions", "utm_campaign", off), n,
                                    _pool([f"camp_{i}" for i in range(50)])),
        "created_at": st,
    }


def build_events(ctx: Ctx, off: int, n: int) -> dict:
    # user/session/时间/事件名全部来自 _prep_behavior：事件继承所属会话的用户与
    # 时间窗，事件名按漏斗权重（旧版三者独立抽——漏斗恒 100%、留存不衰减、
    # 事件的 user 与所属 session 的 user 互相矛盾，审计 L5.6/L5.8）。
    sl = slice(off, off + n)
    g = ctx.cache["events"]
    names = ctx.dim_ids["event_definitions"]
    uid = g["user_id"][sl]
    ev_time = g["event_time"][sl]
    return {
        "event_id": F.pk(n, off + 1),
        "user_id": uid,
        "device_id": np.char.add("dev_", uid.astype("U14")),
        "session_id": g["session_id"][sl],
        "event_name": names[g["name_idx"][sl]],
        "event_time": ev_time,
        "properties": F.const(n, {}),
        "page_name": F.from_pool(ctx.rng("events", "page_name", off), n, _pool(PAGES)),
        "referrer": F.enum(ctx.rng("events", "referrer", off), n,
                           ["", "https://m.example.com", "https://search.example.com"],
                           [60, 25, 15]),
        "ip_address": np.char.add("10.", np.char.add(
            (F.int_uniform(ctx.rng("events", "ip_a", off), n, 0, 255)).astype("U4"),
            np.char.add(".", np.char.add(
                (F.int_uniform(ctx.rng("events", "ip_b", off), n, 0, 255)).astype("U4"),
                np.char.add(".", (F.int_uniform(ctx.rng("events", "ip_c", off), n, 1, 254)
                                  ).astype("U4")))))),
        "created_at": ev_time,
    }


def build_page_views(ctx: Ctx, off: int, n: int) -> dict:
    # user/session/时间与 sessions 一致（来自 _prep_behavior），页面浏览数
    # 由 sessions.page_view_count 反向可对账。
    sl = slice(off, off + n)
    g = ctx.cache["page_views"]
    vt = g["view_time"][sl]
    pages = F.from_pool(ctx.rng("page_views", "page_name", off), n, _pool(PAGES))
    return {
        "page_view_id": F.pk(n, off + 1),
        "user_id": g["user_id"][sl],
        "session_id": g["session_id"][sl],
        "page_name": pages,
        "page_url": np.char.add("https://m.example.com/", pages.astype("U32")),
        "referrer": F.enum(ctx.rng("page_views", "referrer", off), n,
                           ["", "https://m.example.com/home"], [45, 55]),
        "duration_seconds": F.int_weighted(ctx.rng("page_views", "duration_seconds", off),
                                           n, [3, 10, 25, 60, 180], [20, 30, 25, 18, 7]),
        "scroll_depth_pct": F.int_weighted(ctx.rng("page_views", "scroll_depth_pct", off),
                                           n, [10, 25, 50, 75, 100], [18, 22, 25, 20, 15]),
        "view_time": vt,
        "created_at": vt,
    }


# ---------------------------------------------------------------- 社交域

def build_posts(ctx: Ctx, off: int, n: int) -> dict:
    pid = F.pk(n, off + 1)
    pub = F.ts_window(ctx.rng("posts", "published_at", off), n, ctx.start, ctx.days,
                      trend=0.40)
    # 三个计数器从明细回填（旧版 like_count = views×0.06 拍脑袋，虚高 3.3 倍，
    # 审计 L4.3）；view_count 从 like 数上推，保证 view ≥ like。
    sl = slice(off, off + n)
    cn = ctx.cache["posts_counters"]
    return {
        "post_id": pid,
        "user_id": F.fk_skewed(ctx.rng("posts", "user_id", off), n, 1, ctx.n("users")),
        "content_type": F.enum(ctx.rng("posts", "content_type", off), n,
                               ["article", "short_video", "image", "review"],
                               [20, 32, 28, 20]),
        "title": np.char.add("post_", pid.astype("U14")),
        "content": np.char.add("内容正文 ", pid.astype("U14")),
        "media_urls": F.const(n, []),
        "tags": F.const(n, []),
        "location": F.from_pool(ctx.rng("posts", "location", off), n,
                                _pool([c[0] for c in CITIES])),
        "product_ids": F.const(n, []),
        "view_count": cn["view_count"][sl],
        "like_count": cn["like_count"][sl],
        "comment_count": cn["comment_count"][sl],
        "share_count": cn["share_count"][sl],
        "status": F.enum(ctx.rng("posts", "status", off), n,
                         ["published", "draft", "hidden", "deleted"], [85, 8, 5, 2]),
        "is_featured": F.bool_p(ctx.rng("posts", "is_featured", off), n, 0.05),
        "published_at": pub,
        "created_at": pub,
        "updated_at": pub,
    }


def build_post_likes(ctx: Ctx, off: int, n: int) -> dict:
    a, b = ctx.pairs["post_likes"]
    return {
        "user_id": a[off:off + n],
        "post_id": b[off:off + n],
        "created_at": F.ts_window(ctx.rng("post_likes", "created_at", off), n,
                                  ctx.start, ctx.days, trend=0.40),
    }


def build_post_comments(ctx: Ctx, off: int, n: int) -> dict:
    cid = F.pk(n, off + 1)
    return {
        "comment_id": cid,
        # post_id 全局生成（posts.comment_count 要 bincount 它回填），builder 只切片
        "post_id": ctx.cache["post_comments_post_id"][off:off + n],
        "user_id": F.fk_skewed(ctx.rng("post_comments", "user_id", off), n, 1,
                               ctx.n("users")),
        # 回复：指向本块内更早的评论，保证外键落在已存在的 id 上
        "parent_comment_id": F.null_out(
            ctx.rng("post_comments", "parent_comment_id", off),
            np.maximum(off + 1, cid - F.int_uniform(
                ctx.rng("post_comments", "parent_off", off), n, 1, 50)), 0.72),
        "content": np.char.add("评论 ", cid.astype("U14")),
        "like_count": F.int_weighted(ctx.rng("post_comments", "like_count", off), n,
                                     [0, 1, 3, 8, 25], [45, 25, 18, 9, 3]),
        "status": F.enum(ctx.rng("post_comments", "status", off), n,
                         ["visible", "hidden", "deleted"], [92, 5, 3]),
        "created_at": F.ts_window(ctx.rng("post_comments", "created_at", off), n,
                                  ctx.start, ctx.days, trend=0.40),
    }


def build_post_shares(ctx: Ctx, off: int, n: int) -> dict:
    return {
        "share_id": F.pk(n, off + 1),
        "user_id": F.fk_skewed(ctx.rng("post_shares", "user_id", off), n, 1, ctx.n("users")),
        # post_id 全局生成（posts.share_count 要 bincount 它回填），builder 只切片
        "post_id": ctx.cache["post_shares_post_id"][off:off + n],
        "share_channel": F.enum(ctx.rng("post_shares", "share_channel", off), n,
                                ["wechat_friend", "wechat_moment", "weibo", "copy_link"],
                                [40, 32, 12, 16]),
        "created_at": F.ts_window(ctx.rng("post_shares", "created_at", off), n,
                                  ctx.start, ctx.days, trend=0.40),
    }


def build_user_follows(ctx: Ctx, off: int, n: int) -> dict:
    a, b = ctx.pairs["user_follows"]
    return {
        "follower_id": a[off:off + n],
        "following_id": b[off:off + n],
        "created_at": F.ts_window(ctx.rng("user_follows", "created_at", off), n,
                                  ctx.start, ctx.days),
    }


def build_user_messages(ctx: Ctx, off: int, n: int) -> dict:
    mid = F.pk(n, off + 1)
    sent = F.ts_window(ctx.rng("user_messages", "sent_at", off), n, ctx.start, ctx.days)
    read = F.ts_offset(ctx.rng("user_messages", "read_at", off), sent, 1, 4320,
                       cap=ctx.as_of_end, null_p=0.32)
    return {
        "message_id": mid,
        "sender_id": F.fk_skewed(ctx.rng("user_messages", "sender_id", off), n, 1,
                                 ctx.n("users")),
        "receiver_id": F.fk_uniform(ctx.rng("user_messages", "receiver_id", off), n, 1,
                                    ctx.n("users")),
        "content": np.char.add("私信 ", mid.astype("U14")),
        "message_type": F.enum(ctx.rng("user_messages", "message_type", off), n,
                               ["text", "image", "product"], [78, 15, 7]),
        "related_post_id": F.null_out(ctx.rng("user_messages", "related_post_id", off),
                                      F.fk_uniform(ctx.rng("user_messages", "rp", off), n,
                                                   1, ctx.n("posts")), 0.85),
        "related_product_id": F.null_out(
            ctx.rng("user_messages", "related_product_id", off),
            F.from_pool(ctx.rng("user_messages", "rprod", off), n,
                        ctx.dim_ids["products"]), 0.88),
        "is_read": np.array([x is not None for x in read], dtype=bool),
        "sent_at": sent,
        "read_at": read,
    }


# ---------------------------------------------------------------- 交易域

def _cond_ts(rng, base: np.ndarray, mask: np.ndarray, lo_min: int, hi_min: int,
             cap: np.datetime64) -> np.ndarray:
    """按条件生成后续时间戳：mask 为假的位置置空，偏移上限逐行对 cap 截断。

    订单生命周期的时间列（paid/shipped/delivered/cancelled/refunded_at）必须与 status
    一致——status='pending' 却有 paid_at 是脏数据，会让「支付转化」类分析出错。

    v2 改动：旧版对超出 cap 的时间戳**置空但不回退 status**，造出 5052 行
    status='refunded' 而 refunded_at 为空（审计 L4.6）。现在状态生成侧已按剩余窗口
    下调（见 _prep_orders），这里再把偏移上限压到 [lo, cap-base]，双保险后
    mask 为真的行必有非空时间戳。
    """
    base_s = base.astype("datetime64[s]")
    lo = lo_min * 60
    room = (cap - base_s).astype("timedelta64[s]").astype(np.int64)
    hi_row = np.maximum(np.minimum(hi_min * 60, room), lo + 1)
    off = (lo + rng.random(len(base_s)) * (hi_row - lo)).astype(np.int64)
    arr = np.array((base_s + off.astype("timedelta64[s]")).astype(object), dtype=object)
    arr[~mask] = None
    return arr


def build_orders(ctx: Ctx, off: int, n: int) -> dict:
    g = ctx.cache["orders"]
    sl = slice(off, off + n)
    oid = F.pk(n, off + 1)
    status = g["status"][sl]
    placed = g["placed_at"][sl]
    paid_m = np.isin(status, ["paid", "shipped", "delivered", "refunded"])
    ship_m = np.isin(status, ["shipped", "delivered"])
    deliv_m = status == "delivered"
    canc_m = status == "cancelled"
    refund_m = status == "refunded"
    return {
        "order_id": oid,
        "order_no": F.serial_text("NO", oid, width=12),
        "user_id": ctx.cache["orders_user_id"][sl],
        "status": status,
        "total_amount": g["total_amount"][sl],
        "discount_amount": g["discount_amount"][sl],
        "shipping_fee": g["shipping_fee"][sl],
        "actual_amount": g["actual_amount"][sl],
        # 件数 = order_items 真实行数（旧版另抽一份，70% 不符，审计 L4.2）；
        # 用券订单全局定（user_coupons 的核销行要对齐它，审计 L4.7）
        "item_count": ctx.cache["orders_item_count"][sl],
        "coupon_id": ctx.cache["orders_coupon_id"][sl],
        "shipping_address": F.const(n, {"province": "广东", "city": "深圳"}),
        "remark": F.null_out(ctx.rng("orders", "remark", off), F.const(n, "尽快发货"), 0.9),
        "placed_at": placed,
        "paid_at": _cond_ts(ctx.rng("orders", "paid_at", off), placed, paid_m, 1, 240,
                            ctx.as_of_end),
        "shipped_at": _cond_ts(ctx.rng("orders", "shipped_at", off), placed, ship_m,
                               240, 2880, ctx.as_of_end),
        "delivered_at": _cond_ts(ctx.rng("orders", "delivered_at", off), placed, deliv_m,
                                 2880, 10080, ctx.as_of_end),
        "cancelled_at": _cond_ts(ctx.rng("orders", "cancelled_at", off), placed, canc_m,
                                 5, 1440, ctx.as_of_end),
        "cancel_reason": np.where(canc_m, "用户取消", None),
        "refunded_at": _cond_ts(ctx.rng("orders", "refunded_at", off), placed, refund_m,
                                1440, 20160, ctx.as_of_end),
        "refund_reason": np.where(refund_m, "商品问题", None),
        "created_at": placed,
        "updated_at": placed,
    }


def build_order_items(ctx: Ctx, off: int, n: int) -> dict:
    # 金额来自 _prep_order_items 的全局缩放结果：每单明细 actual_amount 之和
    # 精确（到分）等于订单头 total_amount（审计 L4.1 抓的 99.99% 不符）。
    # created_at = 所属订单下单时刻，不再独立乱抽。
    sl = slice(off, off + n)
    it = ctx.cache["items"]
    oids = ctx.cache["order_items_order_id"][sl]
    prods = ctx.dim_ids["products"]
    pnames = ctx.dim_ids["_product_names"]
    idx = F.fk_skewed(ctx.rng("order_items", "product_id", off), n, 1, len(prods)) - 1
    pid = prods[idx]
    return {
        "item_id": F.pk(n, off + 1),
        "order_id": oids,
        "product_id": pid,
        # 反范式的真实商品名（不是占位符），与 products.product_name 一致
        "product_name": pnames[idx],
        "sku_id": pid * 100 + F.int_uniform(ctx.rng("order_items", "sku", off), n, 1, 9),
        "sku_name": np.char.add(pnames[idx].astype("U80"), "-标准装"),
        "quantity": it["quantity"][sl],
        "unit_price": it["unit_price"][sl],
        "discount_amount": it["discount_amount"][sl],
        "actual_amount": it["actual_amount"][sl],
        "created_at": it["created_at"][sl],
    }


def build_payments(ctx: Ctx, off: int, n: int) -> dict:
    # 每个付过钱的订单恰好一条支付：valid → success、refunded → refunded，
    # 与订单状态由构造保证一致（旧版抽样式覆盖漏掉 10% 有效单，审计 L4.6）。
    g = ctx.cache["orders"]
    idx = ctx.cache["payments_order_idx"][off:off + n]     # 0-based 订单下标
    oids = idx + 1
    pid = F.pk(n, off + 1)
    base = g["placed_at"][idx]
    amt = g["actual_amount"][idx]
    refunded = g["status"][idx] == "refunded"
    always = np.ones(n, dtype=bool)
    return {
        "payment_id": pid,
        "payment_no": F.serial_text("PAY", pid, width=12),
        "order_id": oids,
        "user_id": ctx.cache["orders_user_id"][idx],   # 与所属订单同一用户
        "amount": amt,
        "payment_method": F.enum(ctx.rng("payments", "payment_method", off), n,
                                 ["wechat", "alipay", "credit_card", "balance"],
                                 [45, 40, 10, 5]),
        "payment_channel": F.enum(ctx.rng("payments", "payment_channel", off), n,
                                  ["app", "h5", "mini_program"], [58, 22, 20]),
        "status": np.where(refunded, "refunded", "success"),
        "transaction_id": F.serial_text("TXN", pid, width=16),
        "paid_at": _cond_ts(ctx.rng("payments", "paid_at", off), base, always,
                            1, 240, ctx.as_of_end),
        "failure_reason": F.const(n, None),
        "refund_amount": np.where(refunded, amt, 0.0),
        "refunded_at": _cond_ts(ctx.rng("payments", "refunded_at", off), base, refunded,
                                1440, 20160, ctx.as_of_end),
        "created_at": base,
    }


def build_subscriptions(ctx: Ctx, off: int, n: int) -> dict:
    sid = F.pk(n, off + 1)
    plan = F.enum(ctx.rng("subscriptions", "plan_name", off), n,
                  ["monthly", "quarterly", "yearly"], [55, 28, 17])
    price = np.where(plan == "monthly", 19.9, np.where(plan == "quarterly", 49.9, 168.0))
    days = np.where(plan == "monthly", 30, np.where(plan == "quarterly", 90, 365))
    start = F.ts_window(ctx.rng("subscriptions", "start_date", off), n, ctx.start,
                        ctx.days).astype("datetime64[D]")
    st = F.enum(ctx.rng("subscriptions", "status", off), n,
                ["active", "expired", "cancelled"], [62, 26, 12])
    return {
        "subscription_id": sid,
        "user_id": F.fk_skewed(ctx.rng("subscriptions", "user_id", off), n, 1,
                               ctx.n("users")),
        "plan_name": plan,
        "plan_price": price,
        "start_date": start,
        "end_date": start + days.astype("timedelta64[D]"),
        "auto_renew": F.bool_p(ctx.rng("subscriptions", "auto_renew", off), n, 0.68),
        "status": st,
        "payment_id": F.null_out(ctx.rng("subscriptions", "payment_id", off),
                                 F.fk_uniform(ctx.rng("subscriptions", "pay", off), n, 1,
                                              ctx.n("payments")), 0.15),
        "cancelled_at": _cond_ts(ctx.rng("subscriptions", "cancelled_at", off),
                                 start.astype("datetime64[s]"), st == "cancelled",
                                 1440, 60 * 1440, ctx.as_of_end),
        "cancel_reason": np.where(st == "cancelled", "不再需要", None),
        "created_at": start.astype("datetime64[s]"),
        "updated_at": start.astype("datetime64[s]"),
    }


# ---------------------------------------------------------------- 归因域

def build_user_attributions(ctx: Ctx, off: int, n: int) -> dict:
    users = ctx.cache["attributed_users"][off:off + n]
    aid = F.pk(n, off + 1)
    chans = ctx.dim_ids["channels"]
    click = F.ts_window(ctx.rng("user_attributions", "click_time", off), n, ctx.start,
                        ctx.days)
    d2i = F.int_weighted(ctx.rng("user_attributions", "days_to_install", off), n,
                         [0, 1, 2, 5], [62, 22, 10, 6])
    return {
        "attribution_id": aid,
        "user_id": users,
        "channel_id": F.from_pool(ctx.rng("user_attributions", "channel_id", off), n, chans),
        "ad_campaign_id": F.null_out(
            ctx.rng("user_attributions", "ad_campaign_id", off),
            F.from_pool(ctx.rng("user_attributions", "acid", off), n,
                        ctx.dim_ids["ad_campaigns"]), 0.35),
        "creative_id": F.null_out(
            ctx.rng("user_attributions", "creative_id", off),
            F.from_pool(ctx.rng("user_attributions", "crid", off), n,
                        ctx.dim_ids["ad_creatives"]), 0.40),
        "attribution_type": F.enum(ctx.rng("user_attributions", "attribution_type", off),
                                   n, ["last_touch", "first_touch", "linear"],
                                   [70, 22, 8]),
        "click_time": click,
        "install_time": click.astype("datetime64[s]") + (d2i * 86400).astype("timedelta64[s]"),
        "attributed_at": click,
        "days_to_install": d2i,
        "tracking_params": F.const(n, {"utm_source": "douyin"}),
    }


# ---------------------------------------------------------------- 营销域

def build_user_coupons(ctx: Ctx, off: int, n: int) -> dict:
    """前 uc_n_used 行是核销闭环行：与用券订单一一对应，(user, coupon, order) 三元
    对齐、used_at = 下单时刻、received_at 落在 [注册, 下单] 且距下单 ≤30 天。
    其余行只有 unused / expired，expired 由 expire_at 是否已过窗末决定。

    旧版 status 以 50% 概率抽 used、order_id 均匀乱指，与 orders.coupon_id 各说
    各话，核销数是订单数的 5.7 倍（审计 L4.7）。
    """
    cid = F.pk(n, off + 1)
    gidx = np.arange(off, off + n, dtype=np.int64)
    n_used = ctx.cache["uc_n_used"]
    used_m = gidx < n_used

    og = ctx.cache["orders"]
    reg_all = ctx.cache["users_registered_at"].astype("datetime64[s]")
    win_end = (ctx.start.astype("datetime64[s]")
               + np.timedelta64(ctx.days * 86400, "s"))
    r = ctx.rng("user_coupons", "recv", off)

    # —— 核销段（未用行按 0 号占位算，最后 where 合并，保持向量化）——
    safe = np.minimum(gidx, max(n_used - 1, 0))
    o = ctx.cache["orders_coupon_rows"][safe] if n_used else np.zeros(n, np.int64)
    uid_u = ctx.cache["orders_user_id"][o]
    cp_u = ctx.cache["orders_coupon_id"][o].astype(np.int64)   # 用券订单必非空
    placed = og["placed_at"][o].astype("datetime64[s]")
    span = np.minimum(np.maximum(
        (placed - reg_all[uid_u - 1]).astype("timedelta64[s]").astype(np.int64), 120),
        30 * 86400)
    recv_u = np.maximum(
        placed - (60 + r.random(n) * (span - 60)).astype("timedelta64[s]"),
        reg_all[uid_u - 1])

    # —— 未用段：领券时刻落在 [本人注册, 窗末) ——
    uid_g = F.fk_skewed(ctx.rng("user_coupons", "user_id", off), n, 1, ctx.n("users"))
    reg_g = reg_all[uid_g - 1]
    room = np.maximum(
        (win_end - reg_g).astype("timedelta64[s]").astype(np.int64) - 60, 60)
    recv_g = reg_g + (r.random(n) * room).astype("timedelta64[s]")
    cp_g = F.from_pool(ctx.rng("user_coupons", "coupon_id", off), n,
                       ctx.dim_ids["coupons"])

    picked = np.where(used_m, cp_u, cp_g)
    recv = np.where(used_m, recv_u, recv_g).astype("datetime64[s]")
    expire = recv + np.timedelta64(30 * 86400, "s")
    # 枚举取 'unused' 而非 DDL 注释里的 'available'：卡片、示例 SQL、eval 金标、
    # 以及 v1 已提交的 CSV 四方都用 unused，只有 DDL 注释是孤例。跟注释走会让
    # agent 照卡片写 status='unused' 查出 0 行（我们在 eval 里踩过这一刀）。
    st = np.where(used_m, "used",
                  np.where(expire < ctx.as_of_end, "expired", "unused"))
    used_at = np.array(placed.astype(object), dtype=object)
    used_at[~used_m] = None
    # 未使用的券不该带订单号：必须是 NULL 而不是 0（0 不是合法 order_id，
    # 会让「用券订单数」这类统计凭空多出一批）
    order_id = (o + 1).astype(object)
    order_id[~used_m] = None
    return {
        "id": cid,
        "user_id": np.where(used_m, uid_u, uid_g),
        "coupon_id": picked,
        "coupon_code": np.char.add("CP", np.char.zfill(picked.astype("U10"), 6)),
        "received_at": recv,
        "expire_at": expire,
        "used_at": used_at,
        "order_id": order_id,
        "status": st,
        "source": F.enum(ctx.rng("user_coupons", "source", off), n,
                         ["campaign", "new_user", "share", "purchase"], [45, 22, 18, 15]),
    }


def build_push_notifications(ctx: Ctx, off: int, n: int) -> dict:
    pid = F.pk(n, off + 1)
    sched = F.ts_window(ctx.rng("push_notifications", "scheduled_at", off), n, ctx.start,
                        ctx.days)
    delivered = F.bool_p(ctx.rng("push_notifications", "is_delivered", off), n, 0.88)
    opened = delivered & F.bool_p(ctx.rng("push_notifications", "is_opened", off), n, 0.21)
    return {
        "push_id": pid,
        "user_id": F.fk_skewed(ctx.rng("push_notifications", "user_id", off), n, 1,
                               ctx.n("users")),
        "campaign_id": F.null_out(
            ctx.rng("push_notifications", "campaign_id", off),
            F.from_pool(ctx.rng("push_notifications", "cid", off), n,
                        ctx.dim_ids["campaigns"]), 0.18),
        "push_type": F.enum(ctx.rng("push_notifications", "push_type", off), n,
                            ["marketing", "transactional", "system"], [58, 30, 12]),
        "title": np.char.add("推送_", pid.astype("U12")),
        "content": np.char.add("推送内容 ", pid.astype("U12")),
        "deep_link": F.const(n, "app://home"),
        "scheduled_at": sched,
        "sent_at": sched,
        "delivered_at": _cond_ts(ctx.rng("push_notifications", "delivered_at", off), sched,
                                 delivered, 1, 30, ctx.as_of_end),
        "opened_at": _cond_ts(ctx.rng("push_notifications", "opened_at", off), sched,
                              opened, 2, 1440, ctx.as_of_end),
        "is_delivered": delivered,
        "is_opened": opened,
        "failure_reason": np.where(delivered, None, "token 失效"),
        "created_at": sched,
    }


# ---------------------------------------------------------------- 实验域

def build_ab_test_assignments(ctx: Ctx, off: int, n: int) -> dict:
    a, b = ctx.pairs["ab_test_assignments"]
    uid, tid = a[off:off + n], b[off:off + n]
    # variant 必须属于所分配的 test：从该 test 的 variant 里挑
    v_by_test = ctx.dim_ids["_variants_by_test"]
    r = ctx.rng("ab_test_assignments", "variant_id", off)
    variant = np.array([vs[r.integers(0, len(vs))] for vs in (v_by_test[t] for t in tid)],
                       dtype=np.int64)
    assigned = F.ts_window(ctx.rng("ab_test_assignments", "assigned_at", off), n,
                           ctx.start, ctx.days)
    return {
        "id": F.pk(n, off + 1),
        "user_id": uid,
        "test_id": tid,
        "variant_id": variant,
        "assigned_at": assigned,
        "first_exposure_at": F.ts_offset(
            ctx.rng("ab_test_assignments", "first_exposure_at", off), assigned,
            1, 4320, cap=ctx.as_of_end, null_p=0.12),
    }


# ---------------------------------------------------------------- 注册表

BUILDERS = {
    "users": build_users,
    "user_profiles": build_user_profiles,
    "user_devices": build_user_devices,
    "user_segment_members": build_user_segment_members,
    "sessions": build_sessions,
    "events": build_events,
    "page_views": build_page_views,
    "posts": build_posts,
    "post_likes": build_post_likes,
    "post_comments": build_post_comments,
    "post_shares": build_post_shares,
    "user_follows": build_user_follows,
    "user_messages": build_user_messages,
    "orders": build_orders,
    "order_items": build_order_items,
    "payments": build_payments,
    "subscriptions": build_subscriptions,
    "user_attributions": build_user_attributions,
    "user_coupons": build_user_coupons,
    "push_notifications": build_push_notifications,
    "ab_test_assignments": build_ab_test_assignments,
}

# 灌库顺序（外键依赖序）。维度表由 load 脚本先灌，这里只管事实表。
LOAD_ORDER = [
    "users", "user_profiles", "user_devices", "user_segment_members",
    "sessions", "events", "page_views",
    "posts", "post_likes", "post_comments", "post_shares", "user_follows",
    "user_messages",
    "orders", "order_items", "payments",
    "user_attributions", "user_coupons", "push_notifications",
    "ab_test_assignments",
    "subscriptions",          # 依赖 payments
]
