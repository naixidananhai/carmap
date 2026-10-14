# -*- coding: utf-8 -*-
"""
P1 改造后的 A/B 验证
====================
A/B-1 降温方式：按时间（新默认，T 在预算内衰减到 0.35·T0）vs 按迭代（旧行为 α=0.9995）
      —— 验证「长预算下旧调度把 T 压到 0、退化为纯爬山」的假设
A/B-2 fewest_vehicles：两阶段（先定车数→同车数下压费用）vs 单阶段（旧行为）
      —— 验证费用劣势是否被回补，车数是否保持不变
"""
import os
import statistics as st
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from optimizer import ALNS, build_initial_solution, evaluate  # noqa: E402
from p1_alns_benefit import baseline_of, make_haversine_dm, make_instance  # noqa: E402


def run(n, seed, mode, budget, **kw):
    ps, dest, pts = make_instance(n, seed)
    dm = make_haversine_dm(pts)
    base = baseline_of(ps, dm)
    veh = build_initial_solution(ps, dest, dm)
    a = ALNS(veh, ps, dest, dm, base, len(ps), mode, budget, **kw)
    best = a.run()
    return {"cost": round(sum(v.cost for v in best), 2), "veh": len(best),
            "dur": round(sum(v.duration_s for v in best) / 60, 1),
            "iters": a.stats["iters"], "T0": a.stats["T0"], "T_end": a.stats["T_end"],
            "init_veh": len(veh), "two_phase": a.stats.get("two_phase")}


print("=" * 100)
print("A/B-1 降温方式（mode=cost）")
print("=" * 100)
grid = [(20, 1), (20, 2), (20, 3), (30, 1), (30, 2), (30, 3), (45, 1), (45, 2), (45, 3)]
print(f"{'实例':<10}{'预算':>6}{'新:按时间(0.35T0)':>20}{'旧:按迭代(α.9995)':>20}{'差异':>10}")
rows1 = []
for n, s in grid:
    for b in (4.0,):
        a1 = run(n, s, "cost", b)                                    # 新默认
        a2 = run(n, s, "cost", b, cooling="iter")                     # 旧行为
        d = (a1["cost"] - a2["cost"]) / a2["cost"] * 100
        rows1.append((n, s, b, a1, a2))
        print(f"n{n}_s{s:<6}{b:>6.0f}{a1['cost']:>14.2f}元 {a1['veh']}车"
              f"{a2['cost']:>14.2f}元 {a2['veh']}车{d:>9.2f}%")
print(f"\n4s 均值: 新={st.mean(r[3]['cost'] for r in rows1):.2f}  "
      f"旧={st.mean(r[4]['cost'] for r in rows1):.2f}  "
      f"新相对旧 {st.mean([(r[3]['cost']-r[4]['cost'])/r[4]['cost']*100 for r in rows1]):+.2f}%")

print("\n长预算对比（60s，验证旧调度 T→0 的退化假设）:")
print(f"{'实例':<10}{'新:按时间':>22}{'旧:按迭代 T末/T0':>20}{'差异':>10}")
rows1b = []
for n, s in [(30, 1), (45, 1), (45, 2)]:
    a1 = run(n, s, "cost", 60.0)
    a2 = run(n, s, "cost", 60.0, cooling="iter")
    d = (a1["cost"] - a2["cost"]) / a2["cost"] * 100
    rows1b.append((a1, a2))
    print(f"n{n}_s{s:<6}{a1['cost']:>14.2f}元 {a1['veh']}车"
          f"{a2['cost']:>14.2f}元 {a2['veh']}车  T末/T0={a2['T_end']/a2['T0']:.2e}{d:>9.2f}%")
print(f"\n60s 均值: 新={st.mean(r[0]['cost'] for r in rows1b):.2f}  "
      f"旧={st.mean(r[1]['cost'] for r in rows1b):.2f}")

print("\n" + "=" * 100)
print("A/B-2 fewest_vehicles：两阶段 vs 单阶段（预算 4s）")
print("=" * 100)
print(f"{'实例':<10}{'两阶段 费用':>14}{'两阶段 车':>10}{'单阶段 费用':>14}{'单阶段 车':>10}"
      f"{'费用差异':>10}")
rows2 = []
for n, s in grid:
    a1 = run(n, s, "fewest_vehicles", 4.0)                          # 两阶段（新默认）
    a2 = run(n, s, "fewest_vehicles", 4.0, two_phase=False)          # 单阶段（旧行为）
    d = (a1["cost"] - a2["cost"]) / a2["cost"] * 100
    rows2.append((a1, a2))
    print(f"n{n}_s{s:<6}{a1['cost']:>14.2f}{a1['veh']:>10}{a2['cost']:>14.2f}"
          f"{a2['veh']:>10}{d:>9.2f}%")
c1, c2 = st.mean(r[0]["cost"] for r in rows2), st.mean(r[1]["cost"] for r in rows2)
v1, v2 = st.mean(r[0]["veh"] for r in rows2), st.mean(r[1]["veh"] for r in rows2)
print(f"\n均值: 两阶段 {c1:.2f}元/{v1:.2f}车    单阶段 {c2:.2f}元/{v2:.2f}车    "
      f"费用 {(c1-c2)/c2*100:+.2f}%")

print("\n" + "=" * 100)
print("A/B-3 车数上限硬约束 max_vehicles（mode=cost，预算 4s）")
print("=" * 100)
print(f"{'实例':<10}{'不限车':>12}{'上限=n/6':>12}{'上限=下限+1':>14}{'约束后车数':>12}{'约束后费用':>12}")
for n, s in [(20, 1), (30, 1), (45, 1)]:
    free = run(n, s, "cost", 4.0)
    lb = -(-n // 6)
    for cap in (lb, lb + 1):
        r = run(n, s, "cost", 4.0, max_vehicles=cap)
        ok = "OK" if r["veh"] <= cap else "违反!"
        print(f"n{n}_s{s:<6}{free['cost']:>9.2f}元{cap:>9}{r['cost']:>11.2f}元"
              f"{r['veh']:>9}({ok})")
