# -*- coding: utf-8 -*-
"""
拼车路线优化算法 V1.1
=====================
支持：
- 距离源：百度地图真实矩阵 / Haversine 模拟（自动降级）
- 算法流程：DBSCAN 聚类 → 容量拆分（6人优先）→ 贪心合并 → ALNS 优化 → 2-opt 接人顺序
- 三种优化模式：cost / balanced / fewest_vehicles
- 费用函数：C(D) = 16 + 3 * max(D_km - 3, 0)
- 超时约束：单车路线 ≤ 5400 秒
"""

import math
import random
import time
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

# ============ 参数 ============
BASE_FEE = 16.0
PER_KM_FEE = 3.0
FREE_KM = 3.0
MAX_ROUTE_SECONDS = 5400
CAR_CAPACITY = 6  # 最大容量
AVG_SPEED_KMH = 30.0  # Haversine 回退时用


@dataclass
class Passenger:
    id: str
    name: str
    lat: float
    lng: float


@dataclass
class Vehicle:
    passengers: List[Passenger] = field(default_factory=list)
    order: List[Passenger] = field(default_factory=list)  # 优化后的接人顺序
    distance_m: float = 0.0
    duration_s: int = 0
    cost: float = 0.0


# ============ 距离计算 ============
def haversine_m(lat1, lng1, lat2, lng2):
    R = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng/2)**2)
    return R * 2 * math.asin(math.sqrt(a)) * 1.4  # ×1.4 近似驾车


class DistanceMatrix:
    """距离矩阵：支持百度真实数据 / Haversine 回退"""
    def __init__(self, points: List[Tuple[float, float]], baidu_matrix=None):
        """
        points: [(lat, lng), ...] 所有点（乘客起点 + 终点）
        baidu_matrix: 百度返回的 matrix[i][j] = (distance_m, duration_s)，None 则用 Haversine
        """
        self.points = points
        self.n = len(points)
        self.baidu_matrix = baidu_matrix

    def get(self, i, j):
        """返回 (distance_m, duration_s)"""
        if self.baidu_matrix and self.baidu_matrix[i][j] is not None:
            return self.baidu_matrix[i][j]
        # Haversine 回退
        lat1, lng1 = self.points[i]
        lat2, lng2 = self.points[j]
        d = haversine_m(lat1, lng1, lat2, lng2)
        dur = int(d / 1000.0 / AVG_SPEED_KMH * 3600)
        return (d, dur)

    def route_distance(self, indices: List[int], dest_idx: int):
        """按 indices 顺序接人，最后到 dest_idx，返回 (distance_m, duration_s)"""
        total_d = 0.0
        total_s = 0
        for k in range(len(indices)):
            if k == 0:
                fr = indices[k]
            else:
                fr = indices[k-1]
            to = indices[k]
            d, s = self.get(fr, to)
            total_d += d
            total_s += s
        # 最后到终点
        d, s = self.get(indices[-1], dest_idx)
        total_d += d
        total_s += s
        return (total_d, total_s)


# ============ 费用计算 ============
def calc_cost(distance_m: float) -> float:
    km = distance_m / 1000.0
    return BASE_FEE + PER_KM_FEE * max(km - FREE_KM, 0)


# ============ DBSCAN 聚类 ============
def dbscan(points: List[Tuple[float, float]], eps_km=2.0, min_samples=2):
    n = len(points)
    labels = [-1] * n
    visited = [False] * n
    cluster_id = 0

    def region_query(i):
        return [j for j in range(n) if haversine_m(points[i][0], points[i][1],
                                                     points[j][0], points[j][1]) / 1000 <= eps_km]

    for i in range(n):
        if visited[i]:
            continue
        visited[i] = True
        neighbors = region_query(i)
        if len(neighbors) < min_samples:
            labels[i] = -1
            continue
        labels[i] = cluster_id
        seeds = [x for x in neighbors if x != i]
        while seeds:
            j = seeds.pop(0)
            if not visited[j]:
                visited[j] = True
                j_neighbors = region_query(j)
                if len(j_neighbors) >= min_samples:
                    for k in j_neighbors:
                        if k not in seeds and labels[k] == -1:
                            seeds.append(k)
            if labels[j] == -1:
                labels[j] = cluster_id
        cluster_id += 1
    return labels


# ============ 2-opt 优化接人顺序 ============
def two_opt(dm: DistanceMatrix, passenger_indices: List[int], dest_idx: int):
    if len(passenger_indices) <= 2:
        return passenger_indices
    best = passenger_indices[:]
    best_d, best_s = dm.route_distance(best, dest_idx)
    improved = True
    iters = 0
    while improved and iters < 50:
        improved = False
        iters += 1
        n = len(best)
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                new_order = best[:i] + best[i:j+1][::-1] + best[j+1:]
                new_d, new_s = dm.route_distance(new_order, dest_idx)
                if new_d < best_d - 1:
                    best = new_order
                    best_d = new_d
                    best_s = new_s
                    improved = True
    return best


