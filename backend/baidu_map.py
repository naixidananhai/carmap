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

import config  # noqa: F401  导入即加载项目根目录 .env

HOST = "https://api.map.baidu.com"
DRIVING_PATH = "/direction/v2/driving"
GEOCODE_PATH = "/geocoding/v3"
GEOCONV_PATH = "/geoconv/v1"
# 批量算路：单次最多 100 条路线（起终点个数之积 ≤ 100）
# 只返回距离/耗时，不含折线；折线用 DRIVING_PATH 按需补取
ROUTEMATRIX_PATH = "/routematrix/v2/driving"

# ⚠️ 实测结论（2026-10）：/routematrix/v2/driving 官方参数表只有 ak，**不支持 SN 签名**。
# 但实测该 Key 被配置为强制 SN 校验，带 sn 与不带 sn 均返回 `211 APP SN校验失败`。
# 这是账号权限层面的限制，无法通过改签名算法绕过。
# 因此本模块会自动降级到逐对单条接口（见 build_full_matrix），功能正确但速度不提升。
# 若要真正启用批量加速，需在百度控制台为该 Key 放开 routematrix 权限，
# 或改用高德 /v3/distance（按请求计费，个人免费 15 万次/月，见 amap_map.py）。

BAIDU_AK = os.environ.get("BAIDU_MAP_AK", "")
BAIDU_SK = os.environ.get("BAIDU_MAP_SK", "")


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

    def _request(self, path: str, params: dict, retries=1, signed=True):
        """
        带 SN 签名的请求。

        signed=False 时不传 sn：/routematrix/v2/driving 官方文档只要求 ak
        （该接口不支持 SN 签名）。但若 Key 被配置为强制 SN 校验，仍会返回
        211，此时调用方需自行降级。
        """
        if not self.ak:
            raise RuntimeError(
                "缺少百度地图密钥：请在环境变量或项目根目录 .env 中配置 "
                "BAIDU_MAP_AK 与 BAIDU_MAP_SK（参考 .env.example）。"
            )
        if signed:
            params["ak"] = self.ak
            params["sn"] = calc_sn(path, params, self.sk)
        else:
            params["ak"] = self.ak
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

    def build_full_matrix(self, points, with_paths=False):
        """
        构建完整的点对点距离矩阵。

        默认走 **routematrix 批量接口**（/routematrix/v2/driving），
        单次最多 100 条路线（起终点个数之积 ≤ 100），30 人仅需 9 次请求；
        旧实现是 N×(N-1) 次单条驾车请求，30 人要 870 次，慢 100 倍。

        注意：批量接口 **只返回距离和耗时，不返回路径折线**，
        因此默认不再预取折线（预取要为 N² 个点对各发一次请求，
        是纯浪费——求最优解只需要距离，最终方案用到的折线极少）。
        折线改在结果阶段调用 driving_route() 按需补取（带缓存）。

        points: [(lat, lng), ...] 所有点（乘客起点 + 终点）
        返回: (matrix, path_matrix)
          matrix[i][j]     = (distance_m, duration_s)，失败为 None
          path_matrix[i][j] = [(lng, lat), ...]，with_paths=False 时全为 None
        """
        n = len(points)
        matrix = [[None] * n for _ in range(n)]
        path_matrix = [[None] * n for _ in range(n)]
        for i in range(n):
            matrix[i][i] = (0, 0)
            path_matrix[i][i] = []

        # with_paths=True：走旧的多线程单条接口，逐对取折线（慢，保留用于调试对照）
        if with_paths:
            return self._build_matrix_with_paths(points, matrix, path_matrix)

        # 默认：批量接口
        from concurrent.futures import ThreadPoolExecutor, as_completed

        MAX_PAIRS = 100  # 百度批量算路：起终点个数之积不能超过 100
        # 把 N×N 拆成若干个 ≤100 对的批次
        batches = []
        cur = []
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                cur.append((i, j))
                if len(cur) >= MAX_PAIRS:
                    batches.append(cur)
                    cur = []
        if cur:
            batches.append(cur)

        print(f"百度批量算路：{n} 个点，{n*(n-1)} 对路线，分 {len(batches)} 批请求")

        def calc_batch(batch):
            origins = "|".join(f"{points[i][0]},{points[i][1]}" for i, _ in batch)
            dests = "|".join(f"{points[j][0]},{points[j][1]}" for _, j in batch)
            data = self._request(ROUTEMATRIX_PATH, {
                "origins": origins,
                "destinations": dests,
                "output": "json",
            }, signed=False)
            return batch, data

        failed_batches = 0
        done = 0
        total = n * (n - 1)
        # 百度批量接口并发上限较高，保守用 3 线程
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(calc_batch, b) for b in batches]
            for future in as_completed(futures):
                try:
                    batch, data = future.result()
                except Exception as e:
                    print(f"  [批量算路异常] {e}")
                    continue
                if not data:
                    failed_batches += 1
                    done += len(batch)
                    continue
                results = data.get("result") or []
                # 返回顺序为 origins × destinations 的笛卡尔积，且每条带
                # origin_index / dest_index（1 起）。用显式索引映射回 (i, j)，
                # 不依赖返回顺序，更稳妥。
                got = 0
                for item in results:
                    oi = item.get("origin_index")
                    oj = item.get("dest_index")
                    dist = item.get("distance")
                    dur = item.get("duration")
                    if dist is None:
                        continue
                    try:
                        dist_f = float(dist)
                        dur_f = float(dur or 0)
                    except (TypeError, ValueError):
                        continue
                    # 索引为 1 起；batch 内 origins/destinations 顺序一致
                    if oi is None or oj is None:
                        continue
                    if not (1 <= oi <= len(batch)) or not (1 <= oj <= len(batch)):
                        continue
                    i = batch[oi - 1][0]
                    j = batch[oj - 1][1]
                    if i == j:
                        continue
                    matrix[i][j] = (dist_f, dur_f)
                    got += 1
                done += len(batch)
                print(f"  批量算路进度: {min(done, total)}/{total}（本批 {got} 对）")

        filled = sum(1 for i in range(n) for j in range(n) if i != j and matrix[i][j] is not None)
        if failed_batches:
            print(f"  批量算路有 {failed_batches}/{len(batches)} 批失败")
        if filled == 0 and n > 1:
            # 批量接口整体不可用（常见于 Key 被配置为强制 SN 校验，
            # 而 routematrix 不接受 sn 签名）→ 降级到逐对单条接口，功能不受影响
            print("  批量算路不可用，降级为逐对单条驾车接口（较慢但结果正确）")
            return self._build_matrix_with_paths(points, matrix, path_matrix)

        return matrix, path_matrix

    def _build_matrix_with_paths(self, points, matrix, path_matrix):
        """旧实现：多线程逐对调单条驾车接口，同时取回折线。仅调试对照时使用。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        n = len(points)
        tasks = [(i, j) for i in range(n) for j in range(n) if i != j]
        total = len(tasks)
        done = 0

        def calc_one(i, j):
            return (i, j, self.driving_route(points[i][0], points[i][1],
                                             points[j][0], points[j][1]))

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(calc_one, i, j) for i, j in tasks]
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
