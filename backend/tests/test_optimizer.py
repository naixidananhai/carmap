# -*- coding: utf-8 -*-
"""测试 optimizer：用模拟 30 人数据跑一遍"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from optimizer import Passenger, DistanceMatrix, optimize
import random

random.seed(42)

# 模拟 30 人（郑州）
areas = [
    (34.748, 113.625), (34.755, 113.665), (34.765, 113.700),
    (34.775, 113.740), (34.760, 113.780), (34.750, 113.820),
    (34.720, 113.680), (34.700, 113.720), (34.790, 113.600),
]
passengers = []
for i in range(30):
    area = random.choice(areas)
    lat = area[0] + random.uniform(-0.02, 0.02)
    lng = area[1] + random.uniform(-0.02, 0.02)
    passengers.append(Passenger(id=f"P{i+1:03d}", name=f"乘客{i+1}", lat=lat, lng=lng))

dest = Passenger(id="DEST", name="郑州东站", lat=34.7421, lng=113.7536)

# 构建距离矩阵（Haversine 回退，不传 baidu_matrix）
all_points = [(p.lat, p.lng) for p in passengers] + [(dest.lat, dest.lng)]
dm = DistanceMatrix(all_points, baidu_matrix=None)

print("开始优化...")
best, baseline = optimize(passengers, dest, dm, mode="cost", time_budget_s=10)

total_cost = sum(v.cost for v in best)
print(f"\n{'='*60}")
print(f"Baseline（单独叫车）: {baseline:.2f} 元")
print(f"ALNS 优化后:        {len(best)} 辆车，{total_cost:.2f} 元")
print(f"费用下降率:          {(baseline - total_cost) / baseline * 100:.1f}%")
print(f"\n各车详情:")
for i, v in enumerate(best):
    order_str = " → ".join(p.id for p in v.order) + " → 郑州东站"
    print(f"  V{i+1:02d} ({len(v.passengers)}人): {order_str}")
    print(f"       {v.distance_m/1000:.1f}km, {v.duration_s//60}分{v.duration_s%60}秒, {v.cost:.2f}元")
