# -*- coding: utf-8 -*-
"""
A8 补充实验 —— 提前终止（STOP_STABLE）对解质量与耗时的影响
==========================================================
A8 要求支持「连续无改善即提前终止」。这个策略天然是「省时间 ↔ 损质量」的取舍，
因此必须量化，不能只凭直觉开关：

    对照 A（基线）：关闭提前终止（stable_s=inf, gap_stop_pct=-1）→ 一定跑满预算
    对照 B（默认）：stable_s = clamp(0.2×预算, 3s, 60s)，gap 阈值 5%

实例与 P1 报告一致（make_instance + Haversine 离线矩阵），零 API 成本、可复现。

输出：每档的 费用、实际耗时、提前终止比例、迭代次数，以及 B 相对 A 的费用差异。
"""
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from optimizer import optimize  # noqa: E402
from p1_alns_benefit import make_haversine_dm, make_instance  # noqa: E402

INF = float("inf")


def run(n, seed, budget, early, rng_seed):
    """
    同一 rng_seed 下，B 的迭代序列是 A 的前缀（ALNS 用的是全局 random）。
    这样 Δcost 才是有意义的「提前终止的代价」，而不是两次不同随机游走的噪声。
    """
    ps, dest, points = make_instance(n, seed)
    dm = make_haversine_dm(points)
    diag = {}
    kwargs = ({} if early else {"stable_s": INF, "gap_stop_pct": -1})
    random.seed(rng_seed)
    best, base = optimize(ps, dest, dm, mode="cost", time_budget_s=budget,
                          diag=diag, **kwargs)
    j = diag["job"]
    return {"cost": sum(v.cost for v in best), "base": base,
            "elapsed": j["elapsed"], "status": j["status"],
            "iters": j["iteration"], "gap": j["gap"],
            "vehicles": len(best)}


def main():
    sizes = [int(x) for x in (os.environ.get("A8_SIZES") or "15,30,45").split(",")]
    seeds = [int(x) for x in (os.environ.get("A8_SEEDS") or "1,2,3").split(",")]
    budgets = [float(x) for x in (os.environ.get("A8_BUDGETS") or "10,30").split(",")]

    print("=" * 78)
    print("A8 提前终止 A/B：A=跑满预算  B=默认（无改善即停）")
    print("=" * 78)
    summary = []
    for n in sizes:
        for budget in budgets:
            rows_a, rows_b = [], []
            for seed in seeds:
                rng_seed = 1000 * n + seed          # 同一实例两对照共用同一随机序列
                a = run(n, seed, budget, early=False, rng_seed=rng_seed)
                b = run(n, seed, budget, early=True, rng_seed=rng_seed)
                rows_a.append(a)
                rows_b.append(b)
                print(f"n={n:3d} seed={seed} budget={budget:4.0f}s | "
                      f"A {a['cost']:8.2f}元 {a['elapsed']:5.1f}s {a['status']:12s} "
                      f"it={a['iters']:7d} | "
                      f"B {b['cost']:8.2f}元 {b['elapsed']:5.1f}s {b['status']:12s} "
                      f"it={b['iters']:7d} | Δcost {(b['cost'] - a['cost']) / a['cost'] * 100:+5.2f}%")
            ca = statistics.mean(r["cost"] for r in rows_a)
            cb = statistics.mean(r["cost"] for r in rows_b)
            ta = statistics.mean(r["elapsed"] for r in rows_a)
            tb = statistics.mean(r["elapsed"] for r in rows_b)
            early_n = sum(1 for r in rows_b if r["status"] == "STOP_STABLE")
            summary.append({
                "n": n, "budget": budget, "cost_a": ca, "cost_b": cb,
                "delta_pct": (cb - ca) / ca * 100, "t_a": ta, "t_b": tb,
                "early_ratio": early_n / len(rows_b),
            })
            print(f"  → 均值 n={n} budget={budget:.0f}s: 费用 {ca:.2f} → {cb:.2f} "
                  f"({(cb - ca) / ca * 100:+.2f}%)，耗时 {ta:.1f}s → {tb:.1f}s "
                  f"({(tb - ta) / ta * 100:+.0f}%)，提前终止 {early_n}/{len(rows_b)}")

    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)
    print(f"{'n':>4} {'预算':>6} {'费用A':>10} {'费用B':>10} {'Δ费用':>8} "
          f"{'耗时A':>8} {'耗时B':>8} {'提前停':>8}")
    for s in summary:
        print(f"{s['n']:>4} {s['budget']:>6.0f} {s['cost_a']:>10.2f} {s['cost_b']:>10.2f} "
              f"{s['delta_pct']:>+7.2f}% {s['t_a']:>7.1f}s {s['t_b']:>7.1f}s "
              f"{s['early_ratio'] * 100:>7.0f}%")
    avg = statistics.mean(s["delta_pct"] for s in summary)
    print(f"\n平均费用差：{avg:+.2f}%（正=提前终止更贵）")


if __name__ == "__main__":
    main()
