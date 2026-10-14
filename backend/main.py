# -*- coding: utf-8 -*-
"""
FastAPI 后端入口
================
接口：
- POST /api/tasks              创建优化任务（上传名单）
- GET  /api/tasks              历史任务列表（A7：从 SQLite 读，重启不丢）
- GET  /api/tasks/{id}         查询任务状态（A8：task_status / job_status 8 态 / sub_status）
- GET  /api/geocode            地址检索代理
- GET  /health                 健康检查

持久化（A7）
------------
SQLite 单文件（backend/../data/carpool.db，可被 CARPOOL_DB 覆盖）。
内存 `TASKS` 仍是读路径的一级缓存，但每次状态跃迁都写穿到表：
  carpool_task / passenger / optimization_job / solution / vehicle_route / route_passenger
进程重启后，GET /api/tasks/{id} 找不到内存记录时会自动从表里恢复（含完整结果）。
"""

import uuid
import time
from typing import List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware

import db
from optimizer import (Passenger, DistanceMatrix, optimize,
                       allocate_cost, detour_warning, DETOUR_WARNING,
                       MAX_ROUTE_SECONDS)
from map_provider import create_provider

app = FastAPI(title="拼车路线优化系统", version="1.2")

# 建表（幂等）。放在导入期执行：单机工具，无迁移流程，
# 保证任何入口（uvicorn / 测试 import）拿到的都是可用库。
db.init_db()

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

# 内存任务表（一级缓存）。真实数据在 SQLite，这里只是进程内热缓存，
# 任何一次状态变化都会写穿到表（见 JobRecorder），因此进程重启不丢。
TASKS = {}

# 支持的优化模式。V1.1 原设计的 balanced（加权和）已删除：
# P1 实测其 cost 项占最终 score 约 76%，产出与 cost 无系统差异（费用仅差 +1.00%），
# 属冗余模式。旧客户端仍传 balanced 时静默映射为 cost，避免破坏兼容。
VALID_MODES = ("cost", "fewest_vehicles")
MODE_ALIASES = {"balanced": "cost"}


def normalize_mode(mode: Optional[str]) -> str:
    m = (mode or "cost").strip().lower()
    if m in MODE_ALIASES:
        print(f"  [mode] {m!r} 已废弃（P1 验证为冗余模式），自动映射为 "
              f"{MODE_ALIASES[m]!r}")
        return MODE_ALIASES[m]
    if m not in VALID_MODES:
        print(f"  [mode] {m!r} 未知，回退默认 'cost'")
        return "cost"
    return m


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
    mode: str = "cost"  # cost / fewest_vehicles（旧值 balanced 会自动映射到 cost）
    # 10 秒与前端默认一致。P1 实测（见 v1.1/P1 ALNS 真实收益验证报告）：n≤30 在 1 秒后
    # 质量即饱和，n=45 时 4s→60s（13.7 倍迭代）只再省 4.3% 费用，默认取 10s 兼顾等待体验
    time_budget_s: int = 10
    max_vehicles: Optional[int] = None  # 车数上限硬约束（可选）
    direction: str = "pickup"  # pickup=集合接人 / dropoff=散场送人
    # A8：「连续无改善」提前终止窗口（秒）。null=按预算自动（20%，3~60s）；
    #     0=关闭（跑满预算，实测多花约 50% 时间换约 1% 费用，见 v1.1/A7-A8 实现说明.md §5）
    stable_stop_s: Optional[float] = None
    task_name: Optional[str] = None


class PassengerDetailOut(BaseModel):
    """
    乘客个人乘车明细（对齐设计文档《数据库设计 V1.1》route_passenger 表）。
    时长统一用**整数秒**（设计口径：内部按秒判定、展示再换算分钟），
    字段名加 _s 后缀以区别于本响应中既有的分钟制字段（如 leg_durations）。
    """
    name: str
    sequence_no: int                     # 沿最终路线的访问顺序（1 起）：集合=上车顺序，散场=送达顺序
    direct_duration_s: int               # 起点直达终点的标准秒数（不拼车）
    ride_duration_s: int                 # 从本上车点到终点的实际乘车秒数
    increase_duration_s: int             # ride - direct
    detour_ratio: Optional[float] = None  # ride / direct；direct 为 0 时无定义
    allocated_cost: float                 # 本车分摊金额（已含尾差）
    warning: Optional[str] = None         # DETOUR_WARNING / None


