# -*- coding: utf-8 -*-
"""
A9 单元测试 —— 人均车费上限与「建议剔除」
==========================================
业务口径（业务方确认）：
  · 人均车费 = 拼车总费用 / 参与人数，**乘客实付**，无平台补贴；
  · 被剔除者**不再参加活动**（既不产生成本，也不计入分母）；
  · 算法**只给建议、绝不自动剔除**——必须人工确认后带 exclude_names 重算。

覆盖：

  1. per_capita_cost：定义（含 0 人 / 负数边界）。
  2. subset_matrix：裁出「子集 + 终点」的小方阵，真实矩阵数据不丢、终点恒在末位。
  3. 候选生成：距离前缀 + 整车剔除，去重后数量受 max_candidates 约束。
  4. **剔人非单调**（核心事实，用真实实例锁死）：剔掉最远的那个人，总费用反而上升。
     这是「不得只按距离剔人」的直接证据，也是 PER_CAPITA_NEAR_PCT 存在的理由。
  5. 达标场景：cap 放宽后 achievable=True、best.meets_cap=True，
     且达标候选中**保留人数最多**（不是人均最低）。
  6. 不可达场景：cap 极低时 achievable=False、note 明确写「无法」，
     且 best 的人均落在「最优值 × (1+PER_CAPITA_NEAR_PCT)」带宽内
     —— 防止推荐「剔到只剩 2 人」这类不可执行方案。
  7. 建议名单语义自洽：drop_count + keep_count == n；drop_names 里的人确实不在保留集里。
  8. 真的做了真实求解：verify_top 个候选各调用一次 optimize（计数器桩）。
  9. 确定性：同输入两次调用结果完全一致；且**不污染全局 RNG 状态**。
 10. 边界：cap=None → None；人数 ≤ 最低成团 → None；候选为空 → 不崩。
 11. SQLite 往返：per_capita_cap 落库，人均由「总费用 / route_passenger 行数」恢复。

全部离线（Haversine），零 API 成本。
"""
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))
sys.path.insert(0, _HERE)

import optimizer as opt
from optimizer import (CAR_CAPACITY, DistanceMatrix, Passenger,
                       build_initial_solution, calc_cost, optimize,
                       per_capita_cost, subset_matrix, suggest_drops,
                       PER_CAPITA_MIN_SERVED, PER_CAPITA_NEAR_PCT)

from p1_alns_benefit import make_instance, make_haversine_dm  # noqa: E402

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


DEST = Passenger(id="DEST", name="终点", lat=34.7421, lng=113.7536)


def solve_all(ps, dest, budget=2.0, seed=42):
    dm = make_haversine_dm([(p.lat, p.lng) for p in ps] + [(dest.lat, dest.lng)])
    random.seed(seed)
    diag = {}
    best, _ = optimize(ps, dest, dm, mode="cost", time_budget_s=budget, diag=diag)
    return best, sum(v.cost for v in best), dm


# ---------------------------------------------------------------- 1
def t1_definition():
    print("\n=== 1. per_capita_cost 定义 ===")
    check("200 元 / 20 人 = 10.0", per_capita_cost(200.0, 20) == 10.0)
    check("除不尽保留精度", abs(per_capita_cost(100.0, 3) - 33.333333) < 1e-4)
    check("0 人 → None（不抛 ZeroDivisionError）", per_capita_cost(100.0, 0) is None)
    check("负数人 → None", per_capita_cost(100.0, -1) is None)
    check("0 元 / 5 人 = 0.0", per_capita_cost(0.0, 5) == 0.0)


