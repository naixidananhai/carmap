# -*- coding: utf-8 -*-
"""
百度地图 API 客户端（SN 签名版，单条驾车接口）
- 单条驾车路线规划（direction/v2/driving）—— 已验证通过
- 地理编码（geocoding/v3）—— 已验证通过
- 距离矩阵：用单条接口循环调用，构建完整矩阵
- 每条路线同时返回沿真实路网的折线点（steps[].path 拼接），供地图渲染
"""

import requests
import time
import os
import re
import hashlib
from urllib.parse import quote, quote_plus, urlencode
from pathlib import Path


def _load_env_file():
    """
    加载项目根目录下的 .env（若存在），不依赖第三方库。
    便于本地开发：把 BAIDU_MAP_AK / BAIDU_MAP_SK 写在 .env 里（已被 gitignore），
    避免把密钥硬编码进源码、提交到仓库造成泄露。
    """
    try:
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            with env_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
    except Exception:
        pass


_load_env_file()

BAIDU_AK = os.environ.get("BAIDU_MAP_AK", "")
BAIDU_SK = os.environ.get("BAIDU_MAP_SK", "")

HOST = "https://api.map.baidu.com"
DRIVING_PATH = "/direction/v2/driving"
GEOCODE_PATH = "/geocoding/v3"
GEOCONV_PATH = "/geoconv/v1"


def calc_sn(uri_path: str, params: dict, sk: str) -> str:
    """
    百度 SN 签名算法（Java 官方方式，已验证）：
    1. 每个 value 单独 urlencode，拼 query string
    2. 前面加 uri + "?"，后面加 sk
    3. 整体再 quote_plus
    4. MD5 小写十六进制
    """
    query = urlencode(params)
    raw = f"{uri_path}?{query}{sk}"
    final = quote_plus(raw)
    return hashlib.md5(final.encode("utf-8")).hexdigest()


def parse_path_points(path_str: str):
    """
    解析百度 step.path 字符串 -> [(lng, lat), ...]
    格式：'lng,lat;lng,lat;...'（兼容 | 分隔的旧版返回）
    """
    if not path_str:
        return []
    pts = []
    for seg in re.split(r"[;|]", path_str):
        seg = seg.strip()
        if not seg:
            continue
        parts = seg.split(",")
        if len(parts) < 2:
            continue
        try:
            pts.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return pts


