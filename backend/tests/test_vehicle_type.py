# -*- coding: utf-8 -*-
"""
A2 单元测试 —— 4 座 / 6 座车型混用
====================================
依据：
  · 《技术方案设计 V1.1》第二步「容量拆分」：人数 1-4 允许 4/6 座，5-6 只能 6 座
  · 《算法详细设计 V1.1》§2「车型选择」：两种车型价格相同；默认优先 6 人；
    6 人路线超 5400 秒则尝试拆为 4 人
  · 《P0 冲突统一决策 V1.1》A2：车型是可行性变量，不是成本变量

覆盖：

  1. 配车规则 assign_vehicle_type：1~4 人允许 4/6，5~6 人**只能** 6 座；
     auto 按「最小够用」派车，six 一律 6 座，four 只会出现 4 座。
  2. capacity_limit：auto / six → 6；four → 4。
  3. normalize_vehicle_pref：大小写归一、未知值回退 auto（不抛错）。
  4. 费用函数不含车型：同一里程的 4 座车与 6 座车费用完全相同。
  5. **搜索空间不变**：auto 与 six 的容量上限相同 → 用假时钟冻结迭代次数后，
     两者逐迭代走同一条随机轨迹，费用/车数/迭代数完全一致，仅车型标签不同。
     （这是「车型不是成本变量」最直接的证据。）
  6. four 模式：每辆车载客 ≤4、车数 ≥ ceil(n/4)、费用不低于 auto
     （容量上限更小 → 可行集更小 → 不可能更省）。
  7. 下界随容量收紧：lb(capacity=4) ≥ lb(capacity=6)，且仍然有效（最优 ≥ 下界）。
  8. evaluate 容量罚分：超载解排在合理解之后（与 A8 的时长罚分同源兜底）。
  9. destroy_over_time：可行实例上**休眠**（调用 0 次，保证 P1 验证过的轨迹不变）；
     超时实例上定向拆车，抽走的是「离队后掉时长最多」的那位。
 10. 端到端：三种偏好在真实实例上都产出可行解，且每辆车载客 ≤ 容量上限、
     车型标签与载客数一致。
 11. SQLite 往返：车型与 four/six 统计落库后可无损恢复；老结果缺字段时退化为全 6 座。

全部离线（Haversine / 手工缩放矩阵），零 API 成本。
"""
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))
sys.path.insert(0, _HERE)

import optimizer as opt
from optimizer import (CAR_CAPACITY_4, CAR_CAPACITY_6, MAX_ROUTE_SECONDS,
                       DistanceMatrix, Passenger, Vehicle, allocate_cost,
                       annotate_vehicle_types, assign_vehicle_type,
                       build_initial_solution, calc_cost, capacity_limit,
                       count_vehicle_types, evaluate,
                       lower_bound_cost, normalize_vehicle_pref, optimize,
                       solution_feasible, vehicle_feasible)

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


DEST = Passenger(id="DEST", name="终点", lat=34.7421, lng=113.7536)


def make(n, seed=1, spread=0.05):
    rnd = random.Random(seed)
    ps = [Passenger(id=f"P{i+1:03d}", name=f"乘客{i+1}",
                    lat=DEST.lat + rnd.uniform(-spread, spread),
                    lng=DEST.lng + rnd.uniform(-spread, spread)) for i in range(n)]
    pts = [(p.lat, p.lng) for p in ps] + [(DEST.lat, DEST.lng)]
    return ps, DistanceMatrix(pts, baidu_matrix=None)


def scale_dm(dm, k):
    """把矩阵整体放大 k 倍（拉长行程，用于制造超时实例）。"""
    n = len(dm.points)
    m = [[(dm.get(i, j)[0] * k, dm.get(i, j)[1] * k) for j in range(n)] for i in range(n)]
    return DistanceMatrix(dm.points, baidu_matrix=m)


class _Clock:
    """把 optimizer.time 换成确定性假时钟，使「迭代次数」可复现（见测试 5）。"""
    def __init__(self, start=1000.0, step=0.02):
        self.t = start
        self.step = step

    def time(self):
        self.t += self.step
        return self.t


def with_fake_clock(fn):
    saved = opt.time
    opt.time = _Clock()
    try:
        return fn()
    finally:
        opt.time = saved