# ---------------------------------------------------------------- 2
def t2_subset_matrix():
    print("\n=== 2. subset_matrix 裁剪 ===")
    ps, dest, _ = make_instance(6, 3)
    dm = make_haversine_dm([(p.lat, p.lng) for p in ps] + [(dest.lat, dest.lng)])
    keep = [0, 2, 4]
    sub = subset_matrix(dm, keep, dest_idx=6)
    check("尺寸 = 保留数 + 1", sub.n == 4, f"n={sub.n}")
    check("终点在子矩阵末位（坐标与原点集一致）",
          sub.points[3] == dm.points[6],
          f"{sub.points[3]} vs {dm.points[6]}")
    check("保留点坐标逐位一致",
          [sub.points[i] for i in range(3)] == [dm.points[i] for i in keep])
    # 关键：真实矩阵数据必须被裁出来，而不是退化成 Haversine
    raw = [[dm.get(i, j) for j in range(7)] for i in range(7)]
    dm2 = DistanceMatrix(dm.points, baidu_matrix=raw)
    sub2 = subset_matrix(dm2, keep, dest_idx=6)
    check("provider 真实矩阵被裁出（非 None）", sub2.baidu_matrix is not None)
    check("子矩阵取值 == 原矩阵对应格",
          sub2.get(0, 3) == dm.get(0, 6) and sub2.get(1, 2) == dm.get(2, 4))


# ---------------------------------------------------------------- 3
def t3_candidate_generation():
    print("\n=== 3. 候选生成 ===")
    ps, dest, _ = make_instance(15, 1)
    best, total, dm = solve_all(ps, dest)
    cap = 10.0

    r = suggest_drops(ps, dest, dm, cap, current_vehicles=best, current_total=total,
                      max_candidates=24, verify_top=4, verify_budget_s=1.0)
    check("返回了结果字典", isinstance(r, dict))
    check("候选数量 ≤ max_candidates",
          r["candidates_generated"] <= 24, f"{r['candidates_generated']}")
    sc = r["strategy_counts"]
    check("候选覆盖了「距离前缀」与「整车剔除」两类策略",
          sc["距离前缀"] >= 1 and sc["整车剔除"] == len(best),
          f"距离前缀 {sc['距离前缀']} / 整车 {sc['整车剔除']}（车数 {len(best)}）")
    check("current_* 回显正确",
          r["current_total_cost"] == round(total, 2)
          and r["current_per_capita"] == round(total / 15, 2),
          f"¥{r['current_total_cost']} / 人均 ¥{r['current_per_capita']}")

    # max_candidates 收紧后候选数应严格受限（限流必须真的生效）
    r2 = suggest_drops(ps, dest, dm, cap, current_vehicles=best, current_total=total,
                       max_candidates=4, verify_top=2, verify_budget_s=1.0)
    check("max_candidates 严格限流生效",
          r2["candidates_generated"] <= 4, f"{r2['candidates_generated']}")
    check("限流时整车候选优先保留（不被距离前缀挤掉）",
          r2["strategy_counts"]["整车剔除"] == len(best),
          str(r2["strategy_counts"]))


# ---------------------------------------------------------------- 4
def t4_non_monotonic():
    print("\n=== 4. 剔人非单调（核心事实）===")
    ps, dest, _ = make_instance(15, 1)
    dm = make_haversine_dm([(p.lat, p.lng) for p in ps] + [(dest.lat, dest.lng)])
    dest_idx = len(ps)

    random.seed(42)
    diag = {}
    best, _ = optimize(ps, dest, dm, mode="cost", time_budget_s=2.0, diag=diag)
    total_all = sum(v.cost for v in best)

    # 找最远的一位
    far_i = max(range(len(ps)), key=lambda i: dm.get(i, dest_idx)[0])
    keep = [i for i in range(len(ps)) if i != far_i]
    random.seed(42)
    diag2 = {}
    best2, _ = optimize([ps[i] for i in keep], dest, subset_matrix(dm, keep, dest_idx),
                        mode="cost", time_budget_s=2.0, diag=diag2)
    total_cut_far = sum(v.cost for v in best2)

    check("剔掉最远的人后，车辆数并未减少",
          len(best2) == len(best), f"{len(best)} → {len(best2)}")
    check("剔掉最远的人后，总费用反而上升（剔人非单调）",
          total_cut_far > total_all,
          f"¥{total_all:.2f} → ¥{total_cut_far:.2f}")
    check("因此「包内人均」也上升（分母 −1、分子未降）",
          total_cut_far / 14 > total_all / 15,
          f"¥{total_all / 15:.2f} → ¥{total_cut_far / 14:.2f}")