# ============ 构建初始解 ============
def build_initial_solution(passengers: List[Passenger], dest: Passenger, dm: DistanceMatrix):
    """
    返回 List[Vehicle]
    流程：DBSCAN → 容量拆分 → 2-opt 每车顺序
    """
    points = [(p.lat, p.lng) for p in passengers]
    labels = dbscan(points, eps_km=2.0, min_samples=2)

    # 分组
    clusters = {}
    noise = []
    for i, p in enumerate(passengers):
        if labels[i] == -1:
            noise.append(i)
        else:
            clusters.setdefault(labels[i], []).append(i)

    # 容量拆分
    vehicle_indices = []
    for member_indices in clusters.values():
        remaining = list(member_indices)
        while remaining:
            group = remaining[:CAR_CAPACITY] if len(remaining) > CAR_CAPACITY else remaining
            vehicle_indices.append(group)
            remaining = remaining[len(group):]
    for i in noise:
        vehicle_indices.append([i])

    # 每车 2-opt 优化顺序
    vehicles = []
    dest_idx = len(passengers)  # 终点在矩阵最后一位
    for group in vehicle_indices:
        opt_order = two_opt(dm, group, dest_idx)
        d, s = dm.route_distance(opt_order, dest_idx)
        v = Vehicle(
            passengers=[passengers[i] for i in group],
            order=[passengers[i] for i in opt_order],
            distance_m=d,
            duration_s=s,
            cost=calc_cost(d)
        )
        vehicles.append(v)
    return vehicles


# ============ 评价函数（三种模式） ============
def evaluate(vehicles: List[Vehicle], baseline_cost: float, n_passengers: int, mode="cost"):
    total_cost = sum(v.cost for v in vehicles)
    total_duration = sum(v.duration_s for v in vehicles)
    vehicle_count = len(vehicles)

    if mode == "cost":
        # 字典序：费用 → 时间 → 车辆数
        return (total_cost, total_duration, vehicle_count)
    elif mode == "fewest_vehicles":
        return (vehicle_count, total_cost, total_duration)
    elif mode == "balanced":
        cost_norm = total_cost / baseline_cost if baseline_cost > 0 else total_cost
        time_norm = total_duration / (n_passengers * MAX_ROUTE_SECONDS) if n_passengers > 0 else 0
        vehicle_norm = vehicle_count / n_passengers if n_passengers > 0 else 0
        score = 0.5 * cost_norm + 0.3 * time_norm + 0.2 * vehicle_norm
        return (score, total_cost, total_duration, vehicle_count)
    return (total_cost, total_duration, vehicle_count)


