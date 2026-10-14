# -*- coding: utf-8 -*-
"""
散场（dropoff）模式端到端验证
- 逻辑单测：转置矩阵下 build_vehicle_path 的散场段序
- E2E：提交 dropoff 任务，校验输出结构（活动点在首、送人顺序、折线首尾对齐）
- 实证：百度有向矩阵非对称（证明转置处理必要）
"""
import sys, os, json, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from baidu_map import BaiduMapClient
from main import build_vehicle_path
from optimizer import Passenger

BASE = "http://localhost:8000"

PASSENGERS = [
    ("张三", 34.748, 113.625),
    ("李四", 34.755, 113.665),
    ("王五", 34.765, 113.700),
    ("赵六", 34.775, 113.740),
    ("钱七", 34.760, 113.780),
]
DEST = (34.7421, 113.7536)  # 活动点

ok = True
def check(cond, msg):
    global ok
    print(("  [OK] " if cond else "  [FAIL] ") + msg)
    if not cond:
        ok = False

def post_task(direction):
    body = {
        "passengers": [{"name": n, "lat": la, "lng": ln} for n, la, ln in PASSENGERS],
        "dest_lat": DEST[0], "dest_lng": DEST[1],
        "mode": "cost", "time_budget_s": 10,
        "direction": direction,
    }
    req = urllib.request.Request(BASE + "/api/tasks", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["task_id"]

def poll(task_id, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        with urllib.request.urlopen(BASE + "/api/tasks/" + task_id, timeout=30) as r:
            d = json.load(r)
        if d["status"] == "DONE":
            return d["result"]
        if d["status"] == "FAILED":
            raise RuntimeError(d.get("error"))
        time.sleep(2)
    raise TimeoutError("任务超时")

from math import radians, sin, cos, asin, sqrt
def hav(a, b):
    la1, lo1, la2, lo2 = map(radians, [a[1], a[0], b[1], b[0]])
    h = sin((la2-la1)/2)**2 + cos(la1)*cos(la2)*sin((lo2-lo1)/2)**2
    return 6371 * 2 * asin(sqrt(h))

# ---------- 1. 逻辑单测：散场段序 ----------
print("=== 逻辑单测：build_vehicle_path 散场段序 ===")
dest = Passenger(id="DEST", name="活动点", lat=DEST[0], lng=DEST[1])
ps = [Passenger(id=f"P{i+1:03d}", name=n, lat=la, lng=ln) for i, (n, la, ln) in enumerate(PASSENGERS)]
idx_map = {p.id: i for i, p in enumerate(ps)}
dest_idx = len(ps)
all_points = [(p.lat, p.lng) for p in ps] + [DEST]

# 用假 provider 注入已知折线：P[i][j] = i→j 的折线（保持正向，不转置）
# provider.driving_route(o_lat, o_lng, d_lat, d_lng) -> (距离, 耗时, 折线)
# 注意坐标传入顺序为 (lat, lng)，而折线点为 (lng, lat)
class FakeProvider:
    P = {}
    def driving_route(self, o_lat, o_lng, d_lat, d_lng):
        key = (round(o_lat, 6), round(o_lng, 6), round(d_lat, 6), round(d_lng, 6))
        pts = self.P.get(key)
        if pts is None:
            return None
        return (1000.0, 60.0, pts)

fake = FakeProvider()
# 活动点 -> 李四
fake.P[(DEST[0], DEST[1], PASSENGERS[1][1], PASSENGERS[1][2])] = [(113.72, 34.755), (113.73, 34.752)]
# 李四 -> 张三
fake.P[(PASSENGERS[1][1], PASSENGERS[1][2], PASSENGERS[0][1], PASSENGERS[0][2])] = [(113.66, 34.752), (113.65, 34.748)]

stops = [dest, ps[1], ps[0]]   # 送人顺序：活动点 -> 李四 -> 张三
pts = build_vehicle_path(fake, stops, idx_map, dest_idx, all_points)
check(len(pts) > 0, "散场 stops 序列能取到路径段")
check(pts[0] == (DEST[1], DEST[0]), f"首点为活动点精确坐标 {pts[0]}")
check(pts[-1] == (ps[0].lng, ps[0].lat), f"末点为最后住宿点精确坐标 {pts[-1]}")
check((113.72, 34.755) in pts, "第一段取自 活动点→李四（正向折线）")
check((113.66, 34.752) in pts, "第二段取自 李四→张三（正向折线）")

# ---------- 2. E2E：散场任务 ----------
print("\n=== E2E：提交散场任务 ===")
tid = post_task("dropoff")
print(f"  task_id={tid}")
res = poll(tid)
check(res["direction"] == "dropoff", "direction=dropoff")
check(res["vehicle_count"] >= 1, f"车辆数 {res['vehicle_count']}")

for v in res["vehicles"]:
    coords = v["coordinates"]
    check(abs(coords[0]["lat"] - DEST[0]) < 1e-6 and abs(coords[0]["lng"] - DEST[1]) < 1e-6,
          f"{v['vehicle_id']} 首站为活动点")
    check(coords[0]["name"] == "活动点", f"{v['vehicle_id']} 首站名称=活动点")
    legs = v.get("leg_durations") or []
    if len(legs) != len(coords) - 1:
        check(False, f"{v['vehicle_id']} leg 段数 {len(legs)} != 站数-1 {len(coords)-1}")
    elif abs(sum(legs) - v["duration_min"]) > 2:
        check(False, f"{v['vehicle_id']} leg 合计 {sum(legs)} 与总时长 {v['duration_min']} 偏差过大")
    else:
        check(True, f"{v['vehicle_id']} leg_durations {len(legs)} 段，合计 {sum(legs)}≈{v['duration_min']} 分钟")
    path = v["path"]
    if len(path) > 1:
        check(abs(path[0]["lat"] - DEST[0]) < 1e-6 and abs(path[0]["lng"] - DEST[1]) < 1e-6,
              f"{v['vehicle_id']} 折线起点=活动点")
        last = coords[-1]
        check(abs(path[-1]["lat"] - last["lat"]) < 1e-6 and abs(path[-1]["lng"] - last["lng"]) < 1e-6,
              f"{v['vehicle_id']} 折线终点=最后住宿点")
        for c in coords[1:-1]:
            near = any(hav((p["lng"], p["lat"]), (c["lng"], c["lat"])) <= 0.3 for p in path)
            check(near, f"{v['vehicle_id']} 住宿点 {c['name']} 就近经过")
        mx = max(hav((path[i]["lng"], path[i]["lat"]),
                     (path[i+1]["lng"], path[i+1]["lat"])) for i in range(len(path) - 1))
        check(mx < 2.0, f"{v['vehicle_id']} 折线无异常跳跃（最大间距 {mx*1000:.0f}m，{len(path)} 点）")
    else:
        print(f"  [INFO] {v['vehicle_id']} path 为空（降级直线），跳过折线校验")
    print(f"  {v['vehicle_id']}: 活动点 → {' → '.join(v['order'])}"
          f" | {v['distance_km']}km | ¥{v['cost']}")

# ---------- 3. 百度矩阵非对称性实证 ----------
print("\n=== 百度有向矩阵非对称性实证 ===")
c = BaiduMapClient()
a = c.driving_route(DEST[0], DEST[1], PASSENGERS[0][1], PASSENGERS[0][2])
b = c.driving_route(PASSENGERS[0][1], PASSENGERS[0][2], DEST[0], DEST[1])
if a and b:
    print(f"  活动点→张三: {a[0]/1000:.2f}km / {a[1]/60:.1f}min")
    print(f"  张三→活动点: {b[0]/1000:.2f}km / {b[1]/60:.1f}min")
    print("  -> 双向距离不同，转置处理必要且正确" if a[0] != b[0]
          else "  -> 该点对恰好对称")
else:
    print("  [SKIP] 百度接口未返回")

print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
sys.exit(0 if ok else 1)