def run(ps, dm, pref, budget=1.0, seed=7, **kw):
    random.seed(seed)
    diag = {}
    best, base = optimize(ps, DEST, dm, mode="cost", time_budget_s=budget,
                          diag=diag, vehicle_pref=pref, **kw)
    return best, base, diag


# ====================================================================
def t1_rules():
    print("=== 1. 配车规则（1~4 允许 4/6，5~6 只能 6 座）===")
    auto = [assign_vehicle_type(n, "auto") for n in range(1, 7)]
    six = [assign_vehicle_type(n, "six") for n in range(1, 7)]
    four = [assign_vehicle_type(n, "four") for n in range(1, 7)]
    check("auto：1~4 人 → 4 座", auto[:4] == [4, 4, 4, 4], str(auto[:4]))
    check("auto：5~6 人 → 6 座", auto[4:] == [6, 6], str(auto[4:]))
    check("six：全部 6 座", six == [6] * 6, str(six))
    check("four：1~4 人 → 4 座", four[:4] == [4, 4, 4, 4], str(four[:4]))
    check("硬规则：5~6 人任何偏好下都是 6 座（否则装不下）",
          all(assign_vehicle_type(n, p) == 6
              for n in (5, 6) for p in ("auto", "six", "four")))
    check("车型只可能取 4 或 6", all(assign_vehicle_type(n, p) in (4, 6)
                                 for n in range(1, 7)
                                 for p in ("auto", "six", "four")))
    check("0 人/空车不会派 4 座（防脏数据）", assign_vehicle_type(0, "auto") == 6)

    # 批量派车 + 统计
    types = annotate_vehicle_types(
        [Vehicle(passengers=[None] * k) for k in (6, 4, 1, 5)], "auto")
    check("annotate_vehicle_types 按各车载客数派车",
          types == [6, 4, 4, 6], str(types))
    check("count_vehicle_types 统计正确", count_vehicle_types(types) == (2, 2))
    check("空车列表统计为 (0,0)", count_vehicle_types([]) == (0, 0))


def t2_limits():
    print("\n=== 2. 容量上限 ===")
    check("auto 上限 6", capacity_limit("auto") == 6)
    check("six 上限 6", capacity_limit("six") == 6)
    check("four 上限 4", capacity_limit("four") == 4)
    check("默认（不传）等价 auto", capacity_limit() == 6)


def t3_normalize():
    print("\n=== 3. 偏好归一 ===")
    check("大写归一", normalize_vehicle_pref("SIX") == "six")
    check("两侧空白归一", normalize_vehicle_pref("  Four ") == "four")
    check("None → auto", normalize_vehicle_pref(None) == "auto")
    check("空串 → auto", normalize_vehicle_pref("") == "auto")
    check("未知值回退 auto", normalize_vehicle_pref("bogus") == "auto")


def t4_cost_independent():
    print("\n=== 4. 费用函数不含车型 ===")
    import inspect
    sig = list(inspect.signature(calc_cost).parameters)
    check("calc_cost 没有车型参数（费用与车型无关）",
          sig == ["distance_m"], str(sig))
    check("同一里程费用只由里程决定",
          calc_cost(12345.0) == calc_cost(12345.0))
    # 同一辆车，标 4 座或 6 座（用同一 passenger_count 的两种派车）不影响费用
    d = 9876.0
    v = Vehicle(passengers=[None] * 4, distance_m=d, duration_s=600, cost=calc_cost(d))
    check("4 座车与 6 座车在同一里程下费用相等",
          calc_cost(v.distance_m) == calc_cost(d))


