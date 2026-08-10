#!/usr/bin/env python3
"""跨表闭环自测：scale=1 全内存生成，逐条断言审计抓过的缺陷已修。

## 与 selftest_fillers 的分工

selftest_fillers 测**单个填充函数**的分布形状；本文件测**表与表之间的闭环**——
数据审计（docs/data-audit.md）证明单表全对、跨表全错正是旧版的失败模式，
所以这层自测断言的全部是跨表性质：

    头=明细和（精确到分）、item_count=真实行数、三计数器=bincount、
    等级规则成立、每个付钱订单一条支付、券核销三元对齐、
    漏斗单调递减、留存单调衰减、先注册后行为。

## 用法

    python3 scripts/gen/selftest_closures.py        # 约 5 秒，无云依赖

在 test-plan 里排 L0 之后（改生成器后先跑 fillers 再跑这个），全过再谈重灌。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import budget                              # noqa: E402
import tables as T                         # noqa: E402
from main import load_dim_ids              # noqa: E402

PASS = 0


def ok(cond, msg: str) -> None:
    global PASS
    assert cond, f"FAIL: {msg}"
    PASS += 1
    print(f"  ok  {msg}")


def cents(a) -> np.ndarray:
    return np.round(np.asarray(a, dtype=float) * 100).astype(np.int64)


def main() -> int:
    rows = budget.table_rows(1.0)
    start = np.datetime64(budget.DATA_START, "s")
    as_of_end = (np.datetime64(budget.AS_OF, "D").astype("datetime64[s]")
                 + np.timedelta64(86399, "s"))
    ctx = T.Ctx(seed=42, rows=rows, start=start, days=budget.WINDOW_DAYS,
                as_of_end=as_of_end, dim_ids=load_dim_ids())
    T.prepare_globals(ctx)

    t = {}
    for name, build in T.BUILDERS.items():
        t[name] = build(ctx, 0, ctx.n(name))

    nu = ctx.n("users")
    days = budget.WINDOW_DAYS

    # ---------- L4.1 / L4.2 订单头 vs 明细 ----------
    o, it = t["orders"], t["order_items"]
    no = len(o["order_id"])
    item_sum_c = np.bincount(it["order_id"], weights=cents(it["actual_amount"]),
                             minlength=no + 1)[1:].astype(np.int64)
    ok((item_sum_c == cents(o["total_amount"])).all(),
       "每单明细 actual_amount 之和 == 订单头 total_amount（精确到分）")
    real_cnt = np.bincount(it["order_id"], minlength=no + 1)[1:]
    ok((o["item_count"] == real_cnt).all(), "orders.item_count == 明细真实行数")
    ok((cents(it["unit_price"]) * it["quantity"] - cents(it["discount_amount"])
        == cents(it["actual_amount"])).all(),
       "明细行内公式 actual = unit×qty − discount 精确成立")
    ok((np.asarray(it["actual_amount"]) > 0).all(), "明细金额全部为正")
    ok((cents(o["actual_amount"]) == cents(o["total_amount"])
        - cents(o["discount_amount"]) + cents(o["shipping_fee"])).all(),
       "订单头公式 actual = total − discount + shipping 成立")

    # ---------- L4.3 / L4.4 计数器回填 ----------
    p = t["posts"]
    npost = len(p["post_id"])
    for src, col in (("post_likes", "like_count"), ("post_comments", "comment_count"),
                     ("post_shares", "share_count")):
        real = np.bincount(t[src]["post_id"], minlength=npost + 1)[1:]
        ok((p[col] == real).all(), f"posts.{col} == {src} 真实行数")
    ok((p["view_count"] >= p["like_count"]).all(), "view_count ≥ like_count")

    s = t["sessions"]
    ns = len(s["session_id"])
    ev_real = np.bincount(t["events"]["session_id"], minlength=ns + 1)[1:]
    pv_real = np.bincount(t["page_views"]["session_id"], minlength=ns + 1)[1:]
    ok((s["event_count"] == ev_real).all(), "sessions.event_count == events 真实行数")
    ok((s["page_view_count"] == pv_real).all(),
       "sessions.page_view_count == page_views 真实行数")
    ok((np.asarray(s["is_bounce"]) == (pv_real <= 1)).all(), "is_bounce == (pv ≤ 1)")

    # ---------- L4.5 user_level 规则 ----------
    u = t["users"]
    valid_m = np.isin(o["status"], list(T.VALID_STATUS))
    spend = np.bincount(o["user_id"][valid_m],
                        weights=np.asarray(o["actual_amount"], float)[valid_m],
                        minlength=nu + 1)[1:]
    lvl, vip = np.asarray(u["user_level"]), np.asarray(u["is_vip"])
    ok((lvl[(spend >= 10000) | vip] == 5).all(), "消费≥10000 或 VIP ⇒ level 5")
    ok((spend[lvl == 4] >= 1000).all() and (~vip[lvl == 4]).all(),
       "level 4 全部满足消费≥1000 且非 VIP")
    ok((spend[lvl <= 3] < 1000).all(), "level ≤3 消费全部 <1000（优先级正确）")
    share_1k = [float((spend[lvl == k] >= 1000).mean()) for k in (1, 2, 3)]
    ok(max(share_1k) == 0.0, "低等级里没有漏网的高消费用户（旧版五档全是 21%）")

    # ---------- L4.6 支付覆盖与生命周期 ----------
    pay = t["payments"]
    st_by_oid = dict(zip(pay["order_id"].tolist(), np.asarray(pay["status"]).tolist()))
    valid_ids = o["order_id"][valid_m]
    ok(all(st_by_oid.get(i) == "success" for i in valid_ids.tolist()),
       "每个有效订单恰有一条 success 支付")
    refund_ids = o["order_id"][np.asarray(o["status"]) == "refunded"]
    ok(all(st_by_oid.get(i) == "refunded" for i in refund_ids.tolist()),
       "每个退款订单恰有一条 refunded 支付")
    amt_by_oid = dict(zip(pay["order_id"].tolist(),
                          cents(pay["amount"]).tolist()))
    ok(all(amt_by_oid[i] == c for i, c in
           zip(o["order_id"].tolist(), cents(o["actual_amount"]).tolist())
           if i in amt_by_oid), "支付金额 == 订单 actual_amount")
    for stt, col in (("refunded", "refunded_at"), ("cancelled", "cancelled_at")):
        m = np.asarray(o["status"]) == stt
        ok(all(x is not None for x in np.asarray(o[col], dtype=object)[m]),
           f"status={stt} ⇒ {col} 非空")
    paid_m = np.isin(o["status"], ["paid", "shipped", "delivered", "refunded"])
    ok(all(x is not None for x in np.asarray(o["paid_at"], dtype=object)[paid_m]),
       "已付状态 ⇒ paid_at 非空")

    # ---------- L4.7 券核销闭环 ----------
    uc = t["user_coupons"]
    used_m = np.asarray(uc["status"]) == "used"
    cp_orders = np.flatnonzero(
        np.array([x is not None for x in np.asarray(o["coupon_id"], dtype=object)]))
    ok(int(used_m.sum()) == len(cp_orders), "used 券数 == 用券订单数（1:1）")
    uc_oid = np.array([x for x in np.asarray(uc["order_id"], dtype=object)[used_m]],
                      dtype=np.int64)
    ok((np.sort(uc_oid) == np.sort(cp_orders + 1)).all(), "核销行 order_id 与用券订单集合一致")
    idx = uc_oid - 1
    ok((np.asarray(uc["user_id"])[used_m] == o["user_id"][idx]).all(),
       "核销行 user 与订单 user 一致")
    ok((np.asarray(uc["coupon_id"])[used_m]
        == np.array([int(x) for x in np.asarray(o["coupon_id"], dtype=object)[idx]])).all(),
       "核销行 coupon 与订单 coupon 一致")
    ok(all(x is None for x in np.asarray(uc["order_id"], dtype=object)[~used_m]),
       "非核销行 order_id 全为空")
    exp_ts = np.asarray(uc["expire_at"]).astype("datetime64[s]")
    ok((exp_ts[np.asarray(uc["status"]) == "expired"] < as_of_end).all()
       and (exp_ts[np.asarray(uc["status"]) == "unused"] >= as_of_end).all(),
       "expired/unused 与 expire_at 是否过窗一致")

    # ---------- L4.8 / L5.6 事件保底与漏斗 ----------
    ev = t["events"]
    name = np.asarray(ev["event_name"], dtype=object)
    cnt = {n_: int((name == n_).sum())
           for n_ in ("register", "purchase", "use_coupon", "view_home",
                      "view_product", "add_to_cart", "begin_checkout")}
    ok(cnt["register"] == nu, "register 事件数 == 用户数")
    ok(cnt["purchase"] == int(valid_m.sum()), "purchase 事件数 == 有效订单数")
    ok(cnt["use_coupon"] == len(cp_orders), "use_coupon 事件数 == 核销券数")
    ok(cnt["view_home"] > cnt["view_product"] > cnt["add_to_cart"]
       > cnt["begin_checkout"] > cnt["purchase"],
       f"漏斗单调递减 {[cnt[k] for k in ('view_home','view_product','add_to_cart','begin_checkout','purchase')]}")

    # ---------- L5.8 留存衰减 + 先注册后行为 ----------
    reg = np.asarray(u["registered_at"]).astype("datetime64[s]")
    ev_uid = np.asarray(ev["user_id"])
    ev_ts = np.asarray(ev["event_time"]).astype("datetime64[s]")
    ok((ev_ts >= reg[ev_uid - 1] - np.timedelta64(1, "s")).all(), "事件时刻 ≥ 本人注册时刻")
    sess_day = (np.asarray(s["start_time"]).astype("datetime64[D]")
                - reg[s["user_id"] - 1].astype("datetime64[D]")).astype(np.int64)
    ok((sess_day >= 0).all(), "会话日期 ≥ 本人注册日期")
    ok((o["placed_at"].astype("datetime64[s]") >= reg[o["user_id"] - 1]).all(),
       "下单时刻 ≥ 本人注册时刻")
    ok((np.asarray(uc["received_at"]).astype("datetime64[s]")
        >= reg[np.asarray(uc["user_id"]) - 1]).all(), "领券时刻 ≥ 本人注册时刻")
    sess_uid = np.zeros(ns + 1, np.int64)
    sess_uid[s["session_id"]] = s["user_id"]
    ok((ev_uid == sess_uid[ev["session_id"]]).all(), "事件 user == 所属会话 user")

    lag = ((ev_ts - reg[ev_uid - 1]).astype("timedelta64[D]").astype(np.int64))
    h = np.bincount(lag[(lag >= 0) & (lag <= 30)], minlength=31)
    ok(h[0] > h[7] > h[14] > h[28],
       f"活跃量随注册后天数单调衰减 D0={h[0]} D7={h[7]} D14={h[14]} D28={h[28]}")

    # ---------- 最后活跃 ----------
    la = np.asarray(u["last_active_at"]).astype("datetime64[s]")
    ok((la >= reg).all(), "last_active_at ≥ registered_at")
    ok((la <= as_of_end).all(), "last_active_at 不越窗")

    print(f"\n全部通过（{PASS} 项断言）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
