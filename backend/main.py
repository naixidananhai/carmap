# -*- coding: utf-8 -*-
"""
FastAPI 后端入口
================
接口：
- POST /api/tasks          创建优化任务（上传名单）
- GET  /api/tasks/{id}      查询任务状态
- GET  /api/tasks/{id}/result 获取优化结果
- GET  /health             健康检查
"""

import uuid
import time
from typing import List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware

from optimizer import Passenger, DistanceMatrix, optimize
from baidu_map import BaiduMapClient

app = FastAPI(title="拼车路线优化系统", version="1.1")

# CORS：允许前端跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件服务（前端页面）
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

@app.get("/")
async def read_index():
    return FileResponse(os.path.join(frontend_dir, "index.html"))

# 内存存储任务（V1.1 单机，后续可换 SQLite）
TASKS = {}


# ============ 数据模型 ============
class PassengerIn(BaseModel):
    name: str
    address: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


class TaskCreate(BaseModel):
    passengers: List[PassengerIn]
    dest_address: Optional[str] = None
    dest_lat: float
    dest_lng: float
    mode: str = "cost"  # cost / balanced / fewest_vehicles
    time_budget_s: int = 60
    direction: str = "pickup"  # pickup=集合接人 / dropoff=散场送人


class VehicleOut(BaseModel):
    vehicle_id: str
    passenger_count: int
    order: List[str]  # 接人顺序（乘客名）
    coordinates: List[dict] = []  # 接人顺序的坐标 [{lat, lng, name}, ...]
    path: List[dict] = []  # 沿真实路网的折线点 [{lat, lng}, ...]，为空时前端降级为直线
    leg_durations: List[float] = []  # 每段耗时（分钟），与 coordinates 顺序对应，供前端时间线累加
    distance_km: float
    duration_min: float
    cost: float


class TaskResult(BaseModel):
    task_id: str
    status: str  # PENDING / RUNNING / DONE / FAILED
    direction: str = "pickup"  # pickup / dropoff
    total_cost: float
    baseline_cost: float
    savings: float
    saving_rate: float
    vehicle_count: int
    vehicles: List[VehicleOut]
    error: Optional[str] = None


# ============ 后台优化任务 ============
def build_vehicle_path(stops, idx_map, dest_idx, path_matrix):
    """
    按接人顺序拼接一辆车的真实路网折线。
    stops: [Passenger, ..., dest]（dest.id == "DEST"）
    返回 [(lng, lat), ...]；任一段无路径数据时返回 []（前端降级为直线）
    """
    if not path_matrix:
        return []
    pts = []
    for k in range(1, len(stops)):
        prev, cur = stops[k - 1], stops[k]
        i = dest_idx if prev.id == "DEST" else idx_map.get(prev.id)
        j = dest_idx if cur.id == "DEST" else idx_map.get(cur.id)
        if i is None or j is None:
            return []
        seg = path_matrix[i][j]
        if not seg:
            return []
        # 段与段首尾相接，去掉重复的连接点
        if pts and abs(pts[-1][0] - seg[0][0]) < 1e-6 and abs(pts[-1][1] - seg[0][1]) < 1e-6:
            pts.extend(seg[1:])
        else:
            pts.extend(seg)
    if not pts:
        return []
    # 百度会把起终点吸附到最近道路（约几十米偏移），首尾补上精确坐标，
    # 让折线与站点标记对齐；中间站就近经过即可，强行插入会之字形抖动
    first_stop, last_stop = stops[0], stops[-1]
    if abs(pts[0][0] - first_stop.lng) > 1e-6 or abs(pts[0][1] - first_stop.lat) > 1e-6:
        pts.insert(0, (first_stop.lng, first_stop.lat))
    if abs(pts[-1][0] - last_stop.lng) > 1e-6 or abs(pts[-1][1] - last_stop.lat) > 1e-6:
        pts.append((last_stop.lng, last_stop.lat))
    return pts