class VehicleOut(BaseModel):
    vehicle_id: str
    passenger_count: int
    order: List[str]  # 接人顺序（乘客名）
    coordinates: List[dict] = []  # 接人顺序的坐标 [{lat, lng, name}, ...]
    path: List[dict] = []  # 沿真实路网的折线点 [{lat, lng}, ...]，为空时前端降级为直线
    leg_durations: List[float] = []  # 每段耗时（分钟），与 coordinates 顺序对应，供前端时间线累加
    passengers: List[PassengerDetailOut] = []  # 个人乘车明细 + 分摊金额（与 order 同序）
    distance_km: float
    duration_min: float
    cost: float


class TaskResult(BaseModel):
    task_id: str
    status: str  # DONE / FAILED（兼容 V1.0 客户端的粗粒度状态）
    job_status: Optional[str] = None   # A8：optimization_job.status 八态之一
    sub_status: Optional[str] = None   # A8：FAILED 细分（PROVEN_INFEASIBLE / SEARCH_FAILED）
    gap: Optional[float] = None        # 相对理论下界的差距（0.05 = 5%）
    lower_bound: Optional[float] = None  # 费用下界（gap 的分母）
    direction: str = "pickup"  # pickup / dropoff
    total_cost: float
    baseline_cost: float
    savings: float
    saving_rate: float
    vehicle_count: int
    vehicles: List[VehicleOut]
    error: Optional[str] = None
    warning: Optional[str] = None  # 例如「车数上限未能满足」


# ============ 作业状态记录（A8 写穿 SQLite） ============
# 状态语义（optimization_job.status，全量 8 种）：
#   INIT         刚创建，还在准备矩阵
#   FEASIBLE     已找到第一个可行方案（继续优化中）
#   OPTIMIZING   优化进行中
#   STOP_GAP     与理论下界差距 ≤ 5% 提前结束
#   STOP_TIMEOUT 求解预算用尽
#   STOP_STABLE  连续无改善（窗口见 optimizer.stable_window）
#   OPTIMAL      已证明全局最优（费用等于理论下界）
#   FAILED       未找到可行方案（sub_status 细分原因）
JOB_STATUS_TEXT = {
    "INIT": "正在准备距离矩阵",
    "FEASIBLE": "已找到可行方案，继续优化",
    "OPTIMIZING": "优化中",
    "STOP_GAP": "提前收敛（已逼近理论下界）",
    "STOP_TIMEOUT": "预算用尽，返回当前最优",
    "STOP_STABLE": "已收敛（连续无改善），提前结束",
    "OPTIMAL": "已证明达到全局最优",
    "FAILED": "未找到可行方案",
}
SUB_STATUS_TEXT = {
    "PROVEN_INFEASIBLE": "存在乘客的直达耗时已超过单车 90 分钟上限，数学上不可行",
    "SEARCH_FAILED": "求解预算内未找到满足单车 90 分钟约束的方案（不代表数学上不存在）",
}

# 任务状态（carpool_task.status，设计文档 5 种）
TASK_STATUS_TEXT = {
    "CREATED": "已创建",
    "MAPPING": "正在获取地图数据",
    "OPTIMIZING": "优化中",
    "COMPLETED": "已完成",
    "FAILED": "失败",
}

# 旧客户端（含现有前端）依赖的粗粒度状态：PENDING / RUNNING / DONE / FAILED
LEGACY_STATUS = {
    db.JOB_INIT: "PENDING",
    db.JOB_FEASIBLE: "RUNNING",
    db.JOB_OPTIMIZING: "RUNNING",
    db.JOB_FAILED: "FAILED",
}
for _s in db.JOB_TERMINAL_OK:
    LEGACY_STATUS[_s] = "DONE"


