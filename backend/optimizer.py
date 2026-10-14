# -*- coding: utf-8 -*-
"""
拼车路线优化算法 V1.1
=====================
支持：
- 距离源：高德/百度真实矩阵 / Haversine 模拟（由 map_provider 选择与降级）
- 算法流程：DBSCAN 聚类 → 容量拆分 → 噪声点并入 → 贪心合并 → ALNS 优化 → 2-opt 接人顺序
- 两种优化模式：cost（费用最优）/ fewest_vehicles（车数最少，两阶段求解）
  （V1.1 原设计的 balanced 加权和模式已于 P1 验证后删除：见 evaluate 注释）
- 可选约束：max_vehicles（车数上限硬约束）
- 费用函数：C(D) = 16 + 3 * max(D_km - 3, 0)
- 超时约束：单车路线 ≤ 5400 秒

P1 验证结论（详见 v1.1/P1 ALNS 真实收益验证报告）：
- ALNS 相对初始解降本约 48%，且对初始解质量不敏感（换种子终值差 <2%）
- 退火降温由「按迭代」改为「按时间」，避免长预算下 T 崩到 0 退化为纯爬山
- fewest_vehicles 改两阶段：先定最少车数，再在车数上限约束下压费用
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

# 绕路预警阈值（业务需求 §第五节）：满足任一即标注「明显绕路」
DETOUR_RIDE_LIMIT_S = 80 * 60   # 实际乘车时长 > 80 分钟
DETOUR_RATIO_LIMIT = 1.3        # 绕行比例 ride / direct > 1.3
DETOUR_WARNING = "DETOUR_WARNING"


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


def allocate_cost(total_cost: float, n: int) -> List[float]:
    """
    本车费用按同乘人数平摊，返回与「接人顺序」同序的分摊金额列表。

    规则（设计文档《技术方案设计 V1.1》§十一，确定性、可复现、可审计）：
      1. 本车费用先按全精度除以同乘人数，得到每人精确分摊值；
      2. 展示时统一四舍五入到 2 位小数；
      3. 四舍五入后产生的尾差由**接人顺序排在第一位的乘客**承担。

    例：100 元 / 3 人 → [33.34, 33.33, 33.33]（第一位承担 +0.01）
    不变式：sum(返回值) 恒等于 round(total_cost, 2)，即分摊不产生金额损失或凭空多出。

    注：散场模式下并无「上车顺序」（所有人在活动点同时上车），
    此时「第一位」取沿最终路线的访问顺序第一位（即第一位被送达的乘客），
    保持规则确定性，详见 v1.1/A1 费用分摊与个人乘车明细实现说明.md。
    """
    if n <= 0:
        return []
    target = round(float(total_cost), 2)
    exact = target / n
    base = round(exact, 2)
    alloc = [base] * n
    # 尾差 = 目标总额 − 已分配总额（四舍五入带来，通常 ±0.01 量级）
    remainder = round(target - base * n, 2)
    alloc[0] = round(base + remainder, 2)
    return alloc


def detour_warning(ride_s: float, direct_s: float) -> Optional[str]:
    """绕路预警：实际乘车 > 80 分钟，或绕行比例 > 1.3（任一满足即预警）"""
    if ride_s > DETOUR_RIDE_LIMIT_S:
        return DETOUR_WARNING
    if direct_s > 0 and ride_s / direct_s > DETOUR_RATIO_LIMIT:
        return DETOUR_WARNING
    return None


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
    流程：DBSCAN → 容量拆分 → 噪声点并入 → 2-opt 每车顺序

    噪声点处理（P1 修复）：原实现把每个噪声点单独开一车，在 eps=2km / 乘客抖动
    ±2.2km 的条件下平均产生 16.5 车 / 28.5 人（合理值约 5 车）。现改为把噪声点按
    「插入后里程增量最小」并入有余量的既有车辆，无处可插时才单点成车。
    注意：这只改善过程指标（进度条中间态、前期迭代效率），不改变最终质量——
    P1 已验证 ALNS 对初始解不敏感（换种子终值差 <2%）。
    """
    points = [(p.lat, p.lng) for p in passengers]
    labels = dbscan(points, eps_km=2.0, min_samples=2)
    dest_idx = len(passengers)  # 终点在矩阵最后一位

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

    # 噪声点并入：按插入造成的里程增量最小挑选目标车辆
    group_d = [dm.route_distance(two_opt(dm, g, dest_idx), dest_idx)[0]
               for g in vehicle_indices]
    for i in noise:
        best_gi, best_delta, best_order = -1, float('inf'), None
        for gi, g in enumerate(vehicle_indices):
            if len(g) >= CAR_CAPACITY:
                continue
            order = two_opt(dm, g + [i], dest_idx)
            delta = dm.route_distance(order, dest_idx)[0] - group_d[gi]
            if delta < best_delta:
                best_delta, best_gi, best_order = delta, gi, order
        if best_gi >= 0:
            vehicle_indices[best_gi] = best_order
            group_d[best_gi] = dm.route_distance(best_order, dest_idx)[0]
        else:
            # 所有车都满员，只能单点成车
            vehicle_indices.append([i])
            group_d.append(dm.route_distance([i], dest_idx)[0])

    # 每车 2-opt 优化顺序
    vehicles = []
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