def run_optimization(task_id: str, passengers_in: List[PassengerIn],
                     dest_lat: float, dest_lng: float,
                     mode: str, time_budget_s: int, direction: str = "pickup"):
    try:
        TASKS[task_id]["status"] = "RUNNING"
        TASKS[task_id]["stage"] = "matrix"

        # 构建乘客对象
        passengers = []
        for i, p in enumerate(passengers_in):
            passengers.append(Passenger(
                id=f"P{i+1:03d}",
                name=p.name,
                lat=p.lat,
                lng=p.lng,
            ))
        dest = Passenger(id="DEST", name="终点", lat=dest_lat, lng=dest_lng)

        # 构建距离矩阵（先试百度，失败用 Haversine）
        all_points = [(p.lat, p.lng) for p in passengers] + [(dest.lat, dest.lng)]
        baidu = BaiduMapClient()
        baidu_matrix = None
        path_matrix = None  # path_matrix[i][j] = [(lng, lat), ...] 真实路网折线点
        try:
            print(f"[任务 {task_id}] 调用百度地图距离矩阵...")
            baidu_matrix, path_matrix = baidu.build_full_matrix(all_points)
            # 检查是否全 None（调用失败）
            if all(baidu_matrix[i][j] is None for i in range(len(all_points)) for j in range(len(all_points)) if i != j):
                print(f"[任务 {task_id}] 百度矩阵全空，降级到 Haversine")
                baidu_matrix = None
                path_matrix = None
        except Exception as e:
            print(f"[任务 {task_id}] 百度矩阵调用异常: {e}，降级到 Haversine")
            baidu_matrix = None
            path_matrix = None

        # 散场模式：距离矩阵转置。D'(i,j) = D(j,i)，
        # 在转置矩阵上按「接人」语义求解，数学上等价于在原矩阵上
        # 优化「从活动点出发依次送达各住宿点」，optimizer.py 零改动。
        # 注意 1：path_matrix 不转置——折线有方向性（单行道正向/反向不是一条路），
        # 输出反转后段段取原始正向折线 M[上一站][下一站] 即可。
        # 注意 2：必须在创建 DistanceMatrix 之前转置，否则 dm 持原始引用、优化跑错方向
        is_dropoff = (direction == "dropoff")
        fwd_matrix = baidu_matrix  # 转置前的原始矩阵：leg_durations 用正向段的耗时
        if is_dropoff:
            n = len(all_points)
            if baidu_matrix is not None:
                baidu_matrix = [[baidu_matrix[j][i] for j in range(n)] for i in range(n)]
            print(f"[任务 {task_id}] 散场模式：距离矩阵已转置（折线矩阵保持正向）")

        dm = DistanceMatrix(all_points, baidu_matrix=baidu_matrix)

        # 跑优化（stage_cb 把 初始解/ALNS 阶段推给前端进度条）
        best, baseline = optimize(passengers, dest, dm, mode=mode, time_budget_s=time_budget_s,
                                  stage_cb=lambda s: TASKS[task_id].update(stage=s))

        # 组装结果
        total_cost = sum(v.cost for v in best)
        # 乘客 id -> 距离矩阵下标；终点固定在矩阵最后一位
        idx_map = {p.id: i for i, p in enumerate(passengers)}
        dest_idx = len(passengers)
        vehicles_out = []
        for i, v in enumerate(best):
            # optimize 输出的 order 是「接人」语义：首站 → ... → 活动点。
            # 散场时反转为送人顺序「活动点 → ... → 末位」，坐标与折线同步反转
            seq = list(reversed(v.order)) if is_dropoff else list(v.order)
            coords = [{"lat": p.lat, "lng": p.lng, "name": p.name} for p in seq]
            venue_coord = {"lat": dest.lat, "lng": dest.lng,
                           "name": "活动点" if is_dropoff else "终点"}
            if is_dropoff:
                coords.insert(0, venue_coord)
                stops = [dest] + seq
            else:
                coords.append(venue_coord)
                stops = seq + [dest]
            # 拼接沿真实路网的折线（任一段缺失则返回空，前端降级为直线）
            path_pts = build_vehicle_path(stops, idx_map, dest_idx, path_matrix)
            # 每段耗时（分钟），与 coordinates 顺序对应，供前端时间线累加。
            # 散场时 fwd_matrix 是转置前的原始矩阵——转置矩阵的段是反向边，耗时不符
            legs = []
            for k in range(1, len(stops)):
                a = dest_idx if stops[k-1].id == "DEST" else idx_map.get(stops[k-1].id)
                b = dest_idx if stops[k].id == "DEST" else idx_map.get(stops[k].id)
                if a is None or b is None:
                    legs = []
                    break
                if fwd_matrix and fwd_matrix[a][b] is not None:
                    dur = fwd_matrix[a][b][1]
                else:
                    dur = dm.get(a, b)[1]  # Haversine 降级
                legs.append(round(dur / 60.0, 1))
            vehicles_out.append(VehicleOut(
                vehicle_id=f"V{i+1:02d}",
                passenger_count=len(v.passengers),
                order=[p.name for p in seq],
                coordinates=coords,
                path=[{"lat": p[1], "lng": p[0]} for p in path_pts],
                leg_durations=legs,
                distance_km=round(v.distance_m / 1000, 1),
                duration_min=round(v.duration_s / 60, 1),
                cost=round(v.cost, 2),
            ))

        TASKS[task_id]["result"] = TaskResult(
            task_id=task_id,
            status="DONE",
            direction=direction,
            total_cost=round(total_cost, 2),
            baseline_cost=round(baseline, 2),
            savings=round(baseline - total_cost, 2),
            saving_rate=round((baseline - total_cost) / baseline * 100, 1) if baseline > 0 else 0,
            vehicle_count=len(best),
            vehicles=vehicles_out,
        )
        TASKS[task_id]["status"] = "DONE"
        TASKS[task_id]["stage"] = "done"

    except Exception as e:
        import traceback
        traceback.print_exc()
        TASKS[task_id]["status"] = "FAILED"
        TASKS[task_id]["stage"] = "failed"
        TASKS[task_id]["error"] = str(e)


# ============ 接口 ============
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/tasks", response_model=dict)
def create_task(req: TaskCreate, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())[:8]
    direction = req.direction if req.direction in ("pickup", "dropoff") else "pickup"
    TASKS[task_id] = {
        "task_id": task_id,
        "status": "PENDING",
        "created_at": time.time(),
        "result": None,
        "error": None,
        "stage": "pending",
    }
    background_tasks.add_task(
        run_optimization, task_id, req.passengers,
        req.dest_lat, req.dest_lng, req.mode, req.time_budget_s, direction
    )
    return {"task_id": task_id, "status": "PENDING"}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    t = TASKS.get(task_id)
    if not t:
        return {"error": "task not found"}
    return {
        "task_id": task_id,
        "status": t["status"],
        "stage": t.get("stage", "pending"),
        "result": t["result"],
        "error": t.get("error"),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