class JobRecorder:
    """
    把优化过程的状态同步到 SQLite（A7）与内存 TASKS（读路径）。

    写入节流：ALNS 的进度回调每 50 次迭代触发一次（快机器上约每秒数百次），
    每 0.5 秒最多落一次盘；状态跃迁与终态则强制落盘，保证「崩溃后能看出死在哪个状态」。
    """

    def __init__(self, task: dict, job_id: Optional[int], min_interval: float = 0.5):
        self.task = task
        self.job_id = job_id
        self.min_interval = min_interval
        self._last_write = 0.0

    def _apply(self, fields: dict, force: bool) -> None:
        now = time.time()
        if not force and now - self._last_write < self.min_interval:
            return
        self._last_write = now
        db.update_job(self.job_id, **fields)

    def status(self, job_status: str, extra: Optional[dict] = None,
               task_status: Optional[str] = None) -> None:
        """状态跃迁：内存与库同时更新（强制落盘）。"""
        fields = {"status": job_status}
        if extra:
            fields.update({k: v for k, v in extra.items()
                           if k in ("best_cost", "gap", "lower_bound", "sub_status",
                                    "stop_reason", "iteration", "candidate_count",
                                    "elapsed_seconds", "error")})
        self.task["job_status"] = job_status
        self.task["status"] = LEGACY_STATUS.get(job_status, "RUNNING")
        self.task["terminal"] = job_status in db.JOB_TERMINAL
        self.task["status_text"] = JOB_STATUS_TEXT.get(job_status, job_status)
        for k in ("sub_status", "gap", "lower_bound", "stop_reason", "best_cost",
                  "iteration", "candidate_count"):
            if k in fields:
                self.task[k] = fields[k]
        if extra and extra.get("sub_status"):
            self.task["sub_status_text"] = SUB_STATUS_TEXT.get(extra["sub_status"], "")
        if task_status:
            self.task["task_status"] = task_status
            self.task["task_status_text"] = TASK_STATUS_TEXT.get(task_status, task_status)
            db.set_task_status(self.task["task_id"], task_status)
        self._apply(fields, force=True)

    def progress(self, iteration: int, best_cost: Optional[float]) -> None:
        """周期进度：只写内存 + 节流落库（进度不必精确到每一帧）。"""
        self.task["iteration"] = iteration
        self.task["candidate_count"] = iteration
        if best_cost is not None:
            self.task["best_cost"] = round(best_cost, 2)
        started = self.task.get("started_at")
        if started:
            elapsed = time.time() - started
            budget = self.task.get("budget_s") or 0
            self.task["elapsed_seconds"] = round(elapsed, 1)
            self.task["eta_seconds"] = max(0, int(round(budget - elapsed)))
            if budget > 0:
                self.task["progress"] = min(99, int(elapsed / budget * 100))
        self._apply({"iteration": iteration, "candidate_count": iteration,
                     "best_cost": self.task.get("best_cost")}, force=False)

    def finish(self, job: dict, elapsed: float, task_status: Optional[str] = None) -> None:
        self.task["progress"] = 100
        self.task["eta_seconds"] = 0
        self.task["elapsed_seconds"] = round(elapsed, 1)
        self.status(job["status"], extra={
            "sub_status": job.get("sub_status"),
            "best_cost": job.get("best_cost"),
            "gap": job.get("gap"),
            "lower_bound": job.get("lower_bound"),
            "stop_reason": job.get("stop_reason"),
            "iteration": job.get("iteration"),
            "candidate_count": job.get("iteration"),
            "elapsed_seconds": round(elapsed, 1),
        }, task_status=task_status)