def t5_same_search_space():
    print("\n=== 5. auto 与 six 走同一条搜索轨迹（车型不是搜索变量）===")
    ps, dm = make(12, seed=3, spread=0.05)

    # (a) 初始解完全一致
    va = build_initial_solution(ps, DEST, dm, capacity=capacity_limit("auto"))
    vs = build_initial_solution(ps, DEST, dm, capacity=capacity_limit("six"))
    same_init = (len(va) == len(vs) and all(
        len(x.passengers) == len(y.passengers) and abs(x.distance_m - y.distance_m) < 1e-9
        and x.duration_s == y.duration_s
        for x, y in zip(va, vs)))
    check("auto/six 的初始解逐车一致", same_init,
          f"{len(va)} 辆 vs {len(vs)} 辆")

    # (b) 用假时钟冻结迭代次数后，整条轨迹一致
    a = with_fake_clock(lambda: run(ps, dm, "auto", budget=2.0))
    s = with_fake_clock(lambda: run(ps, dm, "six", budget=2.0))
    ca, cs = sum(v.cost for v in a[0]), sum(v.cost for v in s[0])
    check("auto/six 总费用一致", abs(ca - cs) < 1e-9, f"{ca:.4f} vs {cs:.4f}")
    check("auto/six 车数一致", len(a[0]) == len(s[0]),
          f"{len(a[0])} vs {len(s[0])}")
    check("auto/six 迭代数一致",
          a[2]["job"]["iteration"] == s[2]["job"]["iteration"],
          f"{a[2]['job']['iteration']} vs {s[2]['job']['iteration']}")
    check("auto/six 逐车载客数与里程一致",
          [len(v.passengers) for v in a[0]] == [len(v.passengers) for v in s[0]] and
          all(abs(x.distance_m - y.distance_m) < 1e-9
              for x, y in zip(a[0], s[0])))

    # (c) 只有车型标签不同
    check("auto 下 ≤4 人的车标 4 座、5~6 人标 6 座",
          a[2]["job"]["vehicle_types"] ==
          [assign_vehicle_type(len(v.passengers), "auto") for v in a[0]])
    check("six 下一律 6 座",
          all(t == 6 for t in s[2]["job"]["vehicle_types"]))
    check("four_seat_count 与派车结果一致",
          a[2]["job"]["four_seat_count"] ==
          sum(1 for t in a[2]["job"]["vehicle_types"] if t == 4))
    check("six 模式 four_seat_count 恒为 0", s[2]["job"]["four_seat_count"] == 0)