# ============ 评价函数（字典序，越小越好） ============
# 车数超上限时的罚分：必须大过任何真实目标差异，保证「超约束解」永不优于合理解
INFEASIBLE_PENALTY = 1e6


def evaluate(vehicles: List[Vehicle], baseline_cost: float, n_passengers: int,
             mode="cost", max_vehicles=None):
    """
    cost            : (总费用, 总时长, 车数)
    fewest_vehicles : (车数, 总费用, 总时长)
    max_vehicles    : 车数上限硬约束；超出一辆就在首元素上加一个罚分。

    关于 V1.1 的 balanced（加权和）模式已被删除：P1 实测其权重 0.5/0.3/0.2 因
    time_norm / vehicle_norm 分母极大，使 cost 项占最终 score 约 76%，产出与 cost
    无系统差异（15 实例平均 +1.00% 费用），属冗余模式。
    真正的「少用车」需求请用 fewest_vehicles 或 max_vehicles 约束表达。
    """
    total_cost = sum(v.cost for v in vehicles)
    total_duration = sum(v.duration_s for v in vehicles)
    vehicle_count = len(vehicles)
    over = 0
    if max_vehicles is not None and vehicle_count > max_vehicles:
        over = vehicle_count - max_vehicles

    if mode == "fewest_vehicles":
        return (vehicle_count + over * INFEASIBLE_PENALTY, total_cost, total_duration)
    # 默认 cost
    return (total_cost + over * INFEASIBLE_PENALTY, total_duration, vehicle_count)