def _job_payload(task: dict, result=None, error=None) -> dict:
    """GET /api/tasks/{id} 的响应体（含 A8 八态作业状态）。"""
    job_status = task.get("job_status") or db.JOB_INIT
    return {
        "task_id": task.get("task_id"),
        # ---- A8：三层状态（各自有明确的归属表/字段）----
        "task_status": task.get("task_status", "CREATED"),
        "task_status_text": task.get("task_status_text", ""),
        "job_status": job_status,
        "job_status_text": JOB_STATUS_TEXT.get(job_status, ""),
        "sub_status": task.get("sub_status"),
        "sub_status_text": task.get("sub_status_text", ""),
        "stop_reason": task.get("stop_reason"),
        "terminal": job_status in db.JOB_TERMINAL,
        "gap": task.get("gap"),
        "lower_bound": task.get("lower_bound"),
        "best_cost": task.get("best_cost"),
        "iteration": task.get("iteration", 0),
        "candidate_count": task.get("candidate_count", 0),
        "progress": task.get("progress", 0),
        "eta_seconds": task.get("eta_seconds"),
        "elapsed_seconds": task.get("elapsed_seconds", 0),
        "provider": task.get("provider"),
        # ---- 兼容字段：旧前端用 stage 画进度点、用 status 判终态 ----
        "status": LEGACY_STATUS.get(job_status, "RUNNING"),
        "stage": task.get("stage", "pending"),
        "result": result,
        "error": error,
    }


