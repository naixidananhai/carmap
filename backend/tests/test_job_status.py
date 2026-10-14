# -*- coding: utf-8 -*-
"""
A8 单元测试 —— 八态作业状态机 + 费用下界 + 硬约束
==================================================
覆盖：

  1. lower_bound_cost 的**有效性**（这是 gap 与 OPTIMAL 成立的前提）：
     随机实例上「最优解费用 ≥ 下界」必须恒成立——下界宁松不可错。
  2. 下界的解析性质：n=1 时等于单人直达费用；下界随距离单调不减；
     下界不超过「各自单独叫车」的基线。
  3. 终止原因（四种终态）：
     STOP_TIMEOUT（跑满预算）/ STOP_STABLE（连续无改善提前停）
     STOP_GAP（把阈值放宽使其可达 → 证明代码路径真的通）
     OPTIMAL（把下界顶到最优费用上 → 证明 gap=0 时确实报最优）
  4. FAILED 的 sub_status 判定：
     PROVEN_INFEASIBLE（存在乘客直达就超 5400 秒 → 数学上不可行）
     SEARCH_FAILED（下界顶不到但解不可行 → 只是没搜到）
  5. 单车 5400 秒硬约束生效：构造「任意两两拼车都超时」的矩阵，
     断言最终解每辆车都不超时（修复前会返回超时路线却报成功）。
  6. 状态事件序列 INIT → FEASIBLE → OPTIMIZING → 终态。

全部离线（Haversine 或手工矩阵），零 API 成本。
"""
import math
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from optimizer import (BASE_FEE, CAR_CAPACITY, FREE_KM, MAX_ROUTE_SECONDS, PER_KM_FEE,
                       DistanceMatrix, Passenger, calc_cost, gap_ratio, lower_bound_cost,
                       optimize, solution_feasible, stable_window, vehicle_feasible)

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


DEST = Passenger(id="DEST", name="终点", lat=34.7421, lng=113.7536)


def make(n, seed, spread=0.06, scale=1.0):
    rnd = random.Random(seed)
    ps = [Passenger(id=f"P{i+1:03d}", name=f"乘客{i+1}",
                    lat=DEST.lat + rnd.uniform(-spread, spread),
                    lng=DEST.lng + rnd.uniform(-spread, spread)) for i in range(n)]
    pts = [(p.lat, p.lng) for p in ps] + [(DEST.lat, DEST.lng)]
    dm = DistanceMatrix(pts, baidu_matrix=None)
    if scale != 1.0:
        n_ = len(pts)
        m = [[(dm.get(i, j)[0] * scale, dm.get(i, j)[1] * scale) for j in range(n_)]
             for i in range(n_)]
        dm = DistanceMatrix(pts, baidu_matrix=m)
    return ps, dm


def run(ps, dm, budget=2.0, mode="cost", mkv=None, **kw):
    diag = {}
    events = []
    best, base = optimize(ps, DEST, dm, mode=mode, time_budget_s=budget, diag=diag,
                          max_vehicles=mkv, job_cb=events.append, **kw)
    return best, base, diag, events


