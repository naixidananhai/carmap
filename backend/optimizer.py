# -*- coding: utf-8 -*-
"""
拼车路线优化算法 V1.1
=====================
支持：
- 距离源：高德/百度真实矩阵 / Haversine 模拟（由 map_provider 选择与降级）
- 算法流程：DBSCAN 聚类 → 容量拆分 → 噪声点并入 → 贪心合并 → ALNS 优化 → 2-opt 接人顺序
- 两种优化模式：cost（费用最优）/ fewest_vehicles（车数最少，两阶段求解）
  （V1.1 原设计的 balanced 加权和模式已于 P1 验证后删除：见 evaluate 注释）
- 可选约束：max_vehicles（车数上限硬约束）、vehicle_pref（A2 车型 4/6 座）
- 费用函数：C(D) = 16 + 3 * max(D_km - 3, 0)   ← 不含车型项（两种车型同价）
- 超时约束：单车路线 ≤ 5400 秒

A2 车型（4 座 / 6 座）实现要点：
- 车型是**可行性变量**不是成本变量：起步价与里程单价对 4/6 座完全相同；
- 「优先 6 人」落在**分组**上（按 6 座装填 → 车数最少 → 起步价最省），
  而非给 4 人的车贴 6 座标签；
- 解定稿后按实际载客数派车：≤4 人 → 4 座，5~6 人 → 6 座（硬规则）；
- 新增 destroy_over_time 定向拆车算子，专治「初始解自带超时车」。

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

# ---- A2：车型（4 座 / 6 座混用）----
# 车型**不参与费用函数**（见 calc_cost）：两种车型起步价与里程单价完全相同，
# 因此「车型」是纯可行性变量——它只决定「一辆车最多能坐几人」。
#   · 4 座：载客 ≤4 时够用
#   · 6 座：载客 5~6 时唯一可行
# 真正的成本杠杆是**车数**（每辆车 16 元起步价），所以分组一律先按 6 座装填
# （=「优先 6 人」），只有 5400 秒硬约束逼着拆车时才会出现载客 ≤4 的车。
CAR_CAPACITY_4 = 4
CAR_CAPACITY_6 = 6
CAR_CAPACITY = CAR_CAPACITY_6  # 最大容量（历史别名，等价于「不限车型时的上限」）

# 车型偏好（对应前端「车型」三选一）：
#   auto —— 自动：搜索按 6 座装填，最终按实际载客数配「最小够用」车型
#           （≤4 人 → 4 座，5~6 人 → 6 座）。默认值。
#   six  —— 仅 6 座：容量上限 6，且所有车一律标 6 座（适合车队只有 6 座车）
#   four —— 仅 4 座：容量上限降到 4（适合车队只有 4 座车，会自动多派车）
VEHICLE_PREF_AUTO = "auto"
VEHICLE_PREF_SIX = "six"
VEHICLE_PREF_FOUR = "four"
VEHICLE_PREFS = (VEHICLE_PREF_AUTO, VEHICLE_PREF_SIX, VEHICLE_PREF_FOUR)

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


# ============ A2 车型（4 座 / 6 座）============
# 设计要点：车型**不是搜索状态**，而是解的导出属性。
#   · 搜索期只关心「容量上限 capacity_limit(pref)」，保证任何车都不超载；
#   · 解定稿后再由 assign_vehicle_type 按每辆车实际载客数派车型。
# 这样 ALNS 的搜索空间与 A2 之前完全一致（同一批可行解），
# 区别只在「输出给调度的是几座车」，避免把纯标签问题变成搜索问题。

def normalize_vehicle_pref(pref: Optional[str]) -> str:
    """把外部传入的车型偏好归一到 auto / six / four。未知值回退 auto。"""
    p = (pref or VEHICLE_PREF_AUTO).strip().lower()
    if p in VEHICLE_PREFS:
        return p
    print(f"  [车型] {pref!r} 未知，回退默认 {VEHICLE_PREF_AUTO!r}")
    return VEHICLE_PREF_AUTO


def capacity_limit(pref: str = VEHICLE_PREF_AUTO) -> int:
    """
    该车型偏好下车容量上限（分组与可行性判定用）：

    - auto / six → 6：先按 6 座装填（「优先 6 人」，车数最少 = 起步价最省）；
    - four      → 4：车队只有 4 座车，装载上限直接降到 4（会自动多派车）。
    """
    return CAR_CAPACITY_4 if pref == VEHICLE_PREF_FOUR else CAR_CAPACITY_6


def assign_vehicle_type(passenger_count: int, pref: str = VEHICLE_PREF_AUTO) -> int:
    """
    给「实际载客 passenger_count 人」的一辆车派车型（4 或 6）。

    硬规则（《技术方案 V1.1》第二步「容量拆分」）：
      · 5~6 人 → 只能 6 座（4 座装不下）；
      · 1~4 人 → 4 / 6 座都合法，具体看车型偏好。

    两种车型计费完全相同，故「派哪档」不改变费用，只影响可执行的派车单：
      · auto：按「最小够用」派车 —— ≤4 人给 4 座（不占 6 座资源），5~6 人给 6 座；
      · six ：一律 6 座（车队只有 6 座车时使用）；
      · four：只会出现 4 座（容量上限已被 capacity_limit 压到 4）。

    注：「优先 6 人」的实质在**分组**（按 6 装填以减少车数），而非给 4 人的车
    贴 6 座标签——《算法详细设计 V1.1》§2 亦明确「4 人车型或 6 人车型成本完全
    一样，随便选」。故 auto 下取信息量更大的「最小够用」派车口径。
    """
    if passenger_count > CAR_CAPACITY_4:      # 5~6 人
        return CAR_CAPACITY_6
    if passenger_count <= 0:
        return CAR_CAPACITY_6
    return CAR_CAPACITY_6 if pref == VEHICLE_PREF_SIX else CAR_CAPACITY_4


def annotate_vehicle_types(vehicles: List[Vehicle],
                           pref: str = VEHICLE_PREF_AUTO) -> List[int]:
    """解定稿后，按每辆车实际载客数批量派车型。返回与 vehicles 同序的车型列表。"""
    return [assign_vehicle_type(len(v.passengers), pref) for v in vehicles]


def count_vehicle_types(types: List[int]) -> Tuple[int, int]:
    """统计 (4 座数量, 6 座数量)。"""
    four = sum(1 for t in types if t == CAR_CAPACITY_4)
    return four, len(types) - four


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
def build_initial_solution(passengers: List[Passenger], dest: Passenger, dm: DistanceMatrix,
                           capacity: int = CAR_CAPACITY):
    """
    返回 List[Vehicle]
    流程：DBSCAN → 容量拆分 → 噪声点并入 → 2-opt 每车顺序

    capacity（A2）：分组上限。auto/six → 6（「优先 6 人」：14 人 → 6+6+2 而不是
    4+4+4+2，少一辆车就少 16 元起步价）；four → 4（车队只有 4 座车）。

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

    # 容量拆分：按 6（或 4）座切分，先装满再开下一辆
    vehicle_indices = []
    for member_indices in clusters.values():
        remaining = list(member_indices)
        while remaining:
            group = remaining[:capacity] if len(remaining) > capacity else remaining
            vehicle_indices.append(group)
            remaining = remaining[len(group):]

    # 噪声点并入：按插入造成的里程增量最小挑选目标车辆
    group_d = [dm.route_distance(two_opt(dm, g, dest_idx), dest_idx)[0]
               for g in vehicle_indices]
    for i in noise:
        best_gi, best_delta, best_order = -1, float('inf'), None
        for gi, g in enumerate(vehicle_indices):
            if len(g) >= capacity:
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


