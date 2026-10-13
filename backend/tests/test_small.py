# -*- coding: utf-8 -*-
"""快速测试：5 个人，真实百度地图，短超时"""
import requests
import time

body = {
    "passengers": [
        {"name": "张三", "lat": 34.748, "lng": 113.625},
        {"name": "李四", "lat": 34.755, "lng": 113.665},
        {"name": "王五", "lat": 34.765, "lng": 113.700},
        {"name": "赵六", "lat": 34.775, "lng": 113.740},
        {"name": "钱七", "lat": 34.760, "lng": 113.780}
    ],
    "dest_lat": 34.7421,
    "dest_lng": 113.7536,
    "mode": "cost",
    "time_budget_s": 5
}

print("提交任务...", flush=True)
resp = requests.post("http://localhost:8000/api/tasks", json=body, timeout=10)
task_id = resp.json()["task_id"]
print(f"任务 ID: {task_id}", flush=True)

# 轮询
start = time.time()
while True:
    elapsed = time.time() - start
    if elapsed > 45:
        print("超时 45 秒", flush=True)
        break
    try:
        r = requests.get(f"http://localhost:8000/api/tasks/{task_id}", timeout=3)
        data = r.json()
        status = data.get("status")
        print(f"  [{elapsed:.0f}s] 状态: {status}", flush=True)
        if status == "DONE":
            result = data["result"]
            print(f"\n=== 结果 ===", flush=True)
            print(f"Baseline: {result['baseline_cost']} 元", flush=True)
            print(f"优化后: {result['total_cost']} 元（{result['vehicle_count']} 辆车）", flush=True)
            print(f"节省: {result['savings']} 元，下降率 {result['saving_rate']}%", flush=True)
            for v in result["vehicles"]:
                print(f"  {v['vehicle_id']} ({v['passenger_count']}人): {' -> '.join(v['order'])} -> 终点", flush=True)
                print(f"    {v['distance_km']}km, {v['duration_min']}分钟, {v['cost']}元", flush=True)
            break
        elif status == "FAILED":
            print(f"失败: {data.get('error')}", flush=True)
            break
    except Exception as e:
        print(f"  [{elapsed:.0f}s] 查询超时: {e}", flush=True)
    time.sleep(1)
