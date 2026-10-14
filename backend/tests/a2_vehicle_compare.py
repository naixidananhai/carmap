# -*- coding: utf-8 -*-
"""
A2 车型对照实测 —— auto / six / four 三档的差异有多大
======================================================
问题：4 座与 6 座**计价完全相同**，「车型」是纯可行性变量。那么三档偏好到底
     改变了什么？本脚本用同一批实例实测，回答两件事：

  1. auto 与 six 是否**完全等价**（同为容量上限 6）——只是车型标签不同？
  2. four（容量上限降到 4）付出的代价是多少——多几辆车、多花多少钱？

用法：
    python a2_vehicle_compare.py            # 默认 15/30 人 × 2 种子
    python a2_vehicle_compare.py 15 30 45   # 指定规模

结果同时打印到屏幕并写入 a2_vehicle_compare_result.txt（随代码落盘）。
全部离线（Haversine），零 API 成本。
"""
import io
import os
import random
import statistics
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))
sys.path.insert(0, _HERE)

from optimizer import (MAX_ROUTE_SECONDS, optimize, capacity_limit,
                       count_vehicle_types, assign_vehicle_type)
from p1_alns_benefit import make_haversine_dm, make_instance

BUDGET = 5.0
SEEDS = (1, 2)
PREFS = ("auto", "six", "four")

BUFFER = []


def emit(line=""):
    BUFFER.append(line)
    print(line)


def run_one(n, seed, pref):
    ps, dest, points = make_instance(n, seed)
    dm = make_haversine_dm(points)
    diag = {}
    random.seed(1000 * n + seed)
    best, base = optimize(ps, dest, dm, mode="cost", time_budget_s=BUDGET,
                          diag=diag, vehicle_pref=pref)
    job = diag["job"]
    cost = sum(v.cost for v in best)
    pax = [len(v.passengers) for v in best]
    worst = max((v.duration_s for v in best), default=0)
    return {
        "n": n, "seed": seed, "pref": pref,
        "vehicles": len(best), "cost": cost, "baseline": base,
        "four": job["four_seat_count"], "six": job["six_seat_count"],
        "types": job["vehicle_types"], "pax": pax,
        "worst_s": worst, "feasible": job["feasible"],
        "lb": job["lower_bound"], "gap": job["gap"],
    }


def main():
    sizes = [int(x) for x in sys.argv[1:]] or [15, 30]
    emit("A2 车型对照实测（费用完全同价，车型只影响可行性）")
    emit(f"预算 {BUDGET}s/次 · Haversine 离线实例 · 种子 {SEEDS}")
    emit("=" * 78)

    rows = {}
    for n in sizes:
        for sd in SEEDS:
            for pref in PREFS:
                rows[(n, sd, pref)] = run_one(n, sd, pref)

    # ---- 表 1：逐实例明细 ----
    emit("\n[表 1] 逐实例结果")
    emit(f"{'n':>4} {'seed':>4} {'偏好':>5} {'车数':>5} {'4座':>4} {'6座':>4} "
         f"{'费用':>9} {'最长路线s':>10} {'可行':>5}")
    emit("-" * 78)
    for n in sizes:
        for sd in SEEDS:
            for pref in PREFS:
                r = rows[(n, sd, pref)]
                emit(f"{r['n']:>4} {r['seed']:>4} {r['pref']:>5} {r['vehicles']:>5} "
                     f"{r['four']:>4} {r['six']:>4} {r['cost']:>9.2f} "
                     f"{r['worst_s']:>10.0f} {'是' if r['feasible'] else '否':>5}")
        emit("-" * 78)

    # ---- 表 2：auto vs six 的等价性 ----
    emit("\n[表 2] auto 与 six 是否等价（同为容量上限 6）")
    emit(f"{'n':>4} {'seed':>4} {'Δ车数':>6} {'Δ费用':>9} {'Δ费用%':>8} {'车型标签相同':>12}")
    emit("-" * 78)
    same_all = True
    for n in sizes:
        for sd in SEEDS:
            a = rows[(n, sd, "auto")]
            s = rows[(n, sd, "six")]
            dcost = a["cost"] - s["cost"]
            pct = dcost / s["cost"] * 100 if s["cost"] else 0.0
            same_struct = (a["vehicles"] == s["vehicles"] and a["pax"] == s["pax"]
                           and abs(dcost) < 1e-6)
            same_label = a["types"] == s["types"]
            same_all = same_all and same_struct
            emit(f"{n:>4} {sd:>4} {a['vehicles'] - s['vehicles']:>6} {dcost:>9.4f} "
                 f"{pct:>7.3f}% {'相同' if same_label else '不同':>12}")
    emit("-" * 78)
    emit(f"结论：auto 与 six 的(车数, 逐车载客数, 费用) {'完全一致' if same_all else '存在差异'}"
         "——车型不进入搜索，只改派车标签")

    # ---- 表 3：four 的代价 ----
    emit("\n[表 3] four（容量上限 4）相对 auto 的代价")
    emit(f"{'n':>4} {'seed':>4} {'车数 auto→four':>16} {'费用 auto→four':>22} {'多花%':>8}")
    emit("-" * 78)
    extra_v, extra_p = [], []
    for n in sizes:
        for sd in SEEDS:
            a = rows[(n, sd, "auto")]
            f = rows[(n, sd, "four")]
            pct = (f["cost"] - a["cost"]) / a["cost"] * 100 if a["cost"] else 0.0
            extra_v.append(f["vehicles"] - a["vehicles"])
            extra_p.append(pct)
            emit(f"{n:>4} {sd:>4} {a['vehicles']:>7} → {f['vehicles']:<6} "
                 f"{a['cost']:>9.2f} → {f['cost']:<9.2f} {pct:>7.2f}%")
    emit("-" * 78)
    if extra_v:
        emit(f"平均多派 {statistics.mean(extra_v):+.1f} 辆车，平均多花 "
             f"{statistics.mean(extra_p):+.2f}%（上限 {max(extra_p):.2f}%）")

    # ---- 表 4：车型硬规则与下界 ----
    emit("\n[表 4] 硬规则与下界自洽性")
    bad_rule, bad_lb = 0, 0
    for key, r in rows.items():
        for t, p in zip(r["types"], r["pax"]):
            if p >= 5 and t != 6:
                bad_rule += 1
            if t != assign_vehicle_type(p, r["pref"]):
                bad_rule += 1
        if r["cost"] < r["lb"] - 1e-6:
            bad_lb += 1
        if r["worst_s"] > MAX_ROUTE_SECONDS:
            bad_lb += 1
    emit(f"违反「5~6 人必 6 座 / 车型与载客数匹配」的车辆数：{bad_rule}")
    emit(f"违反「费用 ≥ 下界」或「单车 ≤ 5400s」的实例数：{bad_lb}")
    emit(f"共 {len(rows)} 个实例 · 4 座车共 "
         f"{sum(r['four'] for r in rows.values())} 辆 · 6 座车共 "
         f"{sum(r['six'] for r in rows.values())} 辆")
    emit("\n" + "=" * 78)
    emit("全部通过 ✔" if (bad_rule == 0 and bad_lb == 0) else "存在违反项，见上 ✘")

    out = os.path.join(_HERE, "a2_vehicle_compare_result.txt")
    io.open(out, "w", encoding="utf-8", newline="\n").write("\n".join(BUFFER) + "\n")
    print(f"\n结果已写入 {out}")
    sys.exit(0 if (bad_rule == 0 and bad_lb == 0) else 1)


if __name__ == "__main__":
    main()
