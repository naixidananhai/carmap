# -*- coding: utf-8 -*-
"""
P1 改造的最终验证（带随机重复，消除单次运行噪声）
==================================================
对 9 个标准聚类实例 × 3 个随机种子（共 27 次运行/配置）比较：
  A cost（费用最优）
  B fewest_vehicles 两阶段（新）
  C fewest_vehicles 单阶段（旧行为）
  D cost + max_vehicles 上限（验证硬约束是否被严格遵守）
输出：费用均值、车数均值、相对差异、约束违反次数。
"""
import os
import statistics as st
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import random  # noqa: E402
from optimizer import ALNS, build_initial_solution  # noqa: E402
from p1_alns_benefit import baseline_of, make_haversine_dm, make_instance  # noqa: E402

GRID = [(20, 1), (20, 2), (20, 3), (30, 1), (30, 2), (30, 3), (45, 1), (45, 2), (45, 3)]
RNG_SEEDS = (0, 1, 2)
BUDGET = 4.0


def once(n, s, mode, rng, **kw):
    ps, dest, pts = make_instance(n, s)
    dm = make_haversine_dm(pts)
    base = baseline_of(ps, dm)
    veh = build_initial_solution(ps, dest, dm)
    random.seed(rng)
    a = ALNS(veh, ps, dest, dm, base, len(ps), mode, BUDGET, **kw)
    best = a.run()
    return (sum(v.cost for v in best), len(best), a.stats["iters"])


def collect(label, fn):
    costs, vehs, iters, viol = [], [], [], 0
    for n, s in GRID:
        for rng in RNG_SEEDS:
            c, v, it = fn(n, s, rng)
            costs.append(c); vehs.append(v); iters.append(it)
            if label == "D" and v > (-(-n // 6)):
                viol += 1
    return {"label": label, "cost": st.mean(costs), "veh": st.mean(vehs),
            "iters": st.mean(iters), "viol": viol, "costs": costs, "vehs": vehs}


def main():
    t0 = time.time()
    res = {}
    res["A"] = collect("A", lambda n, s, r: once(n, s, "cost", r))
    print(f"A cost                       费用 {res['A']['cost']:8.2f}  车 {res['A']['veh']:.2f}  "
          f"迭代 {res['A']['iters']:.0f}", flush=True)
    res["B"] = collect("B", lambda n, s, r: once(n, s, "fewest_vehicles", r))
    print(f"B fewest 两阶段(新)          费用 {res['B']['cost']:8.2f}  车 {res['B']['veh']:.2f}  "
          f"迭代 {res['B']['iters']:.0f}", flush=True)
    res["C"] = collect("C", lambda n, s, r: once(n, s, "fewest_vehicles", r, two_phase=False))
    print(f"C fewest 单阶段(旧)          费用 {res['C']['cost']:8.2f}  车 {res['C']['veh']:.2f}  "
          f"迭代 {res['C']['iters']:.0f}", flush=True)
    res["D"] = {"viol": 0}
    # D 用一个真正会绑定的上限：几何高度分散的实例
    print("\nD 车数上限约束（分散实例，上限=容量下限）")
    viol = 0
    pairs = []
    for n in (24, 30, 36):
        for span in (0.05, 0.12):
            ps, dest, pts = _spread(n, 1, span)
            dm = make_haversine_dm(pts)
            base = baseline_of(ps, dm)
            lb = -(-n // 6)
            for cap in (lb, lb + 1, None):
                random.seed(0)
                a = ALNS(build_initial_solution(ps, dest, dm), ps, dest, dm, base, n,
                         "cost", BUDGET, max_vehicles=cap)
                best = a.run()
                if cap is not None and len(best) > cap:
                    viol += 1
                pairs.append({"n": n, "span": span, "cap": cap, "veh": len(best),
                              "cost": round(sum(v.cost for v in best), 2)})
    print(f"  约束违反次数 = {viol} / 18（应为 0）", flush=True)
    for p in pairs:
        print(f"    n={p['n']} span={p['span']} cap={str(p['cap']):>4} -> "
              f"{p['veh']}车 {p['cost']:.2f}元", flush=True)

    print("\n" + "=" * 88)
    print("汇总（27 次运行均值）")
    print("=" * 88)
    print(f"  B fewest两阶段 相对 A cost:  {(res['B']['cost']-res['A']['cost'])/res['A']['cost']*100:+.2f}%"
          f"   车数 {res['A']['veh']:.2f} -> {res['B']['veh']:.2f}")
    print(f"  C fewest单阶段 相对 A cost:  {(res['C']['cost']-res['A']['cost'])/res['A']['cost']*100:+.2f}%")
    print(f"  B 相对 C（两阶段收益）:      {(res['B']['cost']-res['C']['cost'])/res['C']['cost']*100:+.2f}%")
    print(f"  改造前 fewest 相对 cost:     +10.59%（P1 基线）")
    print(f"  约束违反: {viol} / 18")
    print(f"\n耗时 {time.time()-t0:.0f}s")


def _spread(n, seed, span):
    from optimizer import Passenger
    rnd = random.Random(seed)
    ps = [Passenger(id=f"P{i+1:03d}", name=f"乘客{i+1}",
                    lat=34.75 + rnd.uniform(-span, span),
                    lng=113.70 + rnd.uniform(-span * 1.2, span * 1.2)) for i in range(n)]
    dest = Passenger(id="DEST", name="终点", lat=34.7421, lng=113.7536)
    return ps, dest, [(p.lat, p.lng) for p in ps] + [(dest.lat, dest.lng)]


if __name__ == "__main__":
    main()