# ---------------------------------------------------------------- 5
def t5_achievable():
    print("\n=== 5. 达标场景 ===")
    ps, dest, _ = make_instance(15, 1)
    best, total, dm = solve_all(ps, dest)
    cap = 13.0   # 实测该实例人均最低约 12.91，13 可达标

    r = suggest_drops(ps, dest, dm, cap, current_vehicles=best, current_total=total,
                      verify_top=4, verify_budget_s=1.5)
    b = r["best"]
    check("achievable = True", r["achievable"] is True)
    check("best.meets_cap = True", b["meets_cap"] is True)
    check("best 的人均 ≤ cap", b["per_capita"] <= cap + 1e-9, f"¥{b['per_capita']}")
    check("note 含「可继续参加活动」", "可继续参加活动" in r["note"])
    # 达标候选里应保留人数最多，而不是人均最低
    ok = [s for s in r["suggestions"] if s["meets_cap"]]
    check("达标者中取保留人数最多",
          b["keep_count"] == max(s["keep_count"] for s in ok),
          f"keep={b['keep_count']} / 候选 {[s['keep_count'] for s in ok]}")
    check("剔除人数 < 原人数（不是把人都剔了）", b["drop_count"] < 15,
          f"剔 {b['drop_count']} 人")


# ---------------------------------------------------------------- 6
def t6_unachievable():
    print("\n=== 6. 不可达场景 ===")
    ps, dest, _ = make_instance(15, 1)
    best, total, dm = solve_all(ps, dest)

    r = suggest_drops(ps, dest, dm, 1.0, current_vehicles=best, current_total=total,
                      verify_top=4, verify_budget_s=1.0)
    b = r["best"]
    check("achievable = False", r["achievable"] is False)
    check("best.meets_cap = False", b["meets_cap"] is False)
    check("note 明确写了无法达标", "无法" in r["note"], r["note"][:40])
    pc_min = min(s["per_capita"] for s in r["suggestions"])
    check("best 落在「近似同优」带宽内（不会为了压低人均而剔光人）",
          b["per_capita"] <= pc_min * (1.0 + PER_CAPITA_NEAR_PCT) + 1e-6,
          f"best ¥{b['per_capita']} ≤ 最低 ¥{pc_min} × {1 + PER_CAPITA_NEAR_PCT}")
    check("note 给出可执行的整改建议（放宽上限 / 换集中上车点）",
          "建议把上限放宽到" in r["note"] and "车均里程" in r["note"],
          r["note"][:40])
    check("best 不全是极端剔除（保留人数 > 最低成团人数）",
          b["keep_count"] > PER_CAPITA_MIN_SERVED,
          f"keep={b['keep_count']}")


# ---------------------------------------------------------------- 7
def t7_semantics():
    print("\n=== 7. 建议名单语义自洽 ===")
    ps, dest, _ = make_instance(15, 1)
    best, total, dm = solve_all(ps, dest)
    r = suggest_drops(ps, dest, dm, 12.0, current_vehicles=best, current_total=total,
                      verify_top=4, verify_budget_s=1.0)
    b = r["best"]
    n = 15
    check("drop_count + keep_count == 总人数",
          b["drop_count"] + b["keep_count"] == n,
          f"{b['drop_count']} + {b['keep_count']} = {b['drop_count'] + b['keep_count']}")
    check("drop_names 长度 == drop_count",
          len(b["drop_names"]) == b["drop_count"], str(b["drop_names"]))
    all_names = {p.name for p in ps}
    check("drop_names 都是真实乘客（无杜撰）",
          set(b["drop_names"]) <= all_names)
    check("drop_names 无重复", len(set(b["drop_names"])) == len(b["drop_names"]))
    check("每个建议都带 strategy 与 per_vehicle_cost",
          all(s.get("strategy") and s.get("per_vehicle_cost") is not None
              for s in r["suggestions"]))


