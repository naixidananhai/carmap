# -*- coding: utf-8 -*-
"""
高德地图（AMap / Gaode）API 客户端
===================================
选型理由（对比百度）：
- 高德距离测量 /v3/distance **按请求次数计费**，个人认证免费 15 万次/月。
- 百度 /routematrix/v2/driving **按最终路线条数计费**，30 人一次任务就消耗
  930 条，个人免费额度仅 6000 次/日，一天只能跑 6 个 30 人任务。
- 高德起点可传 100 个（终点仅 1 个），循环 N 次即可拼出 N×N 矩阵。

坐标系（重要）：
- 项目对外统一使用 **BD09（百度坐标）**，前端仍是百度 JS API，不做改动。
- 高德接口使用 **GCJ-02（火星坐标）**。
- 因此本模块在边界处做转换：
    对外（build_matrix / driving_route）收 BD09，内部转 GCJ-02 调高德，
    折线返回前再转回 BD09，保证前端标记与折线仍在同一坐标系。
- GCJ-02 → BD-09 无官方精确逆变换（百度自身也是偏移算法），此处用标准近似
  公式还原，误差 1~2 米，对折线渲染与聚类均可接受。
"""

import hashlib
import math
import time
from typing import List, Optional, Tuple

import requests

import config  # noqa: F401  导入即加载 .env

HOST = "https://restapi.amap.com"
DISTANCE_PATH = "/v3/distance"          # 距离测量（批量，按请求计费）
DIRECTION_PATH = "/v3/direction/driving"  # 驾车路径规划（含加密折线）
GEOCODE_PATH = "/v3/geocode/geo"          # 地理编码

MAX_ORIGINS = 100  # 高德 origins 单次上限
DEFAULT_QPS_DELAY = 0.05  # 节流间隔，高德个人认证 QPS 较低


# ============================================================
# 坐标系转换
# ============================================================
def bd09_to_gcj02(bd_lng: float, bd_lat: float) -> Tuple[float, float]:
    """BD-09（百度）→ GCJ-02（火星/高德）。正变换，官方标准公式。"""
    x = bd_lng - 0.0065
    y = bd_lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * math.pi)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * math.pi)
    return z * math.cos(theta), z * math.sin(theta)


def gcj02_to_bd09(lng: float, lat: float) -> Tuple[float, float]:
    """GCJ-02（高德）→ BD-09（百度）。反向近似还原，误差约 1~2 米。"""
    z = math.sqrt(lng * lng + lat * lat) + 0.00002 * math.sin(lat * math.pi)
    theta = math.atan2(lat, lng) + 0.000003 * math.cos(lng * math.pi)
    return z * math.cos(theta) + 0.0065, z * math.sin(theta) + 0.006


# ============================================================
# 高德加密 polyline 解码
# ============================================================
def decode_amap_polyline(polyline: str) -> List[Tuple[float, float]]:
    """
    解码高德 direction 接口返回的加密 polyline。

    算法（与 Google encoded polyline 同源，高德在其基础上叠加了偏移）：
      1. 每个字符 ASCII - 63 得到 5 位有效载荷 + 1 位符号位
      2. 符号位 1 表示负数，先取反再加 1（补码）
      3. 经纬度交替累加，缩放系数 1e-5

    返回 [(lng, lat), ...]，顺序与输入一致。
    """
    result = []
    index = 0
    length = len(polyline)
    lat = 0.0
    lng = 0.0
    # 高德在解码前会给经纬度一个统一的起始偏移
    _OFFSET = 1e5

    while index < length:
        for _ in range(2):  # 每轮解出经度、纬度各一个
            shift = 0
            result_byte = 0
            while True:
                if index >= length:
                    return result
                b = ord(polyline[index]) - 63
                index += 1
                result_byte |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            # 符号位处理：奇数表示负数
            delta = ~(result_byte >> 1) if (result_byte & 1) else (result_byte >> 1)
            if _ == 0:
                lng += delta
            else:
                lat += delta
        result.append((lng / _OFFSET / 10.0, lat / _OFFSET / 10.0))
    return result


