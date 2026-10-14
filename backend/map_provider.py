# -*- coding: utf-8 -*-
"""
地图数据源抽象（MapProvider）
===============================
为什么抽象：三家地图的接口形态、坐标系、计费口径都不同，
但上层（优化算法、结果组装）只关心两件事：
    1. build_matrix(points) -> matrix[i][j] = (distance_m, duration_s)
    2. driving_route(...)   -> (distance_m, duration_s, [(lng, lat), ...])
统一后，切换数据源只改环境变量 MAP_PROVIDER，不动任何业务代码。

坐标约定：**统一对外 BD09**（前端仍是百度 JS API）。
高德内部转 GCJ-02 后再调接口，返回前转回 BD09——详见 amap_map.py。

三级降级：
    MAP_PROVIDER=amap     高德（优先：按请求计费，个人免费额度最宽松）
    MAP_PROVIDER=baidu    百度批量/单条
    MAP_PROVIDER=haversine 不调外部 API，纯球面距离（离线兜底）
若首选数据源整体不可用（密钥缺失/接口全失败），自动降级到下一级。
"""

import os
from typing import List, Optional, Tuple

import config  # noqa: F401  导入即加载 .env

Matrix = List[List[Optional[Tuple[float, float]]]]


class BaseProvider:
    """数据源统一接口。子类必须实现 build_matrix 与 driving_route。"""

    name = "base"

    def build_matrix(self, points: List[Tuple[float, float]]) -> Matrix:
        """points: [(lat, lng), ...]（BD09）。返回 N×N 矩阵，失败元素为 None。"""
        raise NotImplementedError

    def driving_route(self, o_lat: float, o_lng: float,
                      d_lat: float, d_lng: float
                      ) -> Optional[Tuple[float, float, List[Tuple[float, float]]]]:
        """单条驾车路线。返回 (距离m, 耗时s, [(lng,lat), ...])，失败 None。"""
        raise NotImplementedError

    def geocode(self, address: str, city: Optional[str] = None
                ) -> Optional[Tuple[float, float]]:
        """地址 -> (lng, lat)（BD09）。city 为城市限定，用于排除同名地标。"""
        return None

    def is_usable(self) -> bool:
        """密钥是否就绪。未就绪则在 create_provider 里被跳过。"""
        return True


class AmapProvider(BaseProvider):
    """高德（推荐）。矩阵按请求计费，个人认证免费 15 万次/月。"""

    name = "amap"

    def __init__(self, client=None):
        if client is None:
            from amap_map import AmapClient
            client = AmapClient()
        self.client = client

    def is_usable(self) -> bool:
        return bool(self.client.key)

    def build_matrix(self, points):
        return self.client.build_matrix(points)

    def driving_route(self, o_lat, o_lng, d_lat, d_lng):
        return self.client.driving_route_bd09(o_lat, o_lng, d_lat, d_lng)

    def geocode(self, address, city=None):
        r = self.client.geocode(address, city)
        if not r:
            return None
        from amap_map import gcj02_to_bd09
        return gcj02_to_bd09(r[0], r[1])


class BaiduProvider(BaseProvider):
    """百度。矩阵优先走批量算路，不可用时自动降级逐对单条。"""

    name = "baidu"

    def __init__(self, client=None):
        if client is None:
            from baidu_map import BaiduMapClient
            client = BaiduMapClient()
        self.client = client

    def is_usable(self) -> bool:
        return bool(self.client.ak)

    def build_matrix(self, points):
        matrix, _ = self.client.build_full_matrix(points, with_paths=False)
        return matrix

    def driving_route(self, o_lat, o_lng, d_lat, d_lng):
        return self.client.driving_route(o_lat, o_lng, d_lat, d_lng)

    def geocode(self, address, city=None):
        return self.client.geocode(address)


class HaversineProvider(BaseProvider):
    """
    直线距离兜底（不调外部 API）。
    沿用 optimizer.haversine_m（含 ×1.4 驾车绕行系数）。
    仅用于「无网络 / 无密钥」时保证功能可用，精度显著低于真实路网。
    """

    name = "haversine"

    def is_usable(self) -> bool:
        return True

    def build_matrix(self, points):
        from optimizer import haversine_m
        n = len(points)
        m: Matrix = [[None] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                if i == j:
                    m[i][j] = (0.0, 0.0)
                    continue
                d = haversine_m(points[i][0], points[i][1], points[j][0], points[j][1])
                # 30km/h 估算耗时
                m[i][j] = (d, d / 1000.0 / 30.0 * 3600)
        return m

    def driving_route(self, o_lat, o_lng, d_lat, d_lng):
        from optimizer import haversine_m
        d = haversine_m(o_lat, o_lng, d_lat, d_lng)
        dur = d / 1000.0 / 30.0 * 3600
        # 无真实路网，返回空折线：前端据此降级为直线连接
        return (d, dur, [])


# 首选顺序：高德 -> 百度 -> 直线
_PROVIDER_ORDER = ["amap", "baidu", "haversine"]


def create_provider(preferred: Optional[str] = None):
    """
    按 MAP_PROVIDER 环境变量创建数据源，密钥缺失时自动退到下一级。

    返回 (provider, 实际使用的名称)。
    """
    pref = (preferred or os.environ.get("MAP_PROVIDER", "amap")).strip().lower()
    if pref not in _PROVIDER_ORDER:
        print(f"  [provider] MAP_PROVIDER={pref!r} 未知，回退默认 amap")
        pref = "amap"

    # 先试首选，不可用则按顺序退级
    order = [pref] + [p for p in _PROVIDER_ORDER if p != pref]

    for name in order:
        try:
            if name == "amap":
                p = AmapProvider()
            elif name == "baidu":
                p = BaiduProvider()
            else:
                p = HaversineProvider()

            if p.is_usable():
                if name != pref:
                    print(f"  [provider] {pref} 不可用，降级为 {name}")
                else:
                    print(f"  [provider] 使用 {name}")
                return p, name
            print(f"  [provider] {name} 密钥未配置，跳过")
        except Exception as e:
            print(f"  [provider] {name} 初始化失败: {e}")

    return HaversineProvider(), "haversine"


def provider_has_keys() -> bool:
    """是否至少配置了一个真实地图数据源（用于前端提示）。"""
    return bool(os.environ.get("AMAP_KEY") or os.environ.get("BAIDU_MAP_AK"))