def main():
    # ---- 1. 下界有效性 ----
    print("=== 1. 费用下界的有效性（gap 与 OPTIMAL 的前提）===")
    viol = 0
    rows = []
    for seed in range(1, 13):
        n = [1, 2, 3, 5, 6, 7, 12, 15][seed % 8]
        ps, dm = make(n, seed)
        direct_m = [dm.get(i, n)[0] for i in range(n)]
        lb = lower_bound_cost(direct_m)
        best, base, diag, _ = run(ps, dm, budget=1.5)
        cost = sum(v.cost for v in best)
        if cost < lb - 1e-6:
            viol += 1
        rows.append((n, lb, cost, diag["job"]["gap"]))
    check("随机实例上「最优费用 ≥ 下界」恒成立", viol == 0, f"违反 {viol} 次")
    for n, lb, cost, g in rows[:6]:
        print(f"       n={n:2d} 下界 {lb:7.2f} ≤ 解 {cost:7.2f}  gap {g * 100:5.1f}%")
    check("gap 全部非负", all(g is not None and g >= 0 for _, _, _, g in rows))

    # 解析性质
    ps, dm = make(3, 42)
    direct = [dm.get(i, 3) for i in range(3)]
    lb1 = lower_bound_cost([direct[0][0]])
    check("n=1 时下界 = 单人直达费用（该情形下下界就是精确值）",
          abs(lb1 - calc_cost(direct[0][0])) < 1e-9, f'{lb1:.2f} vs {calc_cost(direct[0][0]):.2f}')
    lb_a = lower_bound_cost([5000.0, 5000.0])
    lb_b = lower_bound_cost([9000.0, 9000.0])
    check("下界随距离单调不减", lb_b >= lb_a, f"{lb_a:.2f} → {lb_b:.2f}")
    lb0 = lower_bound_cost([])
    check("空名单下界为 0", lb0 == 0.0)
    # 容量下限：6 人 → k_min=1
    lb6 = lower_bound_cost([1000.0] * 6)
    lb7 = lower_bound_cost([1000.0] * 7)
    check("跨容量台阶时下界跳到 2 辆车（≥ 2×起步价）", lb7 >= 2 * BASE_FEE > lb6,
          f"6人 {lb6:.2f} / 7人 {lb7:.2f}")
    check("下界恒 ≤ 单独叫车基线（否则下界无效）",
          all(lb <= sum(calc_cost(d) for d in [1000.0] * 6) for lb in (lb6,))
          and lb6 <= 6 * BASE_FEE + PER_KM_FEE * max(1.0 - FREE_KM, 0) * 6)

    # ---- 2. 四种终态 ----
    print("\n=== 2. 终止原因（8 态里的 5 个终态）===")
    ps, dm = make(15, 7)
    best, base, diag, _ = run(ps, dm, budget=3.0, stable_s=float("inf"), gap_stop_pct=-1)
    j = diag["job"]
    check("关掉提前终止 → STOP_TIMEOUT", j["status"] == "STOP_TIMEOUT", j["status"])
    check("STOP_TIMEOUT 的说明含「预算用尽」", "预算用尽" in j["stop_reason"], j["stop_reason"])
    check("STOP_TIMEOUT 实际跑满预算", j["elapsed"] >= 3.0,
          f'{j["elapsed"]:.2f}s / 3.0s')

    # 同实例同随机序列：默认提前终止应更早结束
    # 注意预算必须大于 stable 窗口（窗口 = clamp(0.2×预算, 3s, 60s)，3s 预算下窗口也是 3s，
    # 此时「跑满」与「停滞」同时到达，只会走 STOP_TIMEOUT 分支）
    random.seed(12345)
    b2, _, d2, _ = run(ps, dm, budget=6.0)
    j2 = d2["job"]
    check("默认配置触发 STOP_STABLE（无改善即停）", j2["status"] == "STOP_STABLE", j2["status"])
    check("提前终止确实省时间", j2["elapsed"] < 6.0,
          f'{j2["elapsed"]:.2f}s < 6.0s')
    check("STOP_STABLE 说明含窗口秒数", f'{stable_window(6.0):g} 秒' in j2["stop_reason"],
          j2["stop_reason"])
    check("提前终止不损失可行性", solution_feasible(b2))
    check("提前终止的解不差于初始解", sum(v.cost for v in b2) <= d2["initial_score"][0],
          f'{sum(v.cost for v in b2):.2f} ≤ {d2["initial_score"][0]:.2f}')

    # STOP_GAP：把阈值放到 1000%，任何解都满足 → 证明确有该分支
    ps3, dm3 = make(12, 3)
    _, _, d3, _ = run(ps3, dm3, budget=3.0, gap_stop_pct=10.0, stable_s=float("inf"))
    j3 = d3["job"]
    check("gap 阈值可达时报 STOP_GAP（分支真实存在）", j3["status"] == "STOP_GAP",
          f'{j3["status"]} gap={j3["gap"]}')
    check("STOP_GAP 说明含实际生效阈值（不会写成默认 5%）",
          "1000%" in j3["stop_reason"], j3["stop_reason"])

    # OPTIMAL：把下界顶到最优费用之上 → gap=0 → 应报 OPTIMAL。
    # 真实的合法下界不可能超过最优解，这里是为了让 gap≤1e-9 这条分支可达而人为构造。
    import optimizer as opt
    ps4, dm4 = make(6, 11)
    _, _, d_probe, _ = run(ps4, dm4, budget=1.5, stable_s=float("inf"), gap_stop_pct=-1)
    best_cost = d_probe["job"]["best_cost"]
    orig_lb = opt.lower_bound_cost
    opt.lower_bound_cost = lambda direct_m, capacity=CAR_CAPACITY: best_cost * 1.2
    try:
        _, _, d4, _ = run(ps4, dm4, budget=1.5, stable_s=float("inf"))
    finally:
        opt.lower_bound_cost = orig_lb
    j4 = d4["job"]
    check("gap=0 时报 OPTIMAL（已证明最优）", j4["status"] == "OPTIMAL",
          f'{j4["status"]} gap={j4["gap"]}')
    check("OPTIMAL 说明含「全局最优」", "全局最优" in j4["stop_reason"], j4["stop_reason"])

    # ---- 3. FAILED 细分 ----
    print("\n=== 3. FAILED 的 sub_status 判定 ===")
    # PROVEN_INFEASIBLE：把矩阵整体放大到「有人单人直达就超 5400 秒」
    # （只要有一个人怎么走都超时，整个实例就数学上不可行）
    ps5, dm5 = make(3, 5, scale=12.0)
    n5 = len(ps5)
    direct5 = [dm5.get(i, n5) for i in range(n5)]
    assert any(s > MAX_ROUTE_SECONDS for _, s in direct5), [s for _, s in direct5]
    best5, _, d5, _ = run(ps5, dm5, budget=1.0)
    j5 = d5["job"]
    check("存在乘客直达即超时 → FAILED", j5["status"] == "FAILED", j5["status"])
    check("sub_status = PROVEN_INFEASIBLE（数学上不可行）",
          j5["sub_status"] == "PROVEN_INFEASIBLE", str(j5["sub_status"]))
    check("已在 diag 里给出最差路线与上限", (d5.get("infeasible") or {}).get("limit_s")
          == MAX_ROUTE_SECONDS, str(d5.get("infeasible")))

    # SEARCH_FAILED：解不可行，但没人「单人直达就超时」→ 只是没搜到
    ps6, dm6 = make(6, 9, scale=1.0)
    orig_feas = opt.solution_feasible
    # 桩函数签名必须与真实 solution_feasible(vehicles, capacity=CAR_CAPACITY) 一致，
    # 否则 optimize 内部按 (vehicles, cap) 调用会 TypeError（A2 引入 capacity 形参）
    opt.solution_feasible = lambda vehicles, capacity=CAR_CAPACITY: False   # 强制判不可行，验证分类逻辑
    try:
        _, _, d6, _ = run(ps6, dm6, budget=1.0)
    finally:
        opt.solution_feasible = orig_feas
    j6 = d6["job"]
    check("解不可行但直达均可行 → sub_status = SEARCH_FAILED",
          j6["status"] == "FAILED" and j6["sub_status"] == "SEARCH_FAILED",
          f'{j6["status"]}/{j6["sub_status"]}')

    # ---- 4. 单车 5400 秒硬约束真的生效 ----
    print("\n=== 4. 单车 90 分钟硬约束 ===")
    # 构造：每人直达 3000 秒（合规），但任意两人同车 ≈ 6000 秒（超时）
    rnd = random.Random(77)
    n = 5
    pts = [(34.7421 + rnd.uniform(-0.05, 0.05), 113.7536 + rnd.uniform(-0.05, 0.05))
           for _ in range(n)] + [(DEST.lat, DEST.lng)]
    base_dm = DistanceMatrix(pts, baidu_matrix=None)
    M = [[None] * (n + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        M[i][i] = (0.0, 0.0)
    for i in range(n):
        M[i][n] = M[n][i] = (20000.0, 3000.0)      # 直达 3000 秒（合规）
    for i in range(n):
        for j in range(n):
            if i != j:
                M[i][j] = (45000.0, 6000.0)        # 两人同车必然超时
    dm7 = DistanceMatrix(pts, baidu_matrix=M)
    ps7 = [Passenger(id=f"P{i+1:03d}", name=f"乘客{i+1}",
                     lat=pts[i][0], lng=pts[i][1]) for i in range(n)]
    best7, _, d7, _ = run(ps7, dm7, budget=2.0)
    worst = max(v.duration_s for v in best7)
    check("拼车必然超时的实例：最终解仍满足单车 ≤ 5400 秒",
          worst <= MAX_ROUTE_SECONDS, f"最长 {worst:.0f}s / {MAX_ROUTE_SECONDS}s")
    check("因此必然一人一车（5 人 5 车）", len(best7) == 5, f"{len(best7)} 辆")
    check("整体判定为可行（无失真交付）", d7["job"]["status"] != "FAILED",
          d7["job"]["status"])

    # 反向对照：不放大时应该能拼车（≤ 6 人一车），证明上面不是被容量卡住
    ps8, dm8 = make(5, 77)
    best8, _, d8, _ = run(ps8, dm8, budget=1.5)
    check("普通实例仍会拼车（对照组）", len(best8) < 5, f"{len(best8)} 辆")

    # ---- 5. 状态事件序列 ----
    print("\n=== 5. 状态事件序列 ===")
    ps9, dm9 = make(9, 21)
    _, _, _, ev = run(ps9, dm9, budget=2.0)
    kinds = [e["kind"] for e in ev]
    check("事件序列以 init 开头", kinds[:1] == ["init"], str(kinds[:4]))
    check("顺序为 init → feasible → optimizing → progress* → finished",
          kinds[0] == "init" and kinds[1] == "feasible" and kinds[2] == "optimizing"
          and kinds[-1] == "finished" and "progress" in kinds, str(kinds[:4] + kinds[-1:]))
    fin = ev[-1]
    check("finished 事件带齐 8 态所需字段",
          all(k in fin for k in ("status", "sub_status", "gap", "lower_bound",
                                 "best_cost", "iteration", "elapsed", "stop_reason")),
          str(sorted(fin.keys())))
    check("finished.status 属于 8 态枚举",
          fin["status"] in ("STOP_GAP", "STOP_TIMEOUT", "STOP_STABLE", "OPTIMAL", "FAILED"),
          fin["status"])
    prog = [e for e in ev if e["kind"] == "progress"]
    check("进度事件的 best_cost 是真实费用（不含 1e6 罚分）",
          all(p["best_cost"] is None or p["best_cost"] < 1e5 for p in prog),
          f'{len(prog)} 条 progress')

    # ---- 6. fewest_vehicles 不做虚假最优声明 ----
    print("\n=== 6. fewest_vehicles 模式 ===")
    ps10, dm10 = make(12, 33)
    _, _, d10, _ = run(ps10, dm10, budget=2.0, mode="fewest_vehicles")
    j10 = d10["job"]
    check("车数目标不报 OPTIMAL / STOP_GAP（无有效下界，不做虚假承诺）",
          j10["status"] in ("STOP_TIMEOUT", "STOP_STABLE"), j10["status"])
    check("两阶段求解仍满足可行性", solution_feasible(
        run(ps10, dm10, budget=2.0, mode="fewest_vehicles")[0]))

    print("\n" + "=" * 62)
    print(f"结果：{len(PASS)}/{len(PASS) + len(FAIL)} 项通过")
    if FAIL:
        print("失败项：" + "，".join(FAIL))
    print("=" * 62)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