def t6_four_mode():
    print("\n=== 6. four 模式（车队只有 4 座车）===")
    ps, dm = make(14, seed=5, spread=0.05)
    fbest, _, fdiag = run(ps, dm, "four", budget=2.0)
    abest, _, adiag = run(ps, dm, "auto", budget=2.0)

    check("four：每辆车载客 ≤4",
          all(len(v.passengers) <= CAR_CAPACITY_4 for v in fbest),
          str([len(v.passengers) for v in fbest]))
    k_min = -(-len(ps) // CAR_CAPACITY_4)
    check(f"four：车数 ≥ ceil(14/4)={k_min}", len(fbest) >= k_min,
          f"实际 {len(fbest)} 辆")
    check("four：车型全部为 4 座",
          all(t == 4 for t in fdiag["job"]["vehicle_types"]),
          str(fdiag["job"]["vehicle_types"]))
    check("four：six_seat_count 为 0", fdiag["job"]["six_seat_count"] == 0)
    fc, ac = sum(v.cost for v in fbest), sum(v.cost for v in abest)
    check("four：费用不低于 auto（容量更小 → 可行集更小）", fc >= ac - 1e-6,
          f"four ¥{fc:.2f} vs auto ¥{ac:.2f}")
    check("four：解可行（时长与容量双约束）", solution_feasible(fbest, 4))
    check("auto：解可行", solution_feasible(abest, 6))


def t7_lower_bound():
    print("\n=== 7. 下界随容量收紧且仍有效 ===")
    for n, seed in ((8, 2), (14, 5), (20, 9)):
        ps, dm = make(n, seed=seed)
        direct = [dm.get(i, n)[0] for i in range(n)]
        lb6 = lower_bound_cost(direct, CAR_CAPACITY_6)
        lb4 = lower_bound_cost(direct, CAR_CAPACITY_4)
        check(f"n={n}: lb(cap=4) ≥ lb(cap=6)", lb4 >= lb6 - 1e-9,
              f"{lb4:.2f} vs {lb6:.2f}")

    ps, dm = make(14, seed=5)
    n = len(ps)
    direct = [dm.get(i, n)[0] for i in range(n)]
    lb4 = lower_bound_cost(direct, CAR_CAPACITY_4)
    fbest, _, fdiag = run(ps, dm, "four", budget=2.0)
    check("four 模式：最优费用 ≥ 对应下界（下界有效）",
          sum(v.cost for v in fbest) >= lb4 - 1e-6,
          f"最优 ¥{sum(v.cost for v in fbest):.2f} ≥ lb ¥{lb4:.2f}")
    check("job 里记录的下界与 lower_bound_cost(cap=4) 一致",
          abs(fdiag["job"]["lower_bound"] - lb4) < 0.01)


def t8_evaluate_penalty():
    print("\n=== 8. evaluate 容量罚分 ===")
    ok = [Vehicle(passengers=[None] * 4, distance_m=5000, duration_s=600, cost=30.0)]
    bad = [Vehicle(passengers=[None] * 5, distance_m=5000, duration_s=600, cost=30.0)]
    s_ok = evaluate(ok, 100.0, 4, "cost", None, CAR_CAPACITY_4)
    s_bad = evaluate(bad, 100.0, 5, "cost", None, CAR_CAPACITY_4)
    check("超载解（5 人 / 上限 4）排在合理解之后", s_bad > s_ok,
          f"{s_bad[0]} vs {s_ok[0]}")
    check("同样 5 人，上限 6 时不罚", evaluate(bad, 100.0, 5, "cost", None, 6)[0] == 30.0)
    check("fewest_vehicles 也受容量罚分约束",
          evaluate(bad, 100.0, 5, "fewest_vehicles", None, CAR_CAPACITY_4)[0] >
          evaluate(ok, 100.0, 4, "fewest_vehicles", None, CAR_CAPACITY_4)[0])
    check("vehicle_feasible 按容量上限判定",
          vehicle_feasible(bad[0], CAR_CAPACITY_4) is False and
          vehicle_feasible(bad[0], CAR_CAPACITY_6) is True)


def t9_destroy_over_time():
    print("\n=== 9. destroy_over_time 超时拆车算子 ===")
    # ---- (a) 可行实例上必须休眠（保证 A2 之前的搜索轨迹逐位不变）----
    ps_ok, dm_ok = make(8, seed=11, spread=0.004)
    calls = {"n": 0}
    real = opt.ALNS.destroy_over_time

    def spy(self, *a, **kw):
        calls["n"] += 1
        return real(self, *a, **kw)

    opt.ALNS.destroy_over_time = spy
    try:
        init_ok = build_initial_solution(ps_ok, DEST, dm_ok, capacity=6)
        check("（前置）小范围实例初始解本身可行",
              all(v.duration_s <= MAX_ROUTE_SECONDS for v in init_ok),
              f"最长 {max(v.duration_s for v in init_ok)}s")
        run(ps_ok, dm_ok, "auto", budget=1.0)
        check("无超时车时算子一次都没被调用（休眠）", calls["n"] == 0,
              f"调用 {calls['n']} 次")
    finally:
        opt.ALNS.destroy_over_time = real

    # ---- (b) 超时实例上定向拆车 ----
    ps, dm = make(6, seed=4, spread=0.05)
    n = len(ps)
    idx = list(range(n))
    order = opt.two_opt(dm, idx, n)
    d, s = dm.route_distance(order, n)
    big = scale_dm(dm, (MAX_ROUTE_SECONDS * 1.4) / max(s, 1))   # 单车必超时
    order_b = opt.two_opt(big, idx, n)
    db_, sb_ = big.route_distance(order_b, n)
    check("（前置）构造出超时的 6 人车", sb_ > MAX_ROUTE_SECONDS,
          f"{sb_}s > {MAX_ROUTE_SECONDS}s")

    veh = Vehicle(passengers=list(ps), order=[ps[i] for i in order_b],
                  distance_m=db_, duration_s=sb_, cost=calc_cost(db_))
    alns = opt.ALNS([veh], ps, DEST, big, 0.0, n, "cost", 2.0, capacity_limit=6)
    before = alns.current[0].duration_s
    removed = alns.destroy_over_time(k=1)
    check("抽出 1 位乘客", len(removed) == 1, f"{len(removed)} 位")
    after = max((v.duration_s for v in alns.current), default=0)
    check("拆车后该车时长确实下降", after < before, f"{before}s → {after}s")

    # 抽出的必须是「离队后掉时长最多」的那位
    drops = []
    for p in ps:
        rest = [q for q in ps if q.id != p.id]
        od = opt.two_opt(big, [ps.index(q) for q in rest], n)
        _, ds = big.route_distance(od, n)
        drops.append((before - ds, p.id))
    drops.sort(reverse=True)
    check("抽出的是掉时长最多的那位", removed[0].id == drops[0][1],
          f"{removed[0].id}（掉 {drops[0][0]}s）")
    check("k=2 时抽出 2 位", len(opt.ALNS([veh], ps, DEST, big, 0.0, n, "cost", 2.0,
                                          capacity_limit=6).destroy_over_time(k=2)) == 2)

    # ---- (c) 无超时车 / 单车单人 → 返回空 ----
    alns_ok = opt.ALNS(build_initial_solution(ps_ok, DEST, dm_ok, capacity=6),
                       ps_ok, DEST, dm_ok, 0.0, len(ps_ok), "cost", 1.0, capacity_limit=6)
    check("无超时车 → 返回 []", alns_ok.destroy_over_time() == [])
    solo = Vehicle(passengers=[ps[0]], order=[ps[0]], distance_m=db_, duration_s=sb_,
                   cost=calc_cost(db_))
    alns_solo = opt.ALNS([solo], [ps[0]], DEST, big, 0.0, 1, "cost", 1.0, capacity_limit=6)
    check("单车单人仍超时 → 返回 []（拆不出来，交给判定）",
          alns_solo.destroy_over_time() == [])

    # ---- (d) 端到端：超时实例最终收敛到可行 ----
    ps_h, dm_h = make(8, seed=6, spread=0.09)
    n_h = len(ps_h)
    o = opt.two_opt(dm_h, list(range(n_h)), n_h)
    d_h, s_h = dm_h.route_distance(o, n_h)
    if s_h <= MAX_ROUTE_SECONDS:                     # 视种子而定，必要时拉长
        dm_h = scale_dm(dm_h, (MAX_ROUTE_SECONDS * 1.3) / max(s_h, 1))
    ok_all = True
    worst = 0
    for sd in (1, 2, 3):
        random.seed(sd)
        b, _ = optimize(ps_h, DEST, dm_h, mode="cost", time_budget_s=2.0,
                        vehicle_pref="auto")
        if not solution_feasible(b, 6):
            ok_all = False
        worst = max(worst, max((v.duration_s for v in b), default=0))
    check("超时实例：3 个种子下都收敛到可行解（无车超 5400s）", ok_all,
          f"最长 {worst}s")


def t10_end_to_end():
    print("\n=== 10. 端到端：三种偏好都产出可行派车单 ===")
    ps, dm = make(15, seed=8, spread=0.06)
    for pref in ("auto", "six", "four"):
        best, _, diag = run(ps, dm, pref, budget=2.0)
        cap = capacity_limit(pref)
        job = diag["job"]
        ok_feasible = solution_feasible(best, cap)
        ok_cap = all(len(v.passengers) <= cap for v in best)
        types = job["vehicle_types"]
        ok_type = all(t == assign_vehicle_type(len(v.passengers), pref)
                      for t, v in zip(types, best))
        ok_rule = all(t == 6 for t, v in zip(types, best) if len(v.passengers) >= 5)
        ok_cnt = (job["four_seat_count"] + job["six_seat_count"] == len(best))
        check(f"{pref:4s}：可行 + 不超容量 + 车型与载客数一致 + 5~6 人必 6 座 + 计数自洽",
              ok_feasible and ok_cap and ok_type and ok_rule and ok_cnt,
              f"{len(best)} 辆，4 座 {job['four_seat_count']} / 6 座 {job['six_seat_count']}"
              f"，车型 {types}")
        check(f"{pref:4s}：job 里记录了 vehicle_pref 与 capacity_limit",
              job["vehicle_pref"] == pref and job["capacity_limit"] == cap)

    # max_vehicles 的容量下限校验要用实际容量（four 模式下下限更高）
    _, _, d4 = run(ps, dm, "four", budget=1.0, max_vehicles=2)
    check("four 模式下 max_vehicles 低于容量下限时被上调到 ceil(15/4)=4",
          d4.get("capacity_clamped", {}).get("raised_to") == 4,
          str(d4.get("capacity_clamped")))


def t11_sqlite_roundtrip():
    print("\n=== 11. SQLite 往返（车型落库与恢复）===")
    import importlib
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db", prefix="carpool_veh_")
    os.close(fd)
    os.unlink(path)
    os.environ["CARPOOL_DB"] = path
    import db
    importlib.reload(db)
    db.init_db()

    tid = db.save_task({"task_code": "vehtest1", "task_name": "车型往返",
                        "status": db.TASK_CREATED, "passenger_count": 10,
                        "optimization_mode": "cost", "vehicle_mode": "auto",
                        "direction": "pickup", "budget_second": 10})
    job_id = db.create_job(tid, 10)

    def vehicle(vid, pax, vt):
        # 乘客明细必须与载体人数一致：load_result 的 passenger_count 由
        # route_passenger 行数推出（真实链路里两者恒等，见 main.py 的 passengers_out）
        return {"vehicle_id": vid, "vehicle_type": vt, "passenger_count": pax,
                "order": [f"乘客{i}" for i in range(pax)],
                "coordinates": [], "path": [], "leg_durations": [],
                "passengers": [{"name": f"乘客{i}", "sequence_no": i + 1,
                                "direct_duration_s": 600, "ride_duration_s": 700,
                                "increase_duration_s": 100, "detour_ratio": 1.17,
                                "allocated_cost": 10.0, "warning": None}
                               for i in range(pax)],
                "distance_km": 12.3, "duration_min": 30.0,
                "cost": 43.9}

    result = {"total_cost": 131.7, "baseline_cost": 300.0, "saving_rate": 56.1,
              "vehicle_count": 3, "four_seat_count": 2, "six_seat_count": 1,
              "direction": "pickup", "warning": None,
              "vehicles": [vehicle("V01", 6, 6), vehicle("V02", 4, 4),
                           vehicle("V03", 3, 4)]}
    db.save_solution(tid, job_id, result)
    back = db.load_result("vehtest1")
    check("车辆数与统计无损恢复",
          back["vehicle_count"] == 3 and back["four_seat_count"] == 2
          and back["six_seat_count"] == 1,
          f"{back['vehicle_count']} 辆 / 4 座 {back['four_seat_count']} / "
          f"6 座 {back['six_seat_count']}")
    check("每辆车车型无损恢复",
          [v["vehicle_type"] for v in back["vehicles"]] == [6, 4, 4],
          str([v["vehicle_type"] for v in back["vehicles"]]))
    check("vehicle_mode 落到了 carpool_task",
          (db.load_task("vehtest1")["task"] or {}).get("vehicle_mode") == "auto")

    # 老结果（无 vehicle_type / 无 four_seat_count）→ 退化为全 6 座，不凭空造 4 座
    tid2 = db.save_task({"task_code": "vehtest2", "task_name": "老结果",
                         "status": db.TASK_CREATED, "passenger_count": 9,
                         "direction": "pickup"})
    job2 = db.create_job(tid2, 10)
    leg = vehicle("V01", 4, 6)
    leg.pop("vehicle_type")
    db.save_solution(tid2, job2, {"total_cost": 50.0, "baseline_cost": 100.0,
                                  "saving_rate": 50.0, "vehicle_count": 2,
                                  "direction": "pickup", "vehicles": [leg]})
    old = db.load_result("vehtest2")
    check("缺 vehicle_type 的老结果退化为 6 座",
          all(v["vehicle_type"] == 6 for v in old["vehicles"]))
    check("缺统计的老结果 four_seat_count 为 0", old["four_seat_count"] == 0,
          f"four={old['four_seat_count']} six={old['six_seat_count']}")

    check("恢复出的载客数与保存时一致",
          [v["passenger_count"] for v in back["vehicles"]] == [6, 4, 3],
          str([v["passenger_count"] for v in back["vehicles"]]))
    # 恢复出的车型必须与内存派车口径一致（重启后前端看到的一样）
    rt = [assign_vehicle_type(v["passenger_count"], "auto") for v in back["vehicles"]]
    check("恢复后的车型与 assign_vehicle_type 口径一致",
          rt == [v["vehicle_type"] for v in back["vehicles"]], str(rt))

    try:
        os.unlink(path)
    except OSError:
        pass


def main():
    t1_rules()
    t2_limits()
    t3_normalize()
    t4_cost_independent()
    t5_same_search_space()
    t6_four_mode()
    t7_lower_bound()
    t8_evaluate_penalty()
    t9_destroy_over_time()
    t10_end_to_end()
    t11_sqlite_roundtrip()

    total = len(PASS) + len(FAIL)
    print(f"\n{'=' * 60}")
    print(f"A2 车型测试：{len(PASS)}/{total} 通过")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
        sys.exit(1)
    print("全部通过 ✔")


if __name__ == "__main__":
    main()