# ============================================================
# 客户端
# ============================================================
class AmapClient:
    """
    高德地图客户端。

    坐标约定：**对外统一 BD09**（与项目其余部分一致），
    仅在调用高德接口前转 GCJ-02，返回折线前转回 BD09。
    """

    def __init__(self, key: Optional[str] = None, qps_delay: float = DEFAULT_QPS_DELAY):
        self.key = key or config.os.environ.get("AMAP_KEY", "")
        self.session = requests.Session()
        self.cache = {}
        self._last_call_ts = 0.0
        self._qps_delay = qps_delay

    # ---------- 基础设施 ----------
    def _throttle(self):
        """简单节流，避免触发高德 QPS 限制（个人认证默认较低）。"""
        if self._qps_delay <= 0:
            return
        elapsed = time.time() - self._last_call_ts
        if elapsed < self._qps_delay:
            time.sleep(self._qps_delay - elapsed)
        self._last_call_ts = time.time()

    def _request(self, path: str, params: dict, retries: int = 2) -> Optional[dict]:
        """
        发起 GET 请求。
        高德鉴权：params["key"] = KEY；若 key 设置了 sig（数字签名），
        还需 sig 参数——本项目用普通 key + IP 白名单，不启用 sig。
        """
        if not self.key:
            raise RuntimeError(
                "缺少高德地图 Key：请在环境变量或项目根目录 .env 中配置 "
                "AMAP_KEY（参考 .env.example）。"
            )
        params = dict(params)
        params["key"] = self.key
        params.setdefault("output", "json")
        url = HOST + path

        for attempt in range(retries):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=5)
                data = resp.json()
                if str(data.get("status")) == "1":
                    return data
                info = data.get("info", "")
                print(f"  [高德API] {path} status={data.get('status')} {info}")
                # 配额/频率类错误退避后重试；参数错误直接返回
                if "QUOTA" in str(info).upper() or "FLOW" in str(info).upper() or "忙" in str(info):
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return None
            except Exception as e:
                print(f"  [高德API异常] {path}: {e}")
                if attempt < retries - 1:
                    time.sleep(0.3)
                    continue
                return None
        return None

    # ---------- 地理编码 ----------
    def geocode(self, address: str, city: Optional[str] = None) -> Optional[Tuple[float, float]]:
        """
        地址 → (lng, lat)，**GCJ-02** 坐标（高德原生坐标系）。
        若项目需要 BD09，请用 bd09_from_amap() 转换。
        """
        cache_key = ("geo", address, city)
        if cache_key in self.cache:
            return self.cache[cache_key]
        params = {"address": address}
        if city:
            params["city"] = city
        data = self._request(GEOCODE_PATH, params)
        if data and data.get("geocodes"):
            loc = data["geocodes"][0].get("location", "")
            if "," in loc:
                lng, lat = loc.split(",", 1)
                out = (float(lng), float(lat))
                self.cache[cache_key] = out
                return out
        return None

    # ---------- 单条驾车路线（含折线） ----------
    def driving_route_bd09(self, o_lat: float, o_lng: float,
                            d_lat: float, d_lng: float
                            ) -> Optional[Tuple[float, float, List[Tuple[float, float]]]]:
        """
        单条驾车路线。**入参/返回折线均为 BD09 坐标**。
        返回 (distance_m, duration_s, [(lng, lat), ...])；失败返回 None。
        """
        cache_key = (round(o_lat, 6), round(o_lng, 6), round(d_lat, 6), round(d_lng, 6))
        if cache_key in self.cache:
            return self.cache[cache_key]

        # BD09 → GCJ-02
        g_o_lng, g_o_lat = bd09_to_gcj02(o_lng, o_lat)
        g_d_lng, g_d_lat = bd09_to_gcj02(d_lng, d_lat)

        data = self._request(DIRECTION_PATH, {
            "origin": f"{g_o_lng},{g_o_lat}",
            "destination": f"{g_d_lng},{g_d_lat}",
            # strategy=0 速度优先；extensions=base 返回 polyline
            "strategy": 0,
            "extensions": "base",
        })
        if not data:
            return None
        route = data.get("route") or {}
        paths = route.get("paths") or []
        if not paths:
            return None
        p0 = paths[0]
        distance_m = float(p0.get("distance", 0) or 0)
        duration_s = float(p0.get("duration", 0) or 0)

        # 拼接加密折线 → 解码 → GCJ-02 转回 BD09
        pts: List[Tuple[float, float]] = []
        steps = p0.get("steps") or []
        for st in steps:
            poly = st.get("polyline") or ""
            if poly:
                pts.extend(decode_amap_polyline(poly))
        # extensions=base 时 steps 内无 polyline，退回顶层 polyline
        if not pts and p0.get("polyline"):
            pts = decode_amap_polyline(p0["polyline"])

        pts_bd09 = [gcj02_to_bd09(lng, lat) for lng, lat in pts]

        result = (distance_m, duration_s, pts_bd09)
        self.cache[cache_key] = result
        return result

    # ---------- 距离矩阵 ----------
    def build_matrix(self, points_bd09: List[Tuple[float, float]]):
        """
        构建距离矩阵。入参 points 为 BD09 的 [(lat, lng), ...]。

        返回 matrix[i][j] = (distance_m, duration_s)，失败为 None。
        **不返回折线**——折线改在结果阶段按需补取（见 driving_route_bd09）。

        实现：高德 /v3/distance 的 origins 可传多点、destination 仅单点，
        因此以每个点为终点循环 N 次，拼成完整 N×N 矩阵。
        """
        n = len(points_bd09)
        matrix = [[None] * n for _ in range(n)]
        for i in range(n):
            matrix[i][i] = (0.0, 0.0)
        if n <= 1:
            return matrix

        # 预先把全部点转成 GCJ-02，避免在循环里重复计算
        pts_gcj = [bd09_to_gcj02(lng, lat) for lat, lng in points_bd09]

        print(f"  高德距离矩阵：{n} 个点，需 {n} 次请求（按请求计费）")

        for j in range(n):
            d_lng, d_lat = pts_gcj[j]
            # 以 j 为终点，把其余点全部作为起点（分批，每批 ≤100）
            origins_all = [(i, pts_gcj[i]) for i in range(n) if i != j]
            for start in range(0, len(origins_all), MAX_ORIGINS):
                batch = origins_all[start:start + MAX_ORIGINS]
                origins_str = "|".join(f"{lng},{lat}" for _, (lng, lat) in batch)
                data = self._request(DISTANCE_PATH, {
                    "origins": origins_str,
                    "destination": f"{d_lng},{d_lat}",
                    "type": 1,  # 1 = 驾车导航距离（考虑路况）
                })
                if not data:
                    continue
                # results 与 origins 顺序一一对应
                for idx, res in enumerate(data.get("results") or []):
                    if idx >= len(batch):
                        break
                    i = batch[idx][0]
                    dist = res.get("distance")
                    dur = res.get("duration")
                    if dist is None:
                        continue
                    # 高德异常时返回 -1，需过滤
                    try:
                        dist_f = float(dist)
                        dur_f = float(dur) if dur is not None else None
                    except (TypeError, ValueError):
                        continue
                    if dist_f < 0:
                        continue
                    if dur_f is None or dur_f < 0:
                        # 缺耗时：按 30km/h 兜底估算，保证时长约束可用
                        dur_f = dist_f / 1000.0 / 30.0 * 3600
                    matrix[i][j] = (dist_f, dur_f)

            done = j + 1
            print(f"  高德距离矩阵进度: {done}/{n}")

        return matrix


