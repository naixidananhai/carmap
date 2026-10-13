# -*- coding: utf-8 -*-
import requests
import time
import json

body = {
    "passengers": [
        {"name": "张三", "lat": 34.748, "lng": 113.625},
        {"name": "李四", "lat": 34.755, "lng": 113.665},
        {"name": "王五", "lat": 34.765, "lng": 113.700}
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

start = time.time()
while True:
    elapsed = time.time() - start
    if elapsed > 30:
        print("超时", flush=True)
        break
    try:
        r = requests.get(f"http://localhost:8000/api/tasks/{task_id}", timeout=3)
        data = r.json()
        status = data.get("status")
        print(f"  [{elapsed:.0f}s] {status}", flush=True)
        if status == "DONE":
            result = data["result"]
            print(f"\n=== 结果 ===", flush=True)
            print(f"车辆数: {result['vehicle_count']}", flush=True)
            for v in result["vehicles"]:
                print(f"\n{v['vehicle_id']} ({v['passenger_count']}人):", flush=True)
                print(f"  顺序: {v['order']}", flush=True)
                print(f"  坐标: {json.dumps(v['coordinates'], ensure_ascii=False, indent=2)}", flush=True)
            break
        elif status == "FAILED":
            print(f"失败: {data.get('error')}", flush=True)
            break
    except Exception as e:
        print(f"  [{elapsed:.0f}s] 查询超时", flush=True)
    time.sleep(1)