# ============ ALNS ============
class ALNS:
    def __init__(self, vehicles: List[Vehicle], passengers: List[Passenger],
                 dest: Passenger, dm: DistanceMatrix,
                 baseline_cost: float, n_passengers: int,
                 mode="cost", time_budget_s=300, trace=False,
                 T0=None, T_end_ratio=0.35, max_vehicles=None, cooling="time",
                 alpha=None, two_phase=None, phase1_ratio=0.4):
        """
        T0           : 退火初温；None 时按模式显式推定（见 _default_T0）
        T_end_ratio  : 按时间降温时，预算末的 T / T0。0.35 取自 P1 实测——当前调度在
                       4s 预算下恰好衰减到 T0 的 37%~73%，是已验证有效的温度区间
        cooling      : "time"（默认，按预算时长几何降温）| "iter"（旧的按迭代降温，仅对照）
        max_vehicles : 车数上限硬约束
        two_phase    : fewest_vehicles 默认启用两阶段（先定车数，再在同车数下压费用）；
                       显式传 False 可关闭
        """
        self.trace = [] if trace else None  # 诊断用：记录收敛轨迹，默认关闭不影响行为
        self.vehicles = vehicles
        self.passengers = passengers
        self.dest = dest
        self.dm = dm
        self.baseline_cost = baseline_cost
        self.n_passengers = n_passengers
        self.mode = mode
        self.time_budget = time_budget_s
        self.dest_idx = len(passengers)
        self.max_vehicles = max_vehicles
        self.cooling = cooling
        self.alpha = 0.9995 if alpha is None else alpha
        self.T_end_ratio = T_end_ratio
        self.T0_override = T0
        self.phase1_ratio = phase1_ratio
        self.two_phase = (mode == "fewest_vehicles" and max_vehicles is None
                          and two_phase is not False)

        # 初始解
        self.best = [self._copy_vehicle(v) for v in vehicles]
        self.best_score = evaluate(self.best, baseline_cost, n_passengers, mode, max_vehicles)
        self.current = [self._copy_vehicle(v) for v in vehicles]
        self.current_score = self.best_score
        self.stats = {}

    def _default_T0(self, score, mode, max_vehicles=None):
        """
        退火温度的量纲必须与「模式主目标的典型单步变化量 δ」匹配。

        P1 实测：cost 模式单步 δ ≈ 10~13 元（初始总分约 350 元），即 δ ≈ 0.03×总分。
        取 T0 = 0.1×总分 → Δ/T ≈ 0.15~0.3，劣解接受概率约 0.7~0.85，既不锁死也不乱走。
        fewest_vehicles 主目标是车数、单步 δ = 1 辆，故 T0 取 10。

        原实现写作 `score[0] * 0.1 if isinstance(score[0], float) else 10.0`，
        用 Python 运行时类型来判量纲：cost/balanced/fewest 分别落到 80元 / 0.06 / 10.0
        三个互不相关的尺度，属「意外正确」。现改为按模式显式给定。

        注意：传入的 score 必须**不含 INFEASIBLE_PENALTY**，否则初始解一旦违反
        max_vehicles，T0 会被算成 1e5 量级，退火立刻退化为随机游走（已在 A/B 中发现）。
        """
        if mode == "fewest_vehicles":
            return 10.0
        return max(float(score[0]), 1.0) * 0.1

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

    def repair_greedy(self, removed: List[Passenger]):
        """贪心插入：每个被移除的乘客找费用增加最少的位置；无处可插则单点成车"""
        for p in removed:
            v, nv = self._best_insertion(p)
            if v is not None:
                self._replace_vehicle(v, nv)
            else:
                nv = self._rebuild_vehicle([p])
                if nv:
                    self.current.append(nv)

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
    def _replace_vehicle(self, old, new):
        """按对象身份替换车辆（不能用 list.index，Vehicle 是 dataclass，== 会按值匹配）"""
        for i, v in enumerate(self.current):
            if v is old:
                self.current[i] = new
                return True
        return False

    def _best_insertion(self, p: Passenger):
        """在所有有余量的车里找「费用增量最小」的插入位。返回 (目标车, 新车)，无解返回 (None, None)"""
        best_v = best_new = None
        best_inc = float('inf')
        for v in self.current:
            if len(v.passengers) >= CAR_CAPACITY:
                continue
            nv = self._rebuild_vehicle(v.passengers + [p])
            if nv and self._is_feasible(nv):
                inc = nv.cost - v.cost
                if inc < best_inc:
                    best_inc, best_v, best_new = inc, v, nv
        return best_v, best_new

    def _energy(self, vehicles: List[Vehicle], mode: str) -> float:
        """
        退火接受门用的标量「能量」——只反映模式主目标，**不含任何硬约束罚分**。

        为什么必须与 evaluate 的字典序分数分开：
        max_vehicles 的罚分是 1e6 量级（为了在排序上压倒一切真实差异），若直接拿它当
        接受门的 delta，任何「车数超限」的候选都会得到 delta≈1e6 → exp(-1e6/T)≈0 →
        一律拒绝。而当所有车都满载时（如 24 人 / 4 车 × 6 人），destroy 之后
        repair_greedy 无处可插、只能新开一辆车 → 候选必然超限 → 于是**每一次迭代都被
        拒绝，搜索彻底冻结**（实测 30 秒 23902 次迭代 0 次改进、结果恒等于初始解）。

        改为：接受门只看费用（或车数），允许 current 在超限状态间穿行；best 仍按
        字典序（含罚分）追踪，因此**返回的解一定是可行解**（只要初始解可行）。
        """
        if mode == "fewest_vehicles":
            return float(len(vehicles))
        return float(sum(v.cost for v in vehicles))

    # ---- 单阶段搜索 ----
    def _run_phase(self, phase_budget, mode, max_vehicles, time_offset,
                   progress_callback=None, phase_name="main"):
        start = time.time()
        iters = 0
        accept_worse = 0
        # T0 用「去掉罚分」的能量来定标：初始解若违反 max_vehicles，self.best_score 里
        # 含着 1e6 罚分，直接拿它算温度会把 T0 放大到 1e5 量级（退火失效）
        best_energy = self._energy(self.best, mode)
        if self.T0_override is not None:
            T0 = self.T0_override
        else:
            T0 = self._default_T0((best_energy,), mode)
        T = T0
        while True:
            elapsed = time.time() - start
            if elapsed >= phase_budget:
                break
            iters += 1

            # 降温。默认按「时间」几何降温：T 在本阶段预算内由 T0 衰减到 T0*T_end_ratio，
            # 保证任何预算下都能真正走完降温曲线。
            # 旧的按迭代降温（α=0.9995）在长预算下会失控：60s 约 2.5 万次迭代 →
            # T 只剩 T0 的 3e-6，退火退化为纯爬山，而 P1 实测纯爬山是最差变体。
            if self.cooling == "time":
                frac = min(elapsed / phase_budget, 1.0) if phase_budget > 0 else 1.0
                T = T0 * (self.T_end_ratio ** frac)
            else:
                T = max(T * self.alpha, 1e-9)

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

            # 评价：score 用于 best 追踪（字典序，含硬约束罚分），
            # energy 用于退火接受门（只看主目标，不含罚分——见 _energy 注释）
            score = evaluate(self.current, self.baseline_cost, self.n_passengers,
                             mode, max_vehicles)
            energy_now = self._energy(self.current, mode)

            # 模拟退火接受（δ 相对当前最优，按能量比较）
            delta = energy_now - best_energy
            if delta < 0 or random.random() < math.exp(-abs(delta) / max(T, 1e-6)):
                # 接受
                if delta > 0:
                    accept_worse += 1
                self.current_score = score
                if score < self.best_score:
                    self.best_score = score
                    best_energy = energy_now
                    self.best = [self._copy_vehicle(v) for v in self.current]
                    if self.trace is not None:
                        self.trace.append({
                            "t": round(time_offset + time.time() - start, 3), "iter": iters,
                            "phase": phase_name,
                            "best0": score[0], "T": round(T, 6), "reason": "improve",
                        })
            else:
                # 回滚到 best
                self.current = [self._copy_vehicle(v) for v in self.best]

            if progress_callback and iters % 50 == 0:
                progress_callback(time_offset + time.time() - start, iters, self.best_score)

        return {"name": phase_name, "mode": mode, "max_vehicles": max_vehicles,
                "iters": iters, "elapsed": round(time.time() - start, 3),
                "accept_worse": accept_worse, "T0": T0, "T_end": T}

    # ---- 主循环 ----
    def run(self, progress_callback=None):
        total_start = time.time()

        if self.two_phase:
            # 阶段 1：车数优先，先把「最少几辆车」定下来
            p1_budget = self.time_budget * self.phase1_ratio
            ph1 = self._run_phase(p1_budget, "fewest_vehicles", None, 0.0,
                                  progress_callback, "fewest_vehicles")
            n_veh = len(self.best)

            # 阶段 2：以阶段 1 的最优解为起点，在「车数 ≤ n_veh」硬约束下最小化费用。
            # 这一步回补了旧 fewest_vehicles 的费用劣势——旧实现的字典序虽然也是
            # (车数, 费用, 时长)，但受搜索轨迹影响，在车数相同时费用平均高 10.59%。
            self.current = [self._copy_vehicle(v) for v in self.best]
            self.best_score = evaluate(self.best, self.baseline_cost, self.n_passengers,
                                       "cost", n_veh)
            self.current_score = self.best_score
            ph2 = self._run_phase(self.time_budget - p1_budget, "cost", n_veh,
                                  p1_budget, progress_callback, "cost_under_cap")
            phases = [ph1, ph2]
        else:
            phases = [self._run_phase(self.time_budget, self.mode, self.max_vehicles,
                                      0.0, progress_callback, self.mode)]

        last = phases[-1]
        self.stats = {
            "iters": sum(p["iters"] for p in phases),
            "elapsed": round(time.time() - total_start, 3),
            "accept_worse": sum(p["accept_worse"] for p in phases),
            "T0": last["T0"], "T_end": last["T_end"],
            "cooling": self.cooling, "alpha": self.alpha,
            "two_phase": self.two_phase, "phases": phases,
        }
        return self.best