def bd09_from_amap(lng: float, lat: float) -> Tuple[float, float]:
    """高德原生坐标 → BD09。供前端地址检索等场景使用。"""
    return gcj02_to_bd09(lng, lat)


if __name__ == "__main__":
    client = AmapClient()
    if not client.key:
        print("未配置 AMAP_KEY，跳过自测")
    else:
        print("=== 测试地理编码 ===")
        r = client.geocode("郑州市中原区")
        print(f"GCJ-02: {r}")

        print("\n=== 坐标往返误差 ===")
        if r:
            lng, lat = r
            bd = gcj02_to_bd09(lng, lat)
            back = bd09_to_gcj02(*bd)
            err_m = ((back[0] - lng) ** 2 + (back[1] - lat) ** 2) ** 0.5 * 111320
            print(f"GCJ-02 -> BD09 -> GCJ-02 误差: {err_m:.2f} 米")

        print("\n=== 测试单条驾车（BD09 入参）===")
        # 中原区 -> 郑州东站
        o = bd09_to_gcj02(113.6195, 34.7545)
        d = bd09_to_gcj02(113.7536, 34.7421)
        res = client.driving_route_bd09(34.7545, 113.6195, 34.7421, 113.7536)
        if res:
            dist, dur, pts = res
            print(f"距离: {dist/1000:.1f} km, 耗时: {dur/60:.1f} 分钟, 折线点: {len(pts)}")
            if pts:
                print(f"起点: {pts[0]}, 终点: {pts[-1]}")
        else:
            print("失败")

        print("\n=== 测试 3 点矩阵 ===")
        pts = [(34.7545, 113.6195), (34.7750, 113.7400), (34.7421, 113.7536)]
        m = client.build_matrix(pts)
        for i in range(len(pts)):
            print([None if m[i][j] is None else round(m[i][j][0] / 1000, 2)
                   for j in range(len(pts))])