def vehicle_feasible(v: Vehicle, capacity: int = CAR_CAPACITY) -> bool:
    """单车硬约束：载客数 ≤ 车型容量上限（A2：auto/six=6，four=4），行程 ≤ 5400 秒。"""
    return v.duration_s <= MAX_ROUTE_SECONDS and len(v.passengers) <= capacity


def solution_feasible(vehicles: List[Vehicle], capacity: int = CAR_CAPACITY) -> bool:
    """整解可行性：每辆车都满足硬约束，且没有空车。"""
    return bool(vehicles) and all(vehicle_feasible(v, capacity) for v in vehicles)


def lower_bound_cost(direct_m: List[float], capacity: int = CAR_CAPACITY) -> float:
    """
    总费用的**严格下界**（A8：gap 的分母；宁可松，不可错）。

    由两个各自可证明成立的下界取大者：

    (1) 人均直达摊派界
        任取一车 v，其路线为 h1→h2→…→hk→活动点，里程 D_v。
        对车内任一乘客 j，子路径 h_j→…→活动点 的长度 ≤ D_v（各段非负），
        由三角不等式又有 d(h_j, 活动点) ≤ 该子路径长度，故 d_j ≤ D_v。
        车内最多 capacity 人 ⇒ Σ_{j∈G_v} d_j ≤ capacity·D_v。
        对所有车求和：Σ_j d_j ≤ capacity·Σ_v D_v
        ⇒ Σ_v D_v ≥ (Σ_j d_j) / capacity。

    (2) 按容量分组的 max 界
        任意一车的里程 ≥ 车内最远那位乘客的直达距离，
        即 D_v ≥ max_{j∈G_v} d_j。把乘客按直达距离降序排为 d_(1)≥d_(2)≥…，
        对第 i 组：前 capacity·(i−1)+1 个乘客（值均 ≥ d_(capacity·(i−1)+1)）
        最多只能塞满 i−1 辆车，故至少 i 辆车里各有一位乘客的直达距离
        ≥ d_(capacity·(i−1)+1)，即第 i 大的「车里程」≥ 该值。
        于是 Σ_v D_v ≥ Σ_{i=1..ceil(n/capacity)} d_(capacity·(i−1)+1)。

    再把「车数 ≥ ceil(n/capacity)」与「费用对里程、车数均单调不减」代入
    费用函数 C = BASE_FEE·k + PER_KM_FEE·Σ_v max(D_v,km − FREE_KM, 0)：

        C ≥ BASE_FEE·k_min + PER_KM_FEE·max(S_lb/1000 − FREE_KM·k_min, 0)

    有效性依赖两个单调性：把 ΣD 与 k 各自放大，C 不会变小
    （Σ_v max(D_v−3, 0) ≥ max(Σ_v D_v − 3k, 0) ≥ max(S_lb − 3·k_min, 0)）。

    说明：该下界不含单车 5400 秒时长约束，也不含 max_vehicles 约束——
    这两个约束只会让真实最优更贵，故对下界无影响（下界仍有效）。
    代价是**偏松**：实测本项目 15 人实例 gap ≈ 55%~65%，因此 STOP_GAP
    在正常规模下不会触发（详见 v1.1/A7-A8 实现说明.md §4）。

    capacity（A2）：必须传**实际生效的容量上限**（capacity_limit(pref)）——
    推导每一步都用「车内最多 capacity 人」，容量变小推导依然成立且界更紧。
    four 模式（上限 4）下 k_min 更大、下界更紧，gap 会相应收窄。
    """
    n = len(direct_m)
    if n <= 0:
        return 0.0
    k_min = -(-n // capacity)                      # ceil(n / capacity)
    total = sum(direct_m)

    lb1 = total / capacity
    desc = sorted(direct_m, reverse=True)
    lb2 = sum(desc[capacity * i] for i in range(k_min) if capacity * i < n)

    s_lb = max(lb1, lb2)
    km = s_lb / 1000.0
    return BASE_FEE * k_min + PER_KM_FEE * max(km - FREE_KM * k_min, 0.0)


def gap_ratio(best_cost: float, lb: Optional[float]) -> Optional[float]:
    """相对下界的差距（0.03 = 3%）。下界缺失或非正时返回 None。"""
    if lb is None or lb <= 0:
        return None
    return max(0.0, (best_cost - lb) / lb)


def evaluate(vehicles: List[Vehicle], baseline_cost: float, n_passengers: int,
             mode="cost", max_vehicles=None, capacity: int = CAR_CAPACITY):
    """
    cost            : (总费用, 总时长, 车数)
    fewest_vehicles : (车数, 总费用, 总时长)
    max_vehicles    : 车数上限硬约束；超出一辆就在首元素上加一个罚分。
    单车时长        : 超过 MAX_ROUTE_SECONDS 的车按辆数计罚分（与上限同理）。
    单车容量        : 载客超 capacity 的车按辆数计罚分（A2；与时长同理）。

    关于 V1.1 的 balanced（加权和）模式已被删除：P1 实测其权重 0.5/0.3/0.2 因
    time_norm / vehicle_norm 分母极大，使 cost 项占最终 score 约 76%，产出与 cost
    无系统差异（15 实例平均 +1.00% 费用），属冗余模式。
    真正的「少用车」需求请用 fewest_vehicles 或 max_vehicles 约束表达。

    关于时长罚分（A8 修复）：原先只有 `_best_insertion` 检查单车时长，
    初始解（DBSCAN + 2-opt，不过时长）与最终结果都不检查，导致**超时路线被当作
    成功方案返回**（实测散场模式出现 5984 秒 > 5400 秒上限仍报成功）。
    现在把违例计入排序分，搜索才有动力把超时路线拆掉。

    关于容量罚分（A2）：搜索期构造路径（_best_insertion / 分组）已保证不超载，
    此处只是与时长同源的兜底——一旦任何算子将来漏检，排序分立刻能感知到。
    """
    total_cost = sum(v.cost for v in vehicles)
    total_duration = sum(v.duration_s for v in vehicles)
    vehicle_count = len(vehicles)
    over = 0
    if max_vehicles is not None and vehicle_count > max_vehicles:
        over = vehicle_count - max_vehicles
    over_dur = sum(1 for v in vehicles if v.duration_s > MAX_ROUTE_SECONDS)
    over_cap = sum(1 for v in vehicles if len(v.passengers) > capacity)
    pen = (over + over_dur + over_cap) * INFEASIBLE_PENALTY

    if mode == "fewest_vehicles":
        return (vehicle_count + pen, total_cost, total_duration)
    # 默认 cost
    return (total_cost + pen, total_duration, vehicle_count)


# ============ ALNS ============
class ALNS:
    def __init__(self, vehicles: List[Vehicle], passengers: List[Passenger],
                 dest: Passenger, dm: DistanceMatrix,
                 baseline_cost: float, n_passengers: int,
                 mode="cost", time_budget_s=300, trace=False,
                 T0=None, T_end_ratio=0.35, max_vehicles=None, cooling="time",
                 alpha=None, two_phase=None, phase1_ratio=0.4,
                 cost_lb=None, gap_stop_pct=None, stable_s=None,
                 capacity_limit: int = CAR_CAPACITY):
        """
        T0           : 退火初温；None 时按模式显式推定（见 _default_T0）
        T_end_ratio  : 按时间降温时，预算末的 T / T0。0.35 取自 P1 实测——当前调度在
                       4s 预算下恰好衰减到 T0 的 37%~73%，是已验证有效的温度区间
        cooling      : "time"（默认，按预算时长几何降温）| "iter"（旧的按迭代降温，仅对照）
        max_vehicles : 车数上限硬约束
        two_phase    : fewest_vehicles 默认启用两阶段（先定车数，再在同车数下压费用）；
                       显式传 False 可关闭
        cost_lb      : 费用下界（见 lower_bound_cost）。仅当给出且 gap_stop_pct 不为 None
                       时才启用 gap 提前终止
        gap_stop_pct : gap 提前终止阈值，小数（0.05 = 5%）。None 表示不启用
        stable_s     : 「连续无改善」提前终止的窗口（秒）。None 表示不启用。
                       只作用于**最后一个阶段**，避免两阶段搜索的第 1 阶段被提前打断
        capacity_limit: 单车载客上限（A2）。auto/six → 6，four → 4。
                       只影响可行性判定，不影响费用（车型不参与费用函数）
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
        self.capacity_limit = capacity_limit   # A2：单车载客上限（4 或 6）
        self.cooling = cooling
        self.alpha = 0.9995 if alpha is None else alpha
        self.T_end_ratio = T_end_ratio
        self.T0_override = T0
        self.phase1_ratio = phase1_ratio
        self.two_phase = (mode == "fewest_vehicles" and max_vehicles is None
                          and two_phase is not False)

        # ---- A8：终止判定所需的输入 ----
        self.cost_lb = cost_lb
        self.gap_stop_pct = gap_stop_pct
        self.stable_s = stable_s
        self.stop = None      # {'reason','detail','elapsed','gap'}，由 _run_phase 填

        # 初始解
        self.best = [self._copy_vehicle(v) for v in vehicles]
        self.best_score = evaluate(self.best, baseline_cost, n_passengers, mode, max_vehicles,
                                   capacity_limit)
        self.current = [self._copy_vehicle(v) for v in vehicles]
        self.current_score = self.best_score
        self.stats = {}

    def _maybe_stop(self, mode, allow_early_stop, iters, last_improve_t):
        """
        判断是否提前终止（A8）。返回终止原因字符串或 None。

        - gap ≤ 阈值：仅在**费用为主要目标**的阶段判定。fewest_vehicles 的目标是车数，
          费用下界对该目标没有意义（且 k_min 并非受 5400 秒约束下的有效下界），
          因此不做任何最优性声明，宁可让它跑到预算用尽。
          gap 需遍历 best 求和，故每 50 次迭代才算一次（停止判定不需要毫秒级精度）。
        - 连续无改善：只在最后一个阶段判定（allow_early_stop），
          否则两阶段搜索的第 1 阶段可能刚定完车数就退出、phase 2 根本没机会跑。
        """
        if allow_early_stop and self.stable_s is not None:
            if time.time() - last_improve_t >= self.stable_s:
                return "STOP_STABLE"
        if (self.gap_stop_pct is not None and self.cost_lb
                and mode == "cost" and iters % 50 == 0):
            g = gap_ratio(sum(v.cost for v in self.best), self.cost_lb)
            if g is not None and g <= self.gap_stop_pct:
                return "STOP_GAP"
        return None

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
        return vehicle_feasible(v, self.capacity_limit)

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

    def destroy_over_time(self, k=2):
        """
        A2 定向拆车：抽出**超时最严重**那辆车里最拖时间的 k 位乘客。

        为什么需要它：5400 秒是硬约束，而 destroy_random / destroy_worst /
        destroy_route 都是「无目标」的——它们不知道哪辆车正卡在超时上。当初始解
        本身就带一辆超时车（散场模式常见，实测出现过 5984s > 5400s），
        靠随机算子把它拆开纯属碰运气。本算子直接对超时车下手，命中率高得多。

        注意：只在 `_run_phase` 检测到确有超时车时才会被选中，因此
        **可行实例的搜索轨迹与 A2 之前逐位一致**（P1 的实测结论不受影响）。

        选人依据：逐位试抽「谁离队能让这辆车掉最多时长」，取 drop 最大的 k 位。
        计算量 O(len(v)) 次重建，且仅作用于一辆车。
        """
        over = [v for v in self.current if v.duration_s > MAX_ROUTE_SECONDS]
        if not over:
            return []
        v = max(over, key=lambda x: x.duration_s)
        if len(v.passengers) <= 1:
            # 单车单人仍超时：拆不出来（此人直达本身就超上限），交给别的算子/判定
            return []
        scored = []
        for p in v.passengers:
            rest = [q for q in v.passengers if q.id != p.id]
            nv = self._rebuild_vehicle(rest)
            drop = v.duration_s - (nv.duration_s if nv else 0)
            scored.append((drop, p))
        scored.sort(key=lambda x: -x[0])
        removed = [p for _, p in scored[:max(1, k)]]
        self._remove_passengers(removed)
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
            if len(v.passengers) >= self.capacity_limit:
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
                   progress_callback=None, phase_name="main",
                   allow_early_stop=False):
        start = time.time()
        iters = 0
        accept_worse = 0
        last_improve_t = start          # A8：连续无改善窗口的计时起点
        stop_reason = None
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

            # 选择 Destroy。
            # A2：仅当**确有超时车**时才把 destroy_over_time 放进轮盘——两个分支
            # 的 random.choice 消耗同样一次随机数，故可行实例的随机轨迹与 A2 之前
            # 逐位一致（不破坏 P1 验证过的行为）；一旦出现超时车，拆车算子即刻生效。
            if any(v.duration_s > MAX_ROUTE_SECONDS for v in self.current):
                destroy_type = random.choice(["random", "worst", "route", "over_time"])
            else:
                destroy_type = random.choice(["random", "worst", "route"])
            if destroy_type == "random":
                removed = self.destroy_random(k=random.randint(1, 4))
            elif destroy_type == "worst":
                removed = self.destroy_worst(k=random.randint(1, 3))
            elif destroy_type == "over_time":
                removed = self.destroy_over_time(k=random.randint(1, 2))
            else:
                removed = self.destroy_route()

            if not removed:
                continue

            # Repair
            self.repair_greedy(removed)

            # 评价：score 用于 best 追踪（字典序，含硬约束罚分），
            # energy 用于退火接受门（只看主目标，不含罚分——见 _energy 注释）
            score = evaluate(self.current, self.baseline_cost, self.n_passengers,
                             mode, max_vehicles, self.capacity_limit)
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
                    last_improve_t = time.time()
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
                # 第 4 个参数是「不含罚分的真实费用」：best_score[0] 可能含 1e6 罚分
                # （车数超限 / 单车超时），直接当费用上报会让进度条显示天文数字
                progress_callback(time_offset + time.time() - start, iters,
                                  self.best_score, sum(v.cost for v in self.best))

            # A8 终止判定：gap 达标 / 长时间无改善（仅最后一个阶段）
            stop_reason = self._maybe_stop(mode, allow_early_stop, iters, last_improve_t)
            if stop_reason:
                break

        return {"name": phase_name, "mode": mode, "max_vehicles": max_vehicles,
                "iters": iters, "elapsed": round(time.time() - start, 3),
                "accept_worse": accept_worse, "T0": T0, "T_end": T,
                "stop": stop_reason}

    # ---- 主循环 ----
    def run(self, progress_callback=None):
        total_start = time.time()

        if self.two_phase:
            # 阶段 1：车数优先，先把「最少几辆车」定下来。
            # allow_early_stop=False：这一阶段的目标是车数，费用 gap 无意义；
            # 若按「无改善」提前退出，phase 2 就完全没机会跑，故必须跑满预算。
            p1_budget = self.time_budget * self.phase1_ratio
            ph1 = self._run_phase(p1_budget, "fewest_vehicles", None, 0.0,
                                  progress_callback, "fewest_vehicles",
                                  allow_early_stop=False)
            n_veh = len(self.best)

            # 阶段 2：以阶段 1 的最优解为起点，在「车数 ≤ n_veh」硬约束下最小化费用。
            # 这一步回补了旧 fewest_vehicles 的费用劣势——旧实现的字典序虽然也是
            # (车数, 费用, 时长)，但受搜索轨迹影响，在车数相同时费用平均高 10.59%。
            self.current = [self._copy_vehicle(v) for v in self.best]
            self.best_score = evaluate(self.best, self.baseline_cost, self.n_passengers,
                                       "cost", n_veh, self.capacity_limit)
            self.current_score = self.best_score
            ph2 = self._run_phase(self.time_budget - p1_budget, "cost", n_veh,
                                  p1_budget, progress_callback, "cost_under_cap",
                                  allow_early_stop=True)
            phases = [ph1, ph2]
        else:
            phases = [self._run_phase(self.time_budget, self.mode, self.max_vehicles,
                                      0.0, progress_callback, self.mode,
                                      allow_early_stop=True)]

        last = phases[-1]
        # 终止原因：取第一个提前终止的阶段；都没提前终止即为预算用尽
        reason = next((p["stop"] for p in phases if p.get("stop")), None)
        self.stop = {
            "reason": reason or "STOP_TIMEOUT",
            "early": bool(reason),
            "elapsed": round(time.time() - total_start, 3),
        }
        self.stats = {
            "iters": sum(p["iters"] for p in phases),
            "elapsed": round(time.time() - total_start, 3),
            "accept_worse": sum(p["accept_worse"] for p in phases),
            "T0": last["T0"], "T_end": last["T_end"],
            "cooling": self.cooling, "alpha": self.alpha,
            "two_phase": self.two_phase, "phases": phases,
            "stop": dict(self.stop),
        }
        return self.best


# ============ 入口函数 ============
# gap 提前终止阈值：取自《算法详细设计 V1.1》§十四「gap <= 5%」
GAP_STOP_PCT = 0.05
# 「连续无改善」默认窗口：设计文档给的是 300 秒预算下 60 秒（= 20% 预算），
# 按比例缩放到实际预算；下限 3 秒（预算 10 秒的默认档），上限 60 秒。
STABLE_S_RATIO = 0.2
STABLE_S_MIN = 3.0
STABLE_S_MAX = 60.0


def stable_window(time_budget_s: float) -> float:
    """按预算缩放「连续无改善」窗口（见 STABLE_S_* 常量说明）。"""
    return min(STABLE_S_MAX, max(STABLE_S_MIN, STABLE_S_RATIO * float(time_budget_s)))


def stop_reason_text(reason: str, stop: Optional[dict] = None,
                     gap: Optional[float] = None) -> str:
    """终止原因的中文说明（写入 optimization_job.stop_reason，供前端/审计阅读）。

    阈值从 stop 字典里取实际生效值（可能被参数覆盖），避免文案与真实判据不一致。
    """
    stop = stop or {}
    g = f"，与下界差 {gap * 100:.1f}%" if gap is not None else ""
    if reason == "STOP_GAP":
        pct = stop.get("gap_stop_pct")
        pct = GAP_STOP_PCT if pct is None else pct
        return f"候选解与理论下界差距已 ≤ {pct * 100:.0f}%{g}，提前收敛"
    if reason == "OPTIMAL":
        return "已证明达到全局最优（费用等于理论下界）"
    if reason == "STOP_STABLE":
        win = stop.get("stable_s")
        return (f"连续 {win:g} 秒无改善，判定收敛并提前结束"
                if win else "连续无改善，判定收敛并提前结束")
    if reason == "STOP_TIMEOUT":
        return f"求解预算用尽，返回当前最优{g}"
    if reason == "FAILED":
        return "未找到满足硬约束（单车 ≤ 90 分钟）的可行方案"
    return reason or ""


def optimize(passengers: List[Passenger], dest: Passenger,
             dm: DistanceMatrix, mode="cost", time_budget_s=300, stage_cb=None,
             diag: Optional[dict] = None, max_vehicles: Optional[int] = None,
             job_cb=None, progress_cb=None, direct_pairs=None,
             gap_stop_pct=None, stable_s=None, vehicle_pref=VEHICLE_PREF_AUTO):
    """
    主入口：给定乘客列表 + 终点 + 距离矩阵，返回 (最优车辆列表, 基线费用)

    stage_cb     : 可选回调，阶段变化时通知（"initial" / "alns"）——旧前端进度条用
    diag         : 可选诊断字典，传入后填入 initial_score/stats/trace/job
    max_vehicles : 车数上限硬约束（None 表示不限）
    vehicle_pref : A2 车型偏好（auto / six / four）。auto=按 6 座装填、按实际载客数
                   派「最小够用」车型；six=一律 6 座；four=容量上限降到 4。
                   车型不参与费用，只影响容量上限与输出的派车型号。
    job_cb       : A8 作业状态回调，收到 {'kind': ...} 形式的事件字典：
                     {'kind':'init'}                                    → INIT
                     {'kind':'feasible', 'vehicles':n, 'cost':x}        → FEASIBLE
                     {'kind':'optimizing'}                              → OPTIMIZING
                     {'kind':'progress','iteration','best_cost','elapsed'} → 周期进度
                     {'kind':'finished','status','sub_status','gap',
                      'lower_bound','best_cost','iteration','elapsed',
                      'stop_reason'}                                    → 终态
    progress_cb  : 可选，(elapsed_total, iters, best_cost) 周期回调（比 job_cb 更密）
    direct_pairs : 可选，[(直达距离_m, 直达耗时_s), ...]，与 passengers 同序、
                   **物理行驶方向**（集合=家→活动点，散场=活动点→家）。
                   None 时退化为 dm.get(i, dest)。散场模式下 dm 是转置矩阵，
                   必须由调用方传入正向值，否则 gap 下界用错方向。
    gap_stop_pct : gap 提前终止阈值；None = 用默认 GAP_STOP_PCT（仅 cost 模式生效）；
                   传负数可关闭（对照实验用）
    stable_s     : 「连续无改善」窗口（秒）；None = 按预算自动缩放（stable_window）；
                   传正无穷可关闭（对照实验用）

    返回：(best, baseline_cost)。可行性判定与终止原因写入 diag["job"]，
    其中 job["vehicle_types"] / four_seat_count / six_seat_count 为 A2 派车结果。
    """
    n_p = len(passengers)
    pref = normalize_vehicle_pref(vehicle_pref)
    cap = capacity_limit(pref)
    _gap_pct = GAP_STOP_PCT if gap_stop_pct is None else gap_stop_pct
    if _gap_pct is not None and _gap_pct < 0:
        _gap_pct = None
    _stable = stable_window(time_budget_s) if stable_s is None else stable_s
    # 容量下限：每车最多 cap 人，低于此值的上限不可能满足，直接上调并提示
    if max_vehicles is not None:
        capacity_min = -(-n_p // cap) if n_p else 0
        if max_vehicles < capacity_min:
            print(f"  [约束] max_vehicles={max_vehicles} 低于容量下限 {capacity_min}，已上调")
            if diag is not None:
                diag["capacity_clamped"] = {"requested": max_vehicles, "raised_to": capacity_min}
            max_vehicles = capacity_min

    baseline_cost = sum(calc_cost(dm.get(i, n_p)[0]) for i in range(n_p))

    # 直达口径（物理方向）：用于费用下界与「数学上是否可解」的判定
    if direct_pairs is None:
        direct_pairs = [dm.get(i, n_p) for i in range(n_p)]
    direct_m = [float(d or 0.0) for d, _ in direct_pairs]
    direct_s = [float(s or 0.0) for _, s in direct_pairs]

    # 费用下界（A8：gap 的分母）。仅 cost 目标有意义，fewest_vehicles 只做记录。
    # A2：必须用实际生效的容量上限（four 模式上限 4，下界更紧）
    lb = lower_bound_cost(direct_m, cap)
    if job_cb:
        job_cb({"kind": "init"})

    # 初始解
    print(f"  构建初始解...（车型 {pref}，容量上限 {cap} 人）")
    if stage_cb:
        stage_cb("initial")
    vehicles = build_initial_solution(passengers, dest, dm, capacity=cap)
    print(f"  初始解: {len(vehicles)} 辆车")
    initial_feasible = solution_feasible(vehicles, cap)
    if job_cb:
        job_cb({"kind": "feasible", "vehicles": len(vehicles),
                "cost": round(sum(v.cost for v in vehicles), 2),
                "initial_feasible": initial_feasible})

    if diag is not None:
        diag["initial_score"] = evaluate(vehicles, baseline_cost, len(passengers), mode,
                                         max_vehicles, cap)
        diag["initial_vehicles"] = len(vehicles)
        diag["baseline_cost"] = baseline_cost
        diag["lower_bound"] = lb

    # ALNS 优化
    print(f"  启动 ALNS，预算 {time_budget_s} 秒...")
    if stage_cb:
        stage_cb("alns")
    if job_cb:
        job_cb({"kind": "optimizing"})

    def _on_progress(elapsed, iters, best_score, best_cost=None):
        # best_cost 由 ALNS 直接给出真实费用（best_score[0] 可能含 1e6 硬约束罚分，
        # 直接当费用上报会让进度条显示天文数字）
        if best_cost is None:
            best_cost = float(best_score[0]) if mode == "cost" else None
        else:
            best_cost = round(float(best_cost), 2)
        if job_cb:
            job_cb({"kind": "progress", "iteration": iters, "best_cost": best_cost,
                    "elapsed": round(elapsed, 2)})
        if progress_cb:
            progress_cb(elapsed, iters, best_cost)

    alns = ALNS(vehicles, passengers, dest, dm, baseline_cost, len(passengers), mode,
                time_budget_s, trace=(diag is not None), max_vehicles=max_vehicles,
                cost_lb=lb, gap_stop_pct=(_gap_pct if mode == "cost" else None),
                stable_s=_stable, capacity_limit=cap)
    best = alns.run(progress_callback=_on_progress)

    stop = dict(alns.stop or {})
    stop["stable_s"] = _stable
    stop["gap_stop_pct"] = _gap_pct
    best_cost = sum(v.cost for v in best)
    gap = gap_ratio(best_cost, lb)
    # gap 恰好为 0（费用等于下界）→ 已证明全局最优；只有 cost 目标才敢这么断言
    reason = stop.get("reason") or "STOP_TIMEOUT"
    if mode == "cost" and gap is not None and gap <= 1e-9:
        reason = "OPTIMAL"
    stop["reason"] = reason

    final_ok = solution_feasible(best, cap)
    # A2：解定稿后按每辆车实际载客数派车型（4 座 / 6 座）。费用与车型无关，
    # 故这一步不改变任何搜索结论，只产出可直接执行的派车单。
    vtypes = annotate_vehicle_types(best, pref)
    four_cnt, six_cnt = count_vehicle_types(vtypes)
    job = {
        "status": reason if final_ok else "FAILED",
        "sub_status": None if final_ok else (
            "PROVEN_INFEASIBLE"
            if any(s > MAX_ROUTE_SECONDS for s in direct_s) else "SEARCH_FAILED"),
        "best_cost": round(best_cost, 2),
        "lower_bound": round(lb, 2),
        "gap": None if gap is None else round(gap, 4),
        "iteration": int(alns.stats.get("iters", 0)),
        "elapsed": float(alns.stats.get("elapsed", 0.0)),
        "feasible": final_ok,
        "initial_feasible": initial_feasible,
        "vehicle_pref": pref,
        "capacity_limit": cap,
        "vehicle_types": vtypes,
        "four_seat_count": four_cnt,
        "six_seat_count": six_cnt,
    }
    job["stop_reason"] = stop_reason_text(job["status"], stop, job["gap"])

    if diag is not None:
        diag["stats"] = getattr(alns, "stats", {})
        diag["trace"] = alns.trace or []
        diag["final_score"] = evaluate(best, baseline_cost, len(passengers), mode,
                                       max_vehicles, cap)
        diag["job"] = job
        if not final_ok:
            worst = max((v.duration_s for v in best), default=0)
            diag["infeasible"] = {"sub_status": job["sub_status"], "worst_route_s": worst,
                                  "limit_s": MAX_ROUTE_SECONDS}
    if not final_ok:
        print(f"  [不可行] 搜索结束时仍有路线超过 {MAX_ROUTE_SECONDS}s 上限"
              f"（sub_status={job['sub_status']}）")
    if job_cb:
        job_cb({"kind": "finished", **job, "stop_reason": job["stop_reason"]})

    # 上限没满足（常见于 5400 秒单车时长约束与地理分散共同作用），如实提示
    if max_vehicles is not None and len(best) > max_vehicles:
        print(f"  提示：未能在 {max_vehicles} 辆车内满足全部约束（实际 {len(best)} 辆），"
              f"已返回搜索到的最优解")
        if diag is not None:
            diag["constraint_missed"] = {"requested": max_vehicles, "actual": len(best)}

    return best, baseline_cost