# ============ 入口函数 ============
def optimize(passengers: List[Passenger], dest: Passenger,
             dm: DistanceMatrix, mode="cost", time_budget_s=300, stage_cb=None,
             diag: Optional[dict] = None, max_vehicles: Optional[int] = None):
    """
    主入口：给定乘客列表 + 终点 + 距离矩阵，返回优化后的车辆列表
    stage_cb: 可选回调，阶段变化时通知（"initial" / "alns"）
    diag: 可选诊断字典，传入后会填入 initial_score/stats/trace（用于收益验证，默认 None 无副作用）
    max_vehicles: 车数上限硬约束（None 表示不限）
    """
    n_p = len(passengers)
    # 容量下限：每车最多 CAR_CAPACITY 人，低于此值的上限不可能满足，直接上调并提示
    if max_vehicles is not None:
        capacity_min = -(-n_p // CAR_CAPACITY) if n_p else 0
        if max_vehicles < capacity_min:
            print(f"  [约束] max_vehicles={max_vehicles} 低于容量下限 {capacity_min}，已上调")
            if diag is not None:
                diag["capacity_clamped"] = {"requested": max_vehicles, "raised_to": capacity_min}
            max_vehicles = capacity_min

    baseline_cost = sum(calc_cost(dm.get(i, n_p)[0]) for i in range(n_p))

    # 初始解
    print("  构建初始解...")
    if stage_cb:
        stage_cb("initial")
    vehicles = build_initial_solution(passengers, dest, dm)
    print(f"  初始解: {len(vehicles)} 辆车")

    if diag is not None:
        diag["initial_score"] = evaluate(vehicles, baseline_cost, len(passengers), mode,
                                         max_vehicles)
        diag["initial_vehicles"] = len(vehicles)
        diag["baseline_cost"] = baseline_cost

    # ALNS 优化
    print(f"  启动 ALNS，预算 {time_budget_s} 秒...")
    if stage_cb:
        stage_cb("alns")
    alns = ALNS(vehicles, passengers, dest, dm, baseline_cost, len(passengers), mode,
                time_budget_s, trace=(diag is not None), max_vehicles=max_vehicles)
    best = alns.run()

    if diag is not None:
        diag["stats"] = getattr(alns, "stats", {})
        diag["trace"] = alns.trace or []
        diag["final_score"] = evaluate(best, baseline_cost, len(passengers), mode, max_vehicles)

    # 上限没满足（常见于 5400 秒单车时长约束与地理分散共同作用），如实提示
    if max_vehicles is not None and len(best) > max_vehicles:
        print(f"  提示：未能在 {max_vehicles} 辆车内满足全部约束（实际 {len(best)} 辆），"
              f"已返回搜索到的最优解")
        if diag is not None:
            diag["constraint_missed"] = {"requested": max_vehicles, "actual": len(best)}

    return best, baseline_cost
