# -*- coding: utf-8 -*-
"""
P1 —— 验证 ALNS 的真实收益
============================
回答三个问题：
  Q1  ALNS 到底带来多少收益？收益来自「修好初始解」还是「精细搜索」？
  Q2  退火温度 T0 = best_score[0] * 0.1 是否失配（P1 待验证假设）？
  Q3  优化模式是否都创造了独立价值？（A3 决策依据——结论：只有 cost 有价值，
      balanced 已删除，fewest_vehicles 已改为两阶段；本脚本现对比 cost 与 fewest_vehicles）

对照组设计（同一实例、同一距离矩阵，只换求解器）：
  A  DBSCAN 初始解          —— build_initial_solution
  B  单点起车 + 贪心插入     —— 复用 ALNS.repair_greedy（=ALNS 的第 1 次迭代）
  C  A + ALNS（现状配置）    —— optimize 的默认路径
  D  A + ALNS（温度修正）    —— 变体实验

用法：
    python tests/p1_alns_benefit.py             # 全量（约 8~10 分钟）
    python tests/p1_alns_benefit.py --quick     # 快速冒烟（约 1 分钟）
    python tests/p1_alns_benefit.py --real      # 追加一条真实高德矩阵实例
"""
import argparse
import json
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from optimizer import (  # noqa: E402
    ALNS, CAR_CAPACITY, DistanceMatrix, Passenger, Vehicle,
    build_initial_solution, calc_cost, evaluate,
)

# ---------------------------------------------------------------- 实例构造
# 郑州 9 个出行片区（覆盖市区约 20km 范围）
AREAS = [
    (34.748, 113.625), (34.755, 113.665), (34.765, 113.700),
    (34.775, 113.740), (34.760, 113.780), (34.750, 113.820),
    (34.720, 113.680), (34.700, 113.720), (34.790, 113.600),
]
DEST = (34.7421, 113.7536)  # 郑州东站（活动点）


def make_instance(n, seed):
    """n 名乘客随机落在 9 个片区内（带 ±2.2km 抖动），返回 (passengers, dest, points)"""
    rnd = random.Random(seed)
    ps = []
    for i in range(n):
        a = rnd.choice(AREAS)
        ps.append(Passenger(
            id=f"P{i+1:03d}", name=f"乘客{i+1}",
            lat=round(a[0] + rnd.uniform(-0.02, 0.02), 6),
            lng=round(a[1] + rnd.uniform(-0.02, 0.02), 6),
        ))
    dest = Passenger(id="DEST", name="终点", lat=DEST[0], lng=DEST[1])
    points = [(p.lat, p.lng) for p in ps] + [DEST]
    return ps, dest, points


def make_haversine_dm(points):
    """离线距离矩阵（Haversine ×1.4 绕行系数），完全确定、零 API 成本"""
    return DistanceMatrix(points, baidu_matrix=None)


def make_real_dm(points):
    """真实高德矩阵（网络 + 配额），失败自动降级为 None"""
    from map_provider import create_provider
    provider, name = create_provider()
    if name != "amap":
        print(f"  [real] provider={name}，跳过真实矩阵")
        return None
    print(f"  [real] 调用高德构建 {len(points)}×{len(points)} 矩阵...")
    m = provider.build_matrix(points)
    if all(m[i][j] is None for i in range(len(points)) for j in range(len(points)) if i != j):
        print("  [real] 矩阵全空，跳过")
        return None
    holes = sum(1 for i in range(len(points)) for j in range(len(points))
                if i != j and m[i][j] is None)
    print(f"  [real] 矩阵就绪，空洞 {holes} 个")
    return m


# ---------------------------------------------------------------- 求解器封装
def baseline_of(passengers, dm):
    n = len(passengers)
    return sum(calc_cost(dm.get(i, n)[0]) for i in range(n))


def singleton_vehicles(passengers, dm):
    dest_idx = len(passengers)
    vs = []
    for i, p in enumerate(passengers):
        d, s = dm.route_distance([i], dest_idx)
        vs.append(Vehicle(passengers=[p], order=[p], distance_m=d,
                          duration_s=s, cost=calc_cost(d)))
    return vs


def _facts(vehicles):
    """与模式无关的三项物理指标"""
    return {"n_veh": len(vehicles),
            "cost": round(sum(v.cost for v in vehicles), 2),
            "dur_min": round(sum(v.duration_s for v in vehicles) / 60, 1)}