# ============ 后台优化任务 ============
def build_vehicle_path(provider, stops, idx_map, dest_idx, all_points):
    """
    按接人顺序拼接一辆车的真实路网折线。

    设计要点（P0 性能优化）：
    矩阵构建阶段**不再预取折线**——预取要为 N² 个点对各发一次请求，
    而求最优解只需要距离；最终方案用到的折线极少（车辆数 × 段数）。
    因此这里按需逐段调用 provider.driving_route()（provider 内部有缓存，
    同一段重复取不会重复计费）。

    stops: [Passenger, ..., dest]（dest.id == "DEST"）
    返回 [(lng, lat), ...]；任一段无路径数据时返回 []（前端降级为直线）
    """
    pts = []
    for k in range(1, len(stops)):
        prev, cur = stops[k - 1], stops[k]
        i = dest_idx if prev.id == "DEST" else idx_map.get(prev.id)
        j = dest_idx if cur.id == "DEST" else idx_map.get(cur.id)
        if i is None or j is None:
            return []
        res = provider.driving_route(all_points[i][0], all_points[i][1],
                                     all_points[j][0], all_points[j][1])
        if not res:
            return []
        seg = res[2]
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
                     mode: str, time_budget_s: int, direction: str = "pickup",
                     max_vehicles: Optional[int] = None,
                     stable_stop_s: Optional[float] = None):
    task = TASKS[task_id]
    rec = JobRecorder(task, task.get("job_id"))
    task["started_at"] = time.time()
    task["budget_s"] = time_budget_s
    try:
        task["stage"] = "matrix"
        rec.status(db.JOB_INIT, task_status=db.TASK_MAPPING)

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

        # 构建距离矩阵（provider 内部处理坐标转换与降级）
        # 注意：不再预取折线，折线在结果阶段按需补取（见 build_vehicle_path）
        all_points = [(p.lat, p.lng) for p in passengers] + [(dest.lat, dest.lng)]
        provider, provider_name = create_provider()
        task["provider"] = provider_name
        db.set_task_status(task_id, db.TASK_MAPPING)
        baidu_matrix = None
        try:
            print(f"[任务 {task_id}] 构建距离矩阵（{provider_name}）...")
            baidu_matrix = provider.build_matrix(all_points)
            # 检查是否全 None（调用失败）
            if all(baidu_matrix[i][j] is None for i in range(len(all_points)) for j in range(len(all_points)) if i != j):
                print(f"[任务 {task_id}] {provider_name} 矩阵全空，降级到 Haversine")
                baidu_matrix = None
        except Exception as e:
            print(f"[任务 {task_id}] {provider_name} 矩阵调用异常: {e}，降级到 Haversine")
            baidu_matrix = None

        # 散场模式：距离矩阵转置。D'(i,j) = D(j,i)，
        # 在转置矩阵上按「接人」语义求解，数学上等价于在原矩阵上
        # 优化「从活动点出发依次送达各住宿点」，optimizer.py 零改动。
        # 折线不在矩阵里（结果阶段按需取），取的时候直接按「上一站→下一站」
        # 的原始正向查询即可，无需特殊处理——provider 的 driving_route
        # 始终返回 A→B 的正向折线，与矩阵方向无关。
        # 注意：必须在创建 DistanceMatrix 之前转置，否则 dm 持原始引用、优化跑错方向
        is_dropoff = (direction == "dropoff")
        fwd_matrix = baidu_matrix  # 转置前的原始矩阵：leg_durations 用正向段的耗时
        if is_dropoff:
            n = len(all_points)
            if baidu_matrix is not None:
                baidu_matrix = [[baidu_matrix[j][i] for j in range(n)] for i in range(n)]
            print(f"[任务 {task_id}] 散场模式：距离矩阵已转置（折线矩阵保持正向）")

        dm = DistanceMatrix(all_points, baidu_matrix=baidu_matrix)

        # 乘客 id -> 距离矩阵下标；终点固定在矩阵最后一位
        idx_map = {p.id: i for i, p in enumerate(passengers)}
        dest_idx = len(passengers)

        # 直达口径（物理行驶方向）：集合=家→活动点；散场=活动点→家。
        # 散场时 dm 是转置矩阵，必须显式给出正向值，否则费用下界会用到反方向的边。
        direct_pairs = []
        for i in range(len(passengers)):
            if is_dropoff:
                if fwd_matrix is not None and fwd_matrix[dest_idx][i] is not None:
                    direct_pairs.append((float(fwd_matrix[dest_idx][i][0]),
                                         float(fwd_matrix[dest_idx][i][1])))
                else:
                    direct_pairs.append(dm.get(dest_idx, i))
            else:
                direct_pairs.append(dm.get(i, dest_idx))

        # 跑优化（stage_cb 把 初始解/ALNS 阶段推给前端进度条，
        # job_cb 把 A8 的 8 态作业状态写穿到 SQLite）
        diag = {}
        best, baseline = optimize(
            passengers, dest, dm, mode=mode, time_budget_s=time_budget_s,
            stage_cb=lambda s: task.update(stage=s),
            max_vehicles=max_vehicles, diag=diag,
            direct_pairs=direct_pairs,
            stable_s=(0.0 if stable_stop_s == 0 else stable_stop_s),
            job_cb=lambda ev: _on_job_event(rec, ev))
        job = diag.get("job") or {}
        warning = None
        missed = diag.get("constraint_missed")
        clamped = diag.get("capacity_clamped")
        if missed:
            warning = (f"车数上限 {missed['requested']} 辆未能满足，"
                       f"实际 {missed['actual']} 辆（受单车 90 分钟时长约束或地理分散限制）")
        elif clamped:
            warning = (f"车数上限 {clamped['requested']} 辆低于满载下限，"
                       f"已按 {clamped['raised_to']} 辆计算")

        # 组装结果
        total_cost = sum(v.cost for v in best)

        def _matrix_secs(a: int, b: int):
            """取 a→b 的耗时（秒）。散场时 dm 是转置矩阵（段是反向边），故优先用转置前的正向矩阵"""
            if fwd_matrix is not None and fwd_matrix[a][b] is not None:
                return float(fwd_matrix[a][b][1])
            return float(dm.get(a, b)[1])  # Haversine 降级（对称，方向无关）

        def _stop_index(p) -> Optional[int]:
            return dest_idx if p.id == "DEST" else idx_map.get(p.id)

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
            # 按需拼接沿真实路网的折线（provider 内部有缓存，重复段不重复计费）
            path_pts = build_vehicle_path(provider, stops, idx_map, dest_idx, all_points)
            # 每段耗时，与 stops / coordinates 顺序对应（leg_secs[k] = stops[k] → stops[k+1]）
            leg_secs = []
            for k in range(1, len(stops)):
                a, b = _stop_index(stops[k - 1]), _stop_index(stops[k])
                if a is None or b is None:
                    leg_secs = []
                    break
                leg_secs.append(_matrix_secs(a, b))
            legs = [round(s / 60.0, 1) for s in leg_secs]  # 分钟制，供前端时间线累加

            # 每人分摊金额（与 seq 同序，尾差归沿路线第一位）
            alloc = allocate_cost(v.cost, len(seq))
            alloc_by_pid = {p.id: alloc[j] for j, p in enumerate(seq)}

            # 个人乘车明细：t_ride(i) = 从该乘客上车点到终点的逐段累加。
            # 集合模式乘客在自家上车 → 本段及其之后所有段（leg_secs[i:]）；
            # 散场模式乘客在活动点上车 → 本段之前所有段（leg_secs[:i]）。
            passengers_out = []
            for k, p in enumerate(stops):
                if p.id == "DEST":
                    continue  # 终点 / 活动点本身不是乘客
                ride_s = (sum(leg_secs[k:]) if not is_dropoff else sum(leg_secs[:k])) \
                    if leg_secs else 0.0
                # 直达时间取「物理方向」的正向矩阵：集合=家→活动点，散场=活动点→家
                pi = idx_map.get(p.id)
                direct_s = 0.0
                if pi is not None:
                    direct_s = _matrix_secs(pi, dest_idx) if not is_dropoff \
                        else _matrix_secs(dest_idx, pi)
                increase_s = ride_s - direct_s
                passengers_out.append(PassengerDetailOut(
                    name=p.name,
                    # 集合：stops=[p0..pk, 终点] → 序号 = k+1；散场：stops=[活动点, h0..hk] → 序号 = k
                    sequence_no=(k + 1) if not is_dropoff else k,
                    direct_duration_s=int(round(direct_s)),
                    ride_duration_s=int(round(ride_s)),
                    increase_duration_s=int(round(increase_s)),
                    detour_ratio=round(ride_s / direct_s, 2) if direct_s > 0 else None,
                    allocated_cost=alloc_by_pid.get(p.id, 0.0),
                    warning=detour_warning(ride_s, direct_s),
                ))

            vehicles_out.append(VehicleOut(
                vehicle_id=f"V{i+1:02d}",
                passenger_count=len(v.passengers),
                order=[p.name for p in seq],
                coordinates=coords,
                path=[{"lat": p[1], "lng": p[0]} for p in path_pts],
                leg_durations=legs,
                passengers=passengers_out,
                distance_km=round(v.distance_m / 1000, 1),
                duration_min=round(v.duration_s / 60, 1),
                cost=round(v.cost, 2),
            ))

        # A8：终态。搜索结束时仍有路线超 5400 秒上限 → FAILED（不做部分交付，
        # 因为「满足硬约束」是承诺；sub_status 区分「证明不可行」与「没搜到」）。
        if not job.get("feasible", True):
            sub = job.get("sub_status") or db.SUB_SEARCH_FAILED
            detail = diag.get("infeasible") or {}
            if sub == db.SUB_PROVEN_INFEASIBLE:
                msg = (f"存在乘客的直达耗时超过单车上限 {MAX_ROUTE_SECONDS} 秒，"
                       f"数学上不存在可行方案")
            else:
                msg = (f"求解预算内未找到满足单车 {MAX_ROUTE_SECONDS} 秒约束的方案"
                       f"（最长路线 {detail.get('worst_route_s', 0):.0f} 秒）；"
                       f"可尝试放宽车数上限或增大求解预算")
            task["stage"] = "failed"
            task["error"] = msg
            rec.finish(job, time.time() - task["started_at"], task_status=db.TASK_FAILED)
            print(f"[任务 {task_id}] 不可行：{msg}")
            return

        result = TaskResult(
            task_id=task_id,
            status="DONE",
            job_status=job.get("status"),
            sub_status=job.get("sub_status"),
            gap=job.get("gap"),
            lower_bound=job.get("lower_bound"),
            direction=direction,
            total_cost=round(total_cost, 2),
            baseline_cost=round(baseline, 2),
            savings=round(baseline - total_cost, 2),
            saving_rate=round((baseline - total_cost) / baseline * 100, 1) if baseline > 0 else 0,
            vehicle_count=len(best),
            vehicles=vehicles_out,
            warning=warning,
        )
        task["result"] = result
        task["stage"] = "done"
        rec.finish(job, time.time() - task["started_at"], task_status=db.TASK_COMPLETED)
        # 落盘最终方案（solution / vehicle_route / route_passenger）
        db.save_solution(task.get("db_task_id"), task.get("job_id"), result.model_dump())
        print(f"[任务 {task_id}] 完成：{job.get('status')}，"
              f"{len(best)} 辆车，{total_cost:.2f} 元，{job.get('stop_reason')}")

    except Exception as e:
        import traceback
        traceback.print_exc()
        task["stage"] = "failed"
        task["error"] = str(e)
        # 崩溃不是「数学不可行」也不是「搜索失败」，故 sub_status 留空（设计文档允许 null）
        rec.status(db.JOB_FAILED, extra={"error": str(e)},
                   task_status=db.TASK_FAILED)