# ---------------------------------------------------------------- 8
def t8_verify_calls_optimize():
    print("\n=== 8. 真的做了真实求解（计数器桩）===")
    ps, dest, _ = make_instance(15, 1)
    best, total, dm = solve_all(ps, dest)
    calls = {"n": 0}
    real_optimize = opt.optimize

    def counting(*a, **kw):
        calls["n"] += 1
        return real_optimize(*a, **kw)

    opt.optimize = counting
    try:
        r = suggest_drops(ps, dest, dm, 10.0, current_vehicles=best,
                          current_total=total, verify_top=3, verify_budget_s=1.0)
    finally:
        opt.optimize = real_optimize
    check("verify_top=3 → 恰好 3 次真实求解",
          calls["n"] == 3, f"实际 {calls['n']} 次")
    check("candidates_verified 与之一致",
          r["candidates_verified"] == 3, str(r["candidates_verified"]))
    # verify_top 有下限 3（达标候选×2 + 估算最优 + 最保守候选），传 2 也仍跑 3 次；
    # 估算阶段绝不能调用 optimize（否则每次建议都退化成几十次全量求解）
    calls["n"] = 0
    opt.optimize = counting
    try:
        suggest_drops(ps, dest, dm, 10.0, current_vehicles=best,
                      current_total=total, verify_top=2, verify_budget_s=1.0)
    finally:
        opt.optimize = real_optimize
    check("verify_top 有下限 3（传 2 仍跑 3 次，估算阶段零调用）",
          calls["n"] == 3, f"实际 {calls['n']} 次")
    calls["n"] = 0
    opt.optimize = counting
    try:
        suggest_drops(ps, dest, dm, 10.0, current_vehicles=best,
                      current_total=total, verify_top=5, verify_budget_s=1.0)
    finally:
        opt.optimize = real_optimize
    check("verify_top=5 → 5 次（候选充足时为 5）",
          calls["n"] == 5, f"实际 {calls['n']} 次")


# ---------------------------------------------------------------- 9
def t9_deterministic():
    print("\n=== 9. 确定性与 RNG 隔离 ===")
    ps, dest, _ = make_instance(15, 1)
    best, total, dm = solve_all(ps, dest)

    r1 = suggest_drops(ps, dest, dm, 12.0, current_vehicles=best, current_total=total,
                       verify_top=3, verify_budget_s=1.0)
    r2 = suggest_drops(ps, dest, dm, 12.0, current_vehicles=best, current_total=total,
                       verify_top=3, verify_budget_s=1.0)
    check("两次调用结果完全一致（含名单与人均）",
          r1["best"] == r2["best"] and r1["note"] == r2["note"],
          f"{r1['best']['drop_names']}")

    before = random.getstate()
    suggest_drops(ps, dest, dm, 12.0, current_vehicles=best, current_total=total,
                  verify_top=3, verify_budget_s=1.0)
    after = random.getstate()
    check("不污染全局 RNG（调用前后 random 状态相同）", before == after)


# ---------------------------------------------------------------- 10
def t10_edges():
    print("\n=== 10. 边界 ===")
    ps, dest, _ = make_instance(15, 1)
    best, total, dm = solve_all(ps, dest)

    check("cap=None → 返回 None（不约束）",
          suggest_drops(ps, dest, dm, None, current_vehicles=best,
                        current_total=total) is None)
    check("人数 ≤ 最低成团 → None",
          suggest_drops(ps[:2], dest, dm, 10.0) is None)

    r = suggest_drops(ps[:3], dest, dm, 1.0, verify_budget_s=0.5)
    check("仅 3 人（无整车剔除候选）→ 不崩且给出行距候选",
          r is not None and r["candidates_generated"] >= 1,
          str(r and r["candidates_generated"]))

    r2 = suggest_drops(ps, dest, dm, 10.0, current_vehicles=None,
                       current_total=None, verify_top=2, verify_budget_s=0.5)
    check("current_vehicles 缺省（只按距离剔）→ 不崩",
          r2 is not None and r2["best"] is not None)
    check("无 current 时 current_* 为 None",
          r2["current_per_capita"] is None and r2["current_vehicle_count"] is None)