class BaiduMapClient:
    def __init__(self, ak=None, sk=None):
        self.ak = ak or BAIDU_AK
        self.sk = sk or BAIDU_SK
        self.session = requests.Session()
        self.cache = {}

    def _request(self, path: str, params: dict, retries=1):
        """带 SN 签名的请求"""
        if not self.ak or not self.sk:
            raise RuntimeError(
                "缺少百度地图密钥：请在环境变量或项目根目录 .env 中配置 "
                "BAIDU_MAP_AK 与 BAIDU_MAP_SK（参考 .env.example）。"
            )
        params["ak"] = self.ak
        params["sn"] = calc_sn(path, params, self.sk)
        url = HOST + path

        for i in range(retries):
            try:
                resp = self.session.get(url, params=params, timeout=3)
                data = resp.json()
                if data.get("status") == 0:
                    return data
                else:
                    print(f"  [百度API] status={data.get('status')} {data.get('message', '')}")
                    if data.get("status") in (211, 1, 210):  # SN失败/服务错误/IP错误，不重试
                        return None
            except Exception as e:
                print(f"  [百度API异常] {e}")
                return None  # 异常立刻返回，不重试
        return None

    def geocode(self, address):
        """地址转 BD09 坐标 (lat, lng)"""
        data = self._request(GEOCODE_PATH, {"address": address, "output": "json"})
        if data and data.get("result"):
            loc = data["result"]["location"]
            return loc["lat"], loc["lng"]
        return None

    def geoconv(self, lng, lat, from_coord=3):
        """坐标转 BD09"""
        data = self._request(GEOCONV_PATH, {
            "coords": f"{lng},{lat}",
            "from": from_coord,
            "to": 5
        })
        if data and data.get("result"):
            item = data["result"][0]
            return item["y"], item["x"]
        return None

    def driving_route(self, origin_lat, origin_lng, dest_lat, dest_lng):
        """
        单条驾车路线规划，返回 (distance_m, duration_s, path_points)
        path_points: [(lng, lat), ...] 沿真实路网的折线点（拼接所有 step 的 path）
        坐标格式：lat,lng
        """
        cache_key = (origin_lat, origin_lng, dest_lat, dest_lng)
        if cache_key in self.cache:
            return self.cache[cache_key]

        data = self._request(DRIVING_PATH, {
            "origin": f"{origin_lat},{origin_lng}",
            "destination": f"{dest_lat},{dest_lng}",
            "output": "json"
        })

        if data and data.get("result"):
            result = data["result"]
            # 取第一条路线
            routes = result.get("routes", [])
            if routes:
                r = routes[0]
                distance_m = r.get("distance", 0)
                duration_s = r.get("duration", 0)
                # 拼接所有 step 的 path，得到沿路网的折线点
                path_pts = []
                for st in r.get("steps", []):
                    path_pts.extend(parse_path_points(st.get("path", "")))
                self.cache[cache_key] = (distance_m, duration_s, path_pts)
                return (distance_m, duration_s, path_pts)
        return None

    def build_full_matrix(self, points):
        """
        构建完整的点对点距离矩阵（用单条驾车接口 + 多线程并发）。
        points: [(lat, lng), ...] 所有点（乘客起点 + 终点）
        返回: (matrix, path_matrix)
          matrix[i][j]     = (distance_m, duration_s)，失败为 None
          path_matrix[i][j] = [(lng, lat), ...] 真实路网折线点，失败为 None
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        n = len(points)
        matrix = [[None] * n for _ in range(n)]
        path_matrix = [[None] * n for _ in range(n)]
        for i in range(n):
            matrix[i][i] = (0, 0)
            path_matrix[i][i] = []

        # 收集所有需要计算的任务
        tasks = []
        for i in range(n):
            for j in range(n):
                if i != j:
                    tasks.append((i, j, points[i], points[j]))

        total = len(tasks)
        done = 0

        def calc_one(i, j, p1, p2):
            result = self.driving_route(p1[0], p1[1], p2[0], p2[1])
            return (i, j, result)

        # 多线程并发（百度免费并发上限 30，保守用 5 线程）
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(calc_one, i, j, p1, p2): (i, j)
                       for i, j, p1, p2 in tasks}
            for future in as_completed(futures):
                i, j, result = future.result()
                if result:
                    matrix[i][j] = (result[0], result[1])
                    path_matrix[i][j] = result[2]
                done += 1
                if done % 50 == 0:
                    print(f"  距离矩阵进度: {done}/{total}")

        return matrix, path_matrix


if __name__ == "__main__":
    client = BaiduMapClient()

    print("=== 测试地理编码 ===")
    r = client.geocode("郑州市中原区")
    print(f"结果: {r}")

    print("\n=== 测试单条驾车 ===")
    # 中原区 -> 郑州东站
    result = client.driving_route(34.7545, 113.6195, 34.7421, 113.7536)
    if result:
        print(f"距离: {result[0]/1000:.1f} km, 耗时: {result[1]/60:.1f} 分钟, 路径点: {len(result[2])} 个")
    else:
        print("失败")

    print("\n=== 测试 3 点矩阵 ===")
    points = [
        (34.7545, 113.6195),  # 中原区
        (34.7750, 113.7400),  # 金水区东
        (34.7421, 113.7536),  # 郑州东站
    ]
    matrix, path_matrix = client.build_full_matrix(points)
    for i in range(3):
        for j in range(3):
            if matrix[i][j]:
                n_pts = len(path_matrix[i][j]) if path_matrix[i][j] else 0
                print(f"  P{i+1} -> P{j+1}: {matrix[i][j][0]/1000:.1f}km, {matrix[i][j][1]/60:.0f}分, 路径点 {n_pts} 个")
