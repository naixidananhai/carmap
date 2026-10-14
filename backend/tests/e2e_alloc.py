# -*- coding: utf-8 -*-
"""
A1 端到端验证：费用分摊 + 个人乘车明细（需要后端已在 8000 端口运行）。

对集合 / 散场两种模式各跑一个任务，逐车校验以下不变式：

  1. 乘客明细条数 == passenger_count，且姓名顺序与 order 完全一致
  2. sequence_no 恰为 1..n 连续整数
  3. 分摊金额合计 == 该车车费（金额守恒，分摊不产生损失也不凭空多出）
  4. increase_duration_s == ride_duration_s - direct_duration_s
  5. detour_ratio ≈ ride / direct
  6. 乘车时长沿路线单调：
       集合模式（各人在自家上车）→ 越晚上车乘得越短，末位 increase == 0
       散场模式（众人在活动点上车）→ 越晚送达乘得越长，首位 increase == 0
  7. 任一人乘车时长 ≤ 整车时长（+ 容差）
  8. warning 与阈值规则一致
"""
import time

import requests

BASE = "http://localhost:8000"
PASSENGERS = [
    {"name": "张三", "lat": 34.748, "lng": 113.625},
    {"name": "李四", "lat": 34.755, "lng": 113.665},
    {"name": "王五", "lat": 34.765, "lng": 113.700},
    {"name": "赵六", "lat": 34.775, "lng": 113.740},
    {"name": "钱七", "lat": 34.760, "lng": 113.780},
    {"name": "孙八", "lat": 34.750, "lng": 113.820},
]
DEST = {"dest_lat": 34.7421, "dest_lng": 113.7536}

fails = []


def check(cond, msg):
    if not cond:
        print("    FAIL  " + msg)
        fails.append(msg)


def run(direction):
    body = dict(passengers=PASSENGERS, mode="cost", time_budget_s=6, direction=direction, **DEST)
    tid = requests.post(f"{BASE}/api/tasks", json=body, timeout=10).json()["task_id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        d = requests.get(f"{BASE}/api/tasks/{tid}", timeout=5).json()
        if d["status"] == "DONE":
            return d["result"]
        if d["status"] == "FAILED":
            raise SystemExit(f"任务 {tid} 失败: {d.get('error')}")
        time.sleep(1)
    raise SystemExit(f"任务 {tid} 超时")


for direction in ("pickup", "dropoff"):
    print("=" * 78)
    print(f"模式：{direction}")
    print("=" * 78)
    r = run(direction)
    print(f"  车数 {r['vehicle_count']} · 总费用 ¥{r['total_cost']} · 方向 {r['direction']}")
    check(r["direction"] == direction, f"direction 回传应为 {direction}")

    n_pax_total = 0
    alloc_total = 0.0
    for v in r["vehicles"]:
        ps = v.get("passengers") or []
        print(f"  {v['vehicle_id']}  {v['passenger_count']} 人  ¥{v['cost']}  "
              f"order={' → '.join(v['order'])}")

        check(len(ps) == v["passenger_count"],
              f"{v['vehicle_id']} 明细条数 {len(ps)} == passenger_count {v['passenger_count']}")
        check([p["name"] for p in ps] == v["order"],
              f"{v['vehicle_id']} 明细姓名顺序与 order 一致")
        check([p["sequence_no"] for p in ps] == list(range(1, len(ps) + 1)),
              f"{v['vehicle_id']} sequence_no 为 1..{len(ps)} 连续")

        s = round(sum(p["allocated_cost"] for p in ps), 2)
        check(s == round(v["cost"], 2),
              f"{v['vehicle_id']} 分摊合计 {s} == 车费 {round(v['cost'], 2)}")

        for p in ps:
            tag = f"{v['vehicle_id']}#{p['sequence_no']} {p['name']}"
            print(f"      #{p['sequence_no']} {p['name']}  分摊 ¥{p['allocated_cost']}"
                  f"  乘车 {p['ride_duration_s']}s / 直达 {p['direct_duration_s']}s"
                  f"  多 {p['increase_duration_s']}s  比例 {p['detour_ratio']}"
                  f"  {p.get('warning') or '-'}")

            check(p["increase_duration_s"] == p["ride_duration_s"] - p["direct_duration_s"],
                  f"{tag} increase == ride - direct")
            if p["detour_ratio"] is not None and p["direct_duration_s"] > 0:
                exp = round(p["ride_duration_s"] / p["direct_duration_s"], 2)
                check(abs(p["detour_ratio"] - exp) <= 0.01,
                      f"{tag} detour_ratio {p['detour_ratio']} ≈ {exp}")

            exp_warn = (p["ride_duration_s"] > 80 * 60) or (
                p["direct_duration_s"] > 0
                and p["ride_duration_s"] / p["direct_duration_s"] > 1.3)
            check(bool(p.get("warning")) == exp_warn,
                  f"{tag} warning 与阈值规则一致（实际 {p.get('warning')}，期望预警={exp_warn}）")

            check(p["ride_duration_s"] <= v["duration_min"] * 60 + 120,
                  f"{tag} 乘车 {p['ride_duration_s']}s ≤ 整车 {v['duration_min']}分+容差")
            n_pax_total += 1
            alloc_total += p["allocated_cost"]

        rides = [p["ride_duration_s"] for p in ps]
        incs = [p["increase_duration_s"] for p in ps]
        if direction == "pickup":
            check(all(rides[i] >= rides[i + 1] for i in range(len(rides) - 1)),
                  f"{v['vehicle_id']} 集合模式：乘车时长沿上车顺序单调不增，实际 {rides}")
            check(incs[-1] == 0, f"{v['vehicle_id']} 集合模式：末位上车者绕路增量为 0，实际 {incs[-1]}")
        else:
            check(all(rides[i] <= rides[i + 1] for i in range(len(rides) - 1)),
                  f"{v['vehicle_id']} 散场模式：乘车时长沿送达顺序单调不减，实际 {rides}")
            check(incs[0] == 0, f"{v['vehicle_id']} 散场模式：首位送达者绕路增量为 0，实际 {incs[0]}")

    check(n_pax_total == len(PASSENGERS), f"乘客总数 {n_pax_total} == {len(PASSENGERS)}")
    check(round(alloc_total, 2) == round(r["total_cost"], 2),
          f"全局分摊合计 {round(alloc_total, 2)} == 总费用 {r['total_cost']}")
    print()

print("=" * 78)
if fails:
    print(f"结果：{len(fails)} 项失败")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("结果：全部通过")