def solve_greedy(passengers, dest, dm, mode):
    """对照组 B：不做 DBSCAN，把每个乘客当独立一车，用生产代码的贪心插入合并一次"""
    base = baseline_of(passengers, dm)
    tmp = ALNS([], passengers, dest, dm, base, len(passengers), mode, 0.0)
    tmp.current = []
    tmp.repair_greedy(list(passengers))
    r = {"score": evaluate(tmp.current, base, len(passengers), mode), "base": base}
    r.update(_facts(tmp.current))
    return r


def solve_initial(passengers, dest, dm, mode):
    """对照组 A：DBSCAN + 容量拆分 + 2-opt，不跑 ALNS"""
    base = baseline_of(passengers, dm)
    veh = build_initial_solution(passengers, dest, dm)
    r = {"score": evaluate(veh, base, len(passengers), mode), "base": base}
    r.update(_facts(veh))
    return r


def solve_alns(passengers, dest, dm, mode, budget=4.0, t0_mult=None, t0_abs=None,
               T_end_ratio=None, cooling=None, max_vehicles=None, trace=False):
    """对照组 C/D：A 的初始解 + ALNS。t0_mult 以初始费用为基数缩放 T0"""
    base = baseline_of(passengers, dm)
    veh = build_initial_solution(passengers, dest, dm)
    init_score = evaluate(veh, base, len(passengers), mode, max_vehicles)
    T0 = None
    if t0_abs is not None:
        T0 = t0_abs
    elif t0_mult is not None:
        T0 = init_score[0] * t0_mult
    kw = {}
    if T_end_ratio is not None:
        kw["T_end_ratio"] = T_end_ratio
    if cooling is not None:
        kw["cooling"] = cooling
    alns = ALNS(veh, passengers, dest, dm, base, len(passengers), mode, budget,
                trace=trace, T0=T0, max_vehicles=max_vehicles, **kw)
    best = alns.run()
    r = {"init": init_score, "score": evaluate(best, base, len(passengers), mode, max_vehicles),
         "base": base, "stats": alns.stats, "trace": alns.trace or []}
    r.update(_facts(best))
    r["init_facts"] = _facts(veh)
    return r


# ---------------------------------------------------------------- 实验
MODES = ("cost", "fewest_vehicles")


def exp1_overview(instances, budget, log):
    """Q1：四组对照 × 两种模式"""
    rows = []
    for tag, passengers, dest, dm in instances:
        for mode in MODES:
            g = solve_greedy(passengers, dest, dm, mode)
            a = solve_initial(passengers, dest, dm, mode)
            c = solve_alns(passengers, dest, dm, mode, budget=budget, trace=True)
            rows.append({
                "instance": tag, "mode": mode,
                "greedy": g["score"][0], "greedy_veh": g["n_veh"], "greedy_cost": g["cost"],
                "init": a["score"][0], "init_veh": a["n_veh"], "init_cost": a["cost"],
                "alns": c["score"][0], "alns_veh": c["n_veh"],
                "alns_cost": c["cost"], "alns_dur_min": c["dur_min"],
                "iters": c["stats"]["iters"], "T0": c["stats"]["T0"],
                "T_end": c["stats"]["T_end"], "alpha": c["stats"]["alpha"],
                "n_improve": len(c["trace"]),
                "last_improve_t": c["trace"][-1]["t"] if c["trace"] else 0.0,
                "budget": budget,
            })
            log(f"    {tag:<12} {mode:<16} greedy={g['score'][0]:8.2f} "
                f"init={a['score'][0]:8.2f} alns={c['score'][0]:8.2f}")
    return rows


def exp2_temperature(instances, budget, mode, log):
    """Q2：T0 扫描（以初始费用 s0 为基数）+ 降温方式 A/B（按时间 vs 按迭代）"""
    variants = [("auto(默认0.1)", dict(t0_mult=None)),
                ("T0=0.5·s0", dict(t0_mult=0.5)),
                ("T0=0.05·s0", dict(t0_mult=0.05)),
                ("T0=0.01·s0", dict(t0_mult=0.01)),
                ("T0=0.002·s0", dict(t0_mult=0.002)),
                ("T0=0(纯爬山)", dict(t0_abs=0.0))]
    cooling = [("冷却=时间 T末=0.35T0(新默认)", dict(T_end_ratio=0.35)),
               ("冷却=时间 T末=0.10T0", dict(T_end_ratio=0.10)),
               ("冷却=时间 T末=0.01T0", dict(T_end_ratio=0.01)),
               ("冷却=迭代 α=0.9995(旧行为)",
                dict(cooling="iter", T_end_ratio=None))]
    out = {"mode": mode, "budget": budget, "variants": [], "cooling": []}
    for label, kw in variants:
        vals, vehs = [], []
        for tag, passengers, dest, dm in instances:
            r = solve_alns(passengers, dest, dm, mode, budget=budget, **kw)
            vals.append(r["score"][0])
            vehs.append(r["n_veh"])
        rec = {"label": label, "mean": round(statistics.mean(vals), 3),
               "median": round(statistics.median(vals), 3),
               "mean_veh": round(statistics.mean(vehs), 2), "raw": vals}
        out["variants"].append(rec)
        log(f"    {label:<28} mean_score={rec['mean']:9.3f}  mean_veh={rec['mean_veh']:.2f}")
    log("    " + "-" * 66)
    for label, kw in cooling:
        if kw.get("cooling") == "iter":
            kw = {"cooling": "iter"}
        vals, vehs = [], []
        for tag, passengers, dest, dm in instances:
            r = solve_alns(passengers, dest, dm, mode, budget=budget, **kw)
            vals.append(r["score"][0])
            vehs.append(r["n_veh"])
        rec = {"label": label, "mean": round(statistics.mean(vals), 3),
               "median": round(statistics.median(vals), 3),
               "mean_veh": round(statistics.mean(vehs), 2), "raw": vals}
        out["cooling"].append(rec)
        log(f"    {label:<28} mean_score={rec['mean']:9.3f}  mean_veh={rec['mean_veh']:.2f}")
    return out