# ============ ALNS ============
class ALNS:
    def __init__(self, vehicles: List[Vehicle], passengers: List[Passenger],
                 dest: Passenger, dm: DistanceMatrix,
                 baseline_cost: float, n_passengers: int,
                 mode="cost", time_budget_s=300):
        self.vehicles = vehicles
        self.passengers = passengers
        self.dest = dest
        self.dm = dm
        self.baseline_cost = baseline_cost
        self.n_passengers = n_passengers
        self.mode = mode
        self.time_budget = time_budget_s
        self.dest_idx = len(passengers)

        # 初始解
        self.best = [self._copy_vehicle(v) for v in vehicles]
        self.best_score = evaluate(self.best, baseline_cost, n_passengers, mode)
        self.current = [self._copy_vehicle(v) for v in vehicles]
        self.current_score = self.best_score

        # 模拟退火参数
        self.T0 = self.best_score[0] * 0.1 if isinstance(self.best_score[0], float) else 10.0
        self.T = self.T0
        self.alpha = 0.9995

    def _copy_vehicle(self, v):
        return Vehicle(
            passengers=list(v.passengers),
            order=list(v.order),
            distance_m=v.distance_m,
            duration_s=v.duration_s,
            cost=v.cost
        )

    def _passenger_index(self, p: Passenger):
        for i, pp in enumerate(self.passengers):
            if pp.id == p.id:
                return i
        return -1

    def _is_feasible(self, v: Vehicle):
        return v.duration_s <= MAX_ROUTE_SECONDS and len(v.passengers) <= CAR_CAPACITY

    def _rebuild_vehicle(self, passenger_list: List[Passenger]):
        """给定乘客列表，2-opt 优化顺序并算费用"""
        indices = [self._passenger_index(p) for p in passenger_list]
        indices = [i for i in indices if i >= 0]
        if not indices:
            return None
        opt = two_opt(self.dm, indices, self.dest_idx)
        d, s = self.dm.route_distance(opt, self.dest_idx)
        v = Vehicle(
            passengers=[self.passengers[i] for i in indices],
            order=[self.passengers[i] for i in opt],
            distance_m=d,
            duration_s=s,
            cost=calc_cost(d)
        )
        return v

    # ---- Destroy 算子 ----
    def destroy_random(self, k=3):
        """随机移除 k 个乘客"""
        all_p = [p for v in self.current for p in v.passengers]
        if len(all_p) <= k:
            return []
        removed = random.sample(all_p, k)
        self._remove_passengers(removed)
        return removed

    def destroy_worst(self, k=3):
        """移除费用贡献最大的乘客（让哪个人下车最省）"""
        # 简单实现：找费用最高的车，从里面随机移除
        sorted_v = sorted(self.current, key=lambda v: v.cost, reverse=True)
        removed = []
        for v in sorted_v:
            if len(removed) >= k:
                break
            if v.passengers:
                removed.append(random.choice(v.passengers))
        self._remove_passengers(removed)
        return removed

    def destroy_route(self):
        """随机清空一辆车"""
        if not self.current:
            return []
        v = random.choice(self.current)
        removed = list(v.passengers)
        self.current.remove(v)
        return removed

    def _remove_passengers(self, removed):
        removed_ids = {p.id for p in removed}
        new_vehicles = []
        for v in self.current:
            v.passengers = [p for p in v.passengers if p.id not in removed_ids]
            if v.passengers:
                # 重新算
                nv = self._rebuild_vehicle(v.passengers)
                if nv:
                    new_vehicles.append(nv)
        self.current = new_vehicles

    # ---- Repair 算子 ----
    def repair_greedy(self, removed: List[Passenger]):
        """贪心插入：每个被移除的乘客找费用增加最少的位置"""
        for p in removed:
            best_v = None
            best_increase = float('inf')
            best_new_v = None
            best_old_v = None

            # 尝试插入每辆车
            for v in self.current:
                if len(v.passengers) >= CAR_CAPACITY:
                    continue
                new_list = v.passengers + [p]
                nv = self._rebuild_vehicle(new_list)
                if nv and self._is_feasible(nv):
                    increase = nv.cost - v.cost
                    if increase < best_increase:
                        best_increase = increase
                        best_v = v
                        best_new_v = nv
                        best_old_v = v

            if best_v:
                # 替换
                idx = self.current.index(best_old_v)
                self.current[idx] = best_new_v
            else:
                # 单独开新车
                nv = self._rebuild_vehicle([p])
                if nv:
                    self.current.append(nv)

    # ---- 主循环 ----
    def run(self, progress_callback=None):
        start = time.time()
        iters = 0
        while time.time() - start < self.time_budget:
            iters += 1
            # 选择 Destroy
            destroy_type = random.choice(["random", "worst", "route"])
            if destroy_type == "random":
                removed = self.destroy_random(k=random.randint(1, 4))
            elif destroy_type == "worst":
                removed = self.destroy_worst(k=random.randint(1, 3))
            else:
                removed = self.destroy_route()

            if not removed:
                continue

            # Repair
            self.repair_greedy(removed)

            # 评价
            score = evaluate(self.current, self.baseline_cost, self.n_passengers, self.mode)

            # 模拟退火接受
            cur0 = score[0]
            best0 = self.best_score[0]
            delta = cur0 - best0
            if delta < 0 or random.random() < math.exp(-abs(delta) / max(self.T, 1e-6)):
                # 接受
                self.current_score = score
                if score < self.best_score:
                    self.best_score = score
                    self.best = [self._copy_vehicle(v) for v in self.current]
            else:
                # 回滚到 best
                self.current = [self._copy_vehicle(v) for v in self.best]

            self.T *= self.alpha

            if progress_callback and iters % 50 == 0:
                elapsed = time.time() - start
                progress_callback(elapsed, iters, self.best_score)

        return self.best


# ============ 入口函数 ============
def optimize(passengers: List[Passenger], dest: Passenger,
             dm: DistanceMatrix, mode="cost", time_budget_s=300, stage_cb=None):
    """
    主入口：给定乘客列表 + 终点 + 距离矩阵，返回优化后的车辆列表
    stage_cb: 可选回调，阶段变化时通知（"initial" / "alns"）
    """
    baseline_cost = sum(calc_cost(dm.get(i, len(passengers))[0]) for i in range(len(passengers)))

    # 初始解
    print("  构建初始解...")
    if stage_cb:
        stage_cb("initial")
    vehicles = build_initial_solution(passengers, dest, dm)
    print(f"  初始解: {len(vehicles)} 辆车")

    # ALNS 优化
    print(f"  启动 ALNS，预算 {time_budget_s} 秒...")
    if stage_cb:
        stage_cb("alns")
    alns = ALNS(vehicles, passengers, dest, dm, baseline_cost, len(passengers), mode, time_budget_s)
    best = alns.run()

    return best, baseline_cost