def _on_job_event(rec: "JobRecorder", ev: dict) -> None:
    """
    接收 optimizer.optimize 抛出的 A8 事件并落到内存 + SQLite。

    INIT → FEASIBLE → OPTIMIZING 是严格的时间顺序（矩阵 → 初始解 → ALNS）。
    注意 FEASIBLE 是个瞬时里程碑：初始解是纯计算、几乎不耗时，
    因此它通常只在库里留下痕迹，客户端 2 秒轮询时多半已到 OPTIMIZING。
    """
    kind = ev.get("kind")
    if kind == "init":
        rec.status(db.JOB_INIT, task_status=db.TASK_MAPPING)
    elif kind == "feasible":
        if ev.get("initial_feasible", True):
            rec.status(db.JOB_FEASIBLE, extra={"best_cost": ev.get("cost")},
                       task_status=db.TASK_OPTIMIZING)
        else:
            # 初始解就违反单车 90 分钟约束，不能宣称「已找到可行方案」
            rec.status(db.JOB_OPTIMIZING, task_status=db.TASK_OPTIMIZING)
    elif kind == "optimizing":
        rec.status(db.JOB_OPTIMIZING, task_status=db.TASK_OPTIMIZING)
    elif kind == "progress":
        rec.progress(int(ev.get("iteration") or 0), ev.get("best_cost"))
    # 'finished' 由 run_optimization 统一处理：那里才知道最终结果与可行性