def exp3_budget(instances, mode, budgets, log):
    """Q1 补充：时间预算是否仍然绑定（收益是否已饱和）"""
    out = []
    for b in budgets:
        vals, iters, last_t = [], [], []
        for tag, passengers, dest, dm in instances:
            r = solve_alns(passengers, dest, dm, mode, budget=b, trace=True)
            vals.append(r["score"][0])
            iters.append(r["stats"]["iters"])
            last_t.append(r["trace"][-1]["t"] if r["trace"] else 0.0)
        rec = {"budget": b, "mean": round(statistics.mean(vals), 3),
               "mean_iters": round(statistics.mean(iters), 0),
               "last_improve_ratio": round(statistics.mean(
                   [t / b for t in last_t]), 3), "raw": vals}
        out.append(rec)
        log(f"    budget={b:>4.0f}s  mean_score={rec['mean']:9.3f}  "
            f"iters≈{rec['mean_iters']:.0f}  末次改进位置={rec['last_improve_ratio']*100:.0f}% 预算处")
    return out


# ---------------------------------------------------------------- 汇总
def summarize(rows, log):
    log("\n" + "=" * 108)
    log("【Q1】ALNS 真实收益（越小越好）")
    log("=" * 108)
    log(f"{'模式':<18}{'贪心单点':>10}{'DBSCAN初始':>12}{'+ALNS':>10}"
        f"{'车数(贪/初/终)':>18}{'ALNS vs 初始':>14}{'ALNS vs 贪心':>14}")
    by = {}
    for r in rows:
        by.setdefault(r["mode"], []).append(r)
    summary = {}
    for mode, rs in by.items():
        g = statistics.mean(r["greedy"] for r in rs)
        i = statistics.mean(r["init"] for r in rs)
        a = statistics.mean(r["alns"] for r in rs)
        vg = statistics.mean(r["greedy_veh"] for r in rs)
        vi = statistics.mean(r["init_veh"] for r in rs)
        va = statistics.mean(r["alns_veh"] for r in rs)
        veh = f"{vg:.1f}/{vi:.1f}/{va:.1f}"
        log(f"{mode:<18}{g:>10.2f}{i:>12.2f}{a:>10.2f}{veh:>18}"
            f"{f'{(i-a)/i*100:+.1f}%':>14}{f'{(g-a)/g*100:+.1f}%':>14}")
        summary[mode] = {"greedy": round(g, 2), "init": round(i, 2), "alns": round(a, 2),
                         "greedy_veh": round(vg, 2), "init_veh": round(vi, 2),
                         "alns_veh": round(va, 2)}
    # 明细
    log("\n明细：")
    log(f"{'实例':<14}{'模式':<18}{'贪心':>9}{'初始':>9}{'+ALNS':>9}{'改进':>8}{'车数':>8}"
        f"{'T0':>10}{'T末':>10}{'改进次数':>9}{'末次改进s':>10}")
    for r in rows:
        imp = (r["init"] - r["alns"]) / r["init"] * 100 if r["init"] else 0
        veh_str = "{}→{}".format(r["init_veh"], r["alns_veh"])
        log(f"{r['instance']:<14}{r['mode']:<18}{r['greedy']:>9.2f}{r['init']:>9.2f}"
            f"{r['alns']:>9.2f}{imp:>7.1f}%{veh_str:>8}"
            f"{r['T0']:>10.3f}{r['T_end']:>10.2e}{r['n_improve']:>9}{r['last_improve_t']:>10.2f}")
    regress = [r for r in rows if r["alns"] > r["init"] + 1e-9]
    log(f"\n退化检查：ALNS 结果劣于初始解的实例数 = {len(regress)} / {len(rows)}")
    log(f"末次改进位置（占预算比例）均值 = "
        f"{statistics.mean(r['last_improve_t'] / r['budget'] for r in rows) * 100:.0f}%")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--budget", type=float, default=4.0)
    ap.add_argument("--seeds", type=int, default=6)
    args = ap.parse_args()

    lines = []

    def log(msg):
        print(msg, flush=True)
        lines.append(msg)

    seeds = range(1, (2 if args.quick else args.seeds) + 1)
    sizes = [20] if args.quick else [20, 30, 45]
    budget = 1.0 if args.quick else args.budget

    instances = []
    for n in sizes:
        for s in seeds:
            ps, dest, pts = make_instance(n, s)
            instances.append((f"n{n}_s{s}", ps, dest, make_haversine_dm(pts)))

    log(f"实例数 = {len(instances)}（合成 / Haversine 矩阵），ALNS 预算 = {budget}s")
    t_start = time.time()

    log("\n" + "=" * 100)
    log("阶段 1/4：四组对照 × 三种模式")
    log("=" * 100)
    rows = exp1_overview(instances, budget, log)
    summary = summarize(rows, log)

    log("\n" + "=" * 100)
    log("阶段 2/4：退火温度扫描（mode=cost，取前 6 个实例）")
    log("=" * 100)
    sub = instances[:6]
    temps = exp2_temperature(sub, min(budget, 3.0), "cost", log)

    log("\n" + "=" * 100)
    log("阶段 3/4：时间预算扫描（mode=cost，取前 6 个实例）")
    log("=" * 100)
    budgets = [1.0, 3.0] if args.quick else [1.0, 3.0, 10.0, 20.0]
    budgets_exp = exp3_budget(sub, "cost", budgets, log)

    log("\n" + "=" * 100)
    log("阶段 4/4：三模式是否产生差异化结果（结构对比）")
    log("=" * 100)
    struct = {}
    for tag, passengers, dest, dm in instances[:3]:
        struct[tag] = {}
        for mode in MODES:
            r = solve_alns(passengers, dest, dm, mode, budget=budget)
            struct[tag][mode] = {"cost": r["cost"], "duration_min": r["dur_min"],
                                 "vehicles": r["n_veh"]}
        log(f"    {tag}: " + " | ".join(
            f"{m}→{v['cost']}元/{v['vehicles']}车/{v['duration_min']}min"
            for m, v in struct[tag].items()))

    # ---- Q1 真实矩阵（可选）
    real = None
    if args.real:
        log("\n" + "=" * 100)
        log("附加：真实高德矩阵实例")
        log("=" * 100)
        ps, dest, pts = make_instance(24, 7)
        m = make_real_dm(pts)
        if m is not None:
            dm = DistanceMatrix(pts, baidu_matrix=m)
            real = {"cost": {}, "matrix_holes": sum(
                1 for i in range(len(pts)) for j in range(len(pts)) if i != j and m[i][j] is None)}
            for mode in MODES:
                g = solve_greedy(ps, dest, dm, mode)
                a = solve_initial(ps, dest, dm, mode)
                c = solve_alns(ps, dest, dm, mode, budget=max(budget, 10.0), trace=True)
                real[mode] = {"greedy": round(g["score"][0], 2),
                              "init": round(a["score"][0], 2),
                              "init_veh": a["n_veh"],
                              "alns": round(c["score"][0], 2),
                              "alns_veh": c["n_veh"],
                              "alns_cost": c["cost"],
                              "alns_dur_min": c["dur_min"],
                              "n_improve": len(c["trace"]),
                              "last_improve_t": c["trace"][-1]["t"] if c["trace"] else 0.0}
                log(f"    {mode:<16} greedy={g['score'][0]:9.2f} init={a['score'][0]:9.2f} "
                    f"({a['n_veh']}车) alns={c['score'][0]:9.2f} ({c['n_veh']}车)")

    out = {"meta": {"instances": len(instances), "budget": budget, "seeds": list(seeds),
                    "sizes": sizes, "elapsed_s": round(time.time() - t_start, 1)},
           "exp1_rows": rows, "exp1_summary": summary,
           "exp2_temperature": temps, "exp3_budget": budgets_exp,
           "exp4_modes": struct, "real": real}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "p1_results.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(os.path.join(os.path.dirname(path), "p1_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(f"\n结果已写入 {path}")
    log(f"总耗时 {out['meta']['elapsed_s']}s")


if __name__ == "__main__":
    main()
