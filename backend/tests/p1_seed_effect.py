# -*- coding: utf-8 -*-
"""
P1 补充实验 —— ALNS 的收益是否依赖"差的初始解"
================================================
背景：build_initial_solution 里每个 DBSCAN 噪声点都单独开一辆车，
      在 eps=2km 的参数下大量点被判为噪声，初始解车数高达 ceil(n/1.5)。
      因此需要区分：ALNS 的收益是「真本事」还是「替 DBSCAN 擦屁股」。

对照（同一实例、同一预算、仅换初始解种子）：
    S1  DBSCAN 种子 + ALNS   （生产路径）
    S2  单点贪心种子 + ALNS   （更强的构造解）
    S3  单点贪心（不跑 ALNS）  （零迭代参照）
输出 S1/S2 的终值与「末次改进位置」，判断 ALNS 价值与初始解质量的关系。
"""
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from optimizer import ALNS, build_initial_solution, evaluate  # noqa: E402
from p1_alns_benefit import (  # noqa: E402
    baseline_of, make_haversine_dm, make_instance, singleton_vehicles,
)

MODES = ("cost", "fewest_vehicles")
SIZES = (20, 30, 45)
SEEDS = (1, 2, 3, 4, 5)


def alns_from(seed_vehicles, passengers, dest, dm, mode, budget):
    base = baseline_of(passengers, dm)
    a = ALNS(seed_vehicles, passengers, dest, dm, base, len(passengers), mode, budget,
             trace=True)
    best = a.run()
    return {"score": evaluate(best, base, len(passengers), mode)[0],
            "cost": round(sum(v.cost for v in best), 2),
            "dur": round(sum(v.duration_s for v in best) / 60, 1),
            "veh": len(best), "seed_veh": len(seed_vehicles),
            "last_t": a.trace[-1]["t"] if a.trace else 0.0,
            "n_improve": len(a.trace)}


def main():
    budget = 4.0
    print(f"预算 {budget}s / 实例   模式        种子车数→终车数   S1(DBSCAN种子)  "
          f"S2(贪心种子)   S3(纯贪心)   S1末次改进   S2末次改进")
    rows = []
    for n in SIZES:
        for s in SEEDS:
            ps, dest, pts = make_instance(n, s)
            dm = make_haversine_dm(pts)
            base = baseline_of(ps, dm)
            for mode in MODES:
                dbscan_seed = build_initial_solution(ps, dest, dm)
                greed_seed = singleton_vehicles(ps, dm)
                # S3：贪心插入一次（零 ALNS 迭代）
                tmp = ALNS([], ps, dest, dm, base, len(ps), mode, 0.0)
                tmp.repair_greedy(list(ps))
                s3 = evaluate(tmp.current, base, len(ps), mode)[0]
                s3_veh = len(tmp.current)
                r1 = alns_from(dbscan_seed, ps, dest, dm, mode, budget)
                r2 = alns_from(greed_seed, ps, dest, dm, mode, budget)
                rows.append({"n": n, "seed": s, "mode": mode,
                             "s1": r1["score"], "s2": r2["score"], "s3": s3,
                             "s1_veh": r1["veh"], "s2_veh": r2["veh"], "s3_veh": s3_veh,
                             "seed_veh": r1["seed_veh"],
                             "s1_last": r1["last_t"], "s2_last": r2["last_t"]})
                print(f"n={n:<3} s={s} {mode:<16} "
                      f"[{r1['seed_veh']}→{r1['veh']}]  {r1['score']:>12.2f}  "
                      f"{r2['score']:>11.2f}  {s3:>11.2f}   {r1['last_t']:>7.2f}s  "
                      f"{r2['last_t']:>7.2f}s")

    print("\n" + "=" * 96)
    print("汇总（每种模式下 15 个实例的均值）")
    print("=" * 96)
    print(f"{'模式':<18}{'S1(DBS+ALNS)':>14}{'S2(贪心+ALNS)':>15}{'S3(纯贪心)':>12}"
          f"{'S2/S1':>9}{'S3/S1':>9}{'种子车数':>10}{'S1终车':>9}{'S2终车':>9}")
    for mode in MODES:
        rs = [r for r in rows if r["mode"] == mode]
        s1 = statistics.mean(r["s1"] for r in rs)
        s2 = statistics.mean(r["s2"] for r in rs)
        s3 = statistics.mean(r["s3"] for r in rs)
        sv = statistics.mean(r["seed_veh"] for r in rs)
        print(f"{mode:<18}{s1:>14.3f}{s2:>15.3f}{s3:>12.3f}"
              f"{s2/s1*100:>8.1f}%{s3/s1*100:>8.1f}%{sv:>10.2f}"
              f"{statistics.mean(r['s1_veh'] for r in rs):>9.2f}"
              f"{statistics.mean(r['s2_veh'] for r in rs):>9.2f}")
        print(f"{'':<18}末次改进位置: S1={statistics.mean(r['s1_last'] for r in rs)/budget*100:.0f}%  "
              f"S2={statistics.mean(r['s2_last'] for r in rs)/budget*100:.0f}% 预算处")


if __name__ == "__main__":
    t = time.time()
    main()
    print(f"\n耗时 {time.time()-t:.1f}s")