# ============ 接口 ============
@app.get("/health")
def health():
    return {"status": "ok", "db": db.db_path(), "matrix_cache": db.matrix_stats()}


@app.get("/api/geocode", response_model=dict)
def api_geocode(address: str = Query(..., min_length=1), city: str = "郑州市"):
    """
    地址检索代理。

    为什么必须由后端转发：前端的地图 Key 是「Web端(JS API)」类型，
    而高德 JS SDK 内置的 Geocoder 在服务端侧仍以 Web服务 身份调用接口，
    会被判定为 USERKEY_PLAT_NOMATCH 而永不回调（实测页面卡在"检索中..."）。
    高德两类 Key 严格隔离，因此地理编码只能走后端持有的「Web服务」Key。

    city 为必填并强制生效：高德在 city 缺失或无法识别时会把同名地标
    解析到其他城市（实测「二七广场」被解析到天津），必须限定城市。

    返回坐标为 **BD09**，与项目其余部分（/api/tasks、折线数据）一致，
    前端无需再转换一次。
    """
    provider, name = create_provider()
    city = (city or "").strip() or "郑州市"
    try:
        loc = provider.geocode(address, city)
    except TypeError:
        # provider.geocode 只有一个参数（部分实现），退回不带城市的方式
        try:
            loc = provider.geocode(address)
        except Exception as e:
            print(f"[geocode] 调用失败: {e}")
            loc = None
    except Exception as e:
        print(f"[geocode] 调用失败: {e}")
        loc = None
    if loc is None:
        return {"ok": False, "error": "地理编码服务不可用"}
    return {"ok": True, "lng": loc[0], "lat": loc[1], "source": name}


