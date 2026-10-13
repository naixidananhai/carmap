# -*- coding: utf-8 -*-
"""端到端验证：提交任务 -> 检查每辆车的真实路网 path"""
import time
from math import radians, sin, cos, asin, sqrt
import requests

BASE = "http://localhost:8000"


def hav(a, b):
    la1, lo1, la2, lo2 = map(radians, [a[1], a[0], b[1], b[0]])
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * asin(sqrt(h))


body = {
    "passengers": [
        {"name": "张三", "lat": 34.748, "lng": 113.625},
        {"name": "李四", "lat": 34.755, "lng": 113.665},
        {"name": "王五", "lat": 34.765, "lng": 113.700},
        {"name": "赵六", "lat": 34.775, "lng": 113.740},
        {"name": "钱七", "lat": 34.760, "lng": 113.780},
    ],
    "dest_lat": 34.7421,
    "dest_lng": 113.7536,
    "mode": "cost",
    "time_budget_s": 5,
}

resp = requests.post(f"{BASE}/api/tasks", json=body, timeout=10)
task_id = resp.json()["task_id"]
print(f"任务 {task_id} 已提交")

deadline = time.time() + 60
stages_seen = set()
while time.time() < deadline:
    d = requests.get(f"{BASE}/api/tasks/{task_id}", timeout=5).json()
    stages_seen.add(d.get("stage"))
    if d["status"] == "DONE":
        break
    if d["status"] == "FAILED":
        print("FAILED:", d.get("error"))
        raise SystemExit(1)
    time.sleep(1)
else:
    print("超时")
    raise SystemExit(1)

if d.get("stage") != "done":
    print(f"!! 完成时 stage={d.get('stage')}，应为 done")
    ok = False
print(f"阶段轨迹: {sorted(stages_seen)}")

result = d["result"]
print(f"\n车辆数: {result['vehicle_count']}, 总费用: {result['total_cost']}, 节省率: {result['saving_rate']}%")

ok = True
for v in result["vehicles"]:
    path = v.get("path") or []
    stops = [(c["lng"], c["lat"]) for c in v["coordinates"]]
    print(f"\n{v['vehicle_id']} ({v['passenger_count']}人): {' -> '.join(v['order'])}")
    print(f"  {v['distance_km']}km / {v['duration_min']}分钟 / ¥{v['cost']}")
    print(f"  path 点数: {len(path)}")

    # leg_durations：段数 = 站数-1，合计 ≈ 总时长（供前端时间线）
    legs = v.get("leg_durations") or []
    print(f"  leg_durations: {legs}（{len(legs)} 段）")
    if len(legs) != len(v["coordinates"]) - 1:
        print(f"  !! leg 段数 {len(legs)} != 站数-1 {len(v['coordinates'])-1}"); ok = False
    elif abs(sum(legs) - v["duration_min"]) > 2:
        print(f"  !! leg 合计 {sum(legs)} 与总时长 {v['duration_min']} 偏差过大"); ok = False

    if len(path) < 2:
        print("  !! path 为空，前端将降级为直线")
        ok = False
        continue

    def close(a, b, tol=1e-4):
        return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol

    # 首尾精确对齐（build_vehicle_path 已补精确坐标）
    first, last = (path[0]["lng"], path[0]["lat"]), (path[-1]["lng"], path[-1]["lat"])
    if not close(first, stops[0]):
        print(f"  !! path 首点 {first} 与第一站 {stops[0]} 不符"); ok = False
    if not close(last, stops[-1]):
        print(f"  !! path 末点 {last} 与终点 {stops[-1]} 不符"); ok = False
    # 中间站就近经过（百度吸附到最近道路，允许约 300m）
    for s in stops[1:-1]:
        d_min = min(hav((p["lng"], p["lat"]), s) for p in path)
        if d_min > 0.3:
            print(f"  !! 站点 {s} 距 path 最近 {d_min*1000:.0f}m，超过 300m"); ok = False
    # 相邻点间距合理（>5km 视为拼接错误）
    max_gap = max(hav((path[i]["lng"], path[i]["lat"]), (path[i+1]["lng"], path[i+1]["lat"]))
                  for i in range(len(path) - 1))
    print(f"  相邻点最大间距: {max_gap*1000:.0f} 米")
    if max_gap > 5:
        print("  !! 存在异常跳跃，疑似拼接错误"); ok = False

print("\n" + ("E2E PASS: 所有车辆均为真实路网折线" if ok else "E2E FAIL"))
raise SystemExit(0 if ok else 1)