# ---------------------------------------------------------------- 11
def t11_sqlite_roundtrip():
    print("\n=== 11. SQLite 往返（人均上限落库 / 人均恢复）===")
    import importlib
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db", prefix="carpool_pc_")
    os.close(fd)
    os.unlink(path)
    os.environ["CARPOOL_DB"] = path
    import db
    importlib.reload(db)
    db.init_db()

    tid = db.save_task({"task_code": "pctest1", "task_name": "人均往返",
                        "status": db.TASK_CREATED, "passenger_count": 12,
                        "optimization_mode": "cost", "vehicle_mode": "auto",
                        "direction": "pickup", "budget_second": 10,
                        "per_capita_cap": 13.0})
    job_id = db.create_job(tid, 10)

    def vehicle(vid, pax, cost):
        return {"vehicle_id": vid, "vehicle_type": 6, "passenger_count": pax,
                "order": [f"乘客{i}" for i in range(pax)],
                "coordinates": [], "path": [], "leg_durations": [],
                "passengers": [{"name": f"乘客{i}", "sequence_no": i + 1,
                                "direct_duration_s": 600, "ride_duration_s": 700,
                                "increase_duration_s": 100, "detour_ratio": 1.17,
                                "allocated_cost": 10.0, "warning": None}
                               for i in range(pax)],
                "distance_km": 12.3, "duration_min": 30.0, "cost": cost}

    # 12 人 / 2 车 / 总费用 155.0 → 人均 12.92
    result = {"total_cost": 155.0, "baseline_cost": 400.0, "saving_rate": 61.3,
              "vehicle_count": 2, "four_seat_count": 0, "six_seat_count": 2,
              "direction": "pickup", "warning": None,
              "vehicles": [vehicle("V01", 6, 77.5), vehicle("V02", 6, 77.5)]}
    db.save_solution(tid, job_id, result)
    back = db.load_result("pctest1")

    check("served_count 由 route_passenger 行数推出 = 12",
          back["served_count"] == 12, str(back["served_count"]))
    check("人均 = 总费用 / 参与人数 = 12.92",
          abs(back["per_capita_cost"] - 12.92) < 1e-6, str(back["per_capita_cost"]))
    check("per_capita_cap 落到 carpool_task 并可恢复",
          back["per_capita_cap"] == 13.0, str(back["per_capita_cap"]))
    check("老任务（未设上限）恢复为 None",
          db.save_task({"task_code": "pctest2", "task_name": "无上限",
                        "status": db.TASK_CREATED, "passenger_count": 3,
                        "direction": "pickup"}) is not None
          and db.load_task("pctest2")["task"].get("per_capita_cap") is None)


def main():
    print("=" * 62)
    print("A9 人均车费上限与「建议剔除」单元测试")
    print("=" * 62)
    t1_definition()
    t2_subset_matrix()
    t3_candidate_generation()
    t4_non_monotonic()
    t5_achievable()
    t6_unachievable()
    t7_semantics()
    t8_verify_calls_optimize()
    t9_deterministic()
    t10_edges()
    t11_sqlite_roundtrip()

    total = len(PASS) + len(FAIL)
    print(f"\n{'=' * 62}")
    print(f"A9 测试：{len(PASS)}/{total} 通过")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
        sys.exit(1)
    print("全部通过 ✔")


if __name__ == "__main__":
    main()