@app.post("/api/tasks", response_model=dict)
def create_task(req: TaskCreate, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())[:8]
    direction = req.direction if req.direction in ("pickup", "dropoff") else "pickup"
    mode = normalize_mode(req.mode)

    # A7：任务与名单先落库（CREATED）。这样即使进程在优化中途崩溃，
    # 提交内容仍可查，且 optimization_job 里能看出死在哪一个状态。
    db_task_id = db.save_task({
        "task_code": task_id,
        "task_name": req.task_name or (req.dest_address or "拼车任务"),
        "status": db.TASK_CREATED,
        "passenger_count": len(req.passengers),
        "destination_name": req.dest_address,
        "destination_lng": req.dest_lng,
        "destination_lat": req.dest_lat,
        "optimization_mode": mode,
        "direction": direction,
        "budget_second": req.time_budget_s,
        "max_vehicles": req.max_vehicles,
    })
    db.save_passengers(db_task_id, [p.model_dump() for p in req.passengers])
    job_id = db.create_job(db_task_id, req.time_budget_s)

    TASKS[task_id] = {
        "task_id": task_id,
        "db_task_id": db_task_id,
        "job_id": job_id,
        "created_at": time.time(),
        "result": None,
        "error": None,
        "stage": "pending",
        # A8 状态字段初始值
        "task_status": db.TASK_CREATED,
        "task_status_text": TASK_STATUS_TEXT[db.TASK_CREATED],
        "job_status": db.JOB_INIT,
        "sub_status": None,
        "sub_status_text": "",
        "stop_reason": None,
        "gap": None,
        "lower_bound": None,
        "best_cost": None,
        "iteration": 0,
        "candidate_count": 0,
        "progress": 0,
        "eta_seconds": req.time_budget_s,
        "elapsed_seconds": 0,
        "terminal": False,
        "status_text": JOB_STATUS_TEXT[db.JOB_INIT],
    }
    background_tasks.add_task(
        run_optimization, task_id, req.passengers,
        req.dest_lat, req.dest_lng, mode, req.time_budget_s, direction,
        req.max_vehicles, req.stable_stop_s
    )
    # status=CREATED 为设计文档口径；PENDING 为旧客户端兼容字段
    return {"task_id": task_id, "status": "PENDING",
            "task_status": db.TASK_CREATED, "job_status": db.JOB_INIT}


@app.get("/api/tasks", response_model=dict)
def list_tasks(limit: int = Query(20, ge=1, le=200)):
    """
    历史任务列表（A7）。直接读 SQLite，因此**重启后依然完整**。
    """
    return {"tasks": db.list_tasks(limit), "db": db.db_path()}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    """
    查询任务状态（A8 八态作业状态）。

    内存有记录就直接返回；没有（进程刚刚重启过）则从 SQLite 恢复：
    任务/作业状态来自 carpool_task + optimization_job，结果来自
    solution + vehicle_route + route_passenger。
    """
    t = TASKS.get(task_id)
    if t:
        return _job_payload(t, result=t.get("result"), error=t.get("error"))

    saved = db.load_task(task_id)
    if not saved:
        return {"error": "task not found"}
    trow, job = saved["task"], saved["job"] or {}
    job_status = job.get("status") or db.JOB_INIT
    task_status = trow.get("status") or db.TASK_CREATED
    result = saved["result"]
    return {
        "task_id": task_id,
        "task_status": task_status,
        "task_status_text": TASK_STATUS_TEXT.get(task_status, task_status),
        "job_status": job_status,
        "job_status_text": JOB_STATUS_TEXT.get(job_status, job_status),
        "sub_status": job.get("sub_status"),
        "sub_status_text": SUB_STATUS_TEXT.get(job.get("sub_status"), ""),
        "stop_reason": job.get("stop_reason"),
        "terminal": job_status in db.JOB_TERMINAL,
        "gap": job.get("gap"),
        "lower_bound": job.get("lower_bound"),
        "best_cost": job.get("best_cost"),
        "iteration": job.get("iteration", 0),
        "candidate_count": job.get("candidate_count", 0),
        "progress": 100 if job_status in db.JOB_TERMINAL else 0,
        "eta_seconds": 0,
        "elapsed_seconds": job.get("elapsed_seconds", 0),
        "provider": None,
        "status": LEGACY_STATUS.get(job_status, "RUNNING"),
        "stage": "done" if job_status in db.JOB_TERMINAL_OK else (
            "failed" if job_status == db.JOB_FAILED else "pending"),
        "result": result,
        "error": job.get("error"),
        "restored_from_db": True,   # 标记：本次响应的数据来自 SQLite 而非进程内存
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
