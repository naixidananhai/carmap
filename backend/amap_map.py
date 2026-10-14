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
# 个人认证 Key 的 QPS 上限较低（实测约 3~5 次/秒），这里保守取 0.35s/次 ≈ 2.8 QPS，
# 否则会频繁触发 CUQPS_HAS_EXCEEDED_THE_LIMIT 导致矩阵大面积空洞
DEFAULT_QPS_DELAY = 0.35

# 触发限流时的退避序列（秒）
_BACKOFF = (0.6, 1.2, 2.0)
# 需识别为「限流/配额」并退避重试的错误码
_THROTTLE_CODES = ("CUQPS_HAS_EXCEEDED_THE_LIMIT", "CUQPS_OVER_LIMIT",
                   "DAILY_QUERY_OVER_LIMIT", "QPS_HAS_EXCEEDED_THE_LIMIT",
                   "QUOTA", "FLOW", "RATE_LIMIT")

# A7：距阵点对缓存落到 SQLite（map_matrix 表），跨任务复用、进程重启后仍生效。
# 数据库不可用时静默降级为「无缓存」，绝不影响主流程。
PROVIDER_NAME = "amap"


def _cache_load(points_bd09: List[Tuple[float, float]], matrix) -> int:
    """
    用 SQLite 的 map_matrix 预填矩阵，返回命中数。
    键为 BD09 的定点坐标串，因此同一批地址在不同任务之间可以复用。
    """
    try:
        import db
    except Exception:  # noqa: BLE001
        return 0
    n = len(points_bd09)
    pairs = [(points_bd09[i][0], points_bd09[i][1],
              points_bd09[j][0], points_bd09[j][1])
             for i in range(n) for j in range(n) if i != j]
    try:
        hits = db.matrix_lookup(PROVIDER_NAME, pairs)
    except Exception as e:  # noqa: BLE001
        print(f"  [矩阵缓存] 读取失败，忽略缓存：{e}")
        return 0
    k = 0
    for idx, (i, j) in enumerate([(i, j) for i in range(n) for j in range(n) if i != j]):
        hit = hits[idx] if idx < len(hits) else None
        if hit is None:
            continue
        matrix[i][j] = (float(hit[0]), float(hit[1]))
        k += 1
    return k


def _cache_save(rows) -> None:
    """把本次新取到的点对写回 SQLite（失败只打印，不影响返回结果）。"""
    if not rows:
        return
    try:
        import db
        db.matrix_put_many(PROVIDER_NAME, rows)
        print(f"  高德距离矩阵：{len(rows)} 对已写入本地缓存（下次同点位零请求）")
    except Exception as e:  # noqa: BLE001
        print(f"  [矩阵缓存] 写入失败，忽略：{e}")


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
# 高德 polyline 解析
# ============================================================
def decode_amap_polyline(polyline: str) -> List[Tuple[float, float]]:
    """
    解析高德 direction 接口返回的 polyline，返回 [(lng, lat), ...]（GCJ-02）。

    实测结论（2026-10，以真实响应为准，不要凭记忆改）：
    Web 服务 /v3/direction/driving 在 extensions=base 下返回的 polyline 是
    **明文绝对坐标**，格式为 "lng,lat;lng,lat;..."，坐标已是 GCJ-02 绝对值，
    每个 step 的 polyline 都自带绝对坐标，无需累加基准。

    注意：不同版本/不同客户端（JS SDK vs Web服务）曾出现过加密格式
    （Google encoded polyline 变体，缩放 1e5）。为兼容，这里保留
    _decode_encoded_polyline 作为兜底：按"明文"解析失败时自动尝试。

    返回顺序与输入一致。
    """
    if not polyline:
        return []

    text = polyline.strip()
    # 明文判定：含 ";" 分隔且形如 "数字,数字"
    if ";" in text or ("," in text and "?" not in text and "A" not in text[:5]):
        pts: List[Tuple[float, float]] = []
        ok = True
        for seg in text.split(";"):
            seg = seg.strip()
            if not seg:
                continue
            parts = seg.split(",")
            if len(parts) < 2:
                ok = False
                break
            try:
                pts.append((float(parts[0]), float(parts[1])))
            except ValueError:
                ok = False
                break
        # 明文坐标量级校验：经度应在 [-180,180]、纬度在 [-90,90]
        if ok and pts and all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in pts):
            return pts

    return _decode_encoded_polyline(text)


def _decode_encoded_polyline(polyline: str) -> List[Tuple[float, float]]:
    """
    加密 polyline 解码（Google encoded polyline 变体，缩放 1e5）。
    作为兼容兜底保留——实测 Web 服务返回明文，此路径一般不会触发。
    """
    result = []
    index = 0
    length = len(polyline)
    lat = 0.0
    lng = 0.0
    offset = 1e5

    while index < length:
        for which in (0, 1):  # 每轮解出经度、纬度各一个
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
            # 符号位处理：奇数表示负数（补码）
            delta = ~(result_byte >> 1) if (result_byte & 1) else (result_byte >> 1)
            if which == 0:
                lng += delta
            else:
                lat += delta
        result.append((lng / offset, lat / offset))
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

    def __init__(self, key: Optional[str] = None, qps_delay: Optional[float] = None):
        self.key = key or config.os.environ.get("AMAP_KEY", "")
        # 节流间隔可用 AMAP_QPS_DELAY 覆盖（默认 0.35s ≈ 2.8 QPS）
        try:
            env_delay = float(config.os.environ.get("AMAP_QPS_DELAY", ""))
            if env_delay > 0:
                qps_delay = env_delay
        except ValueError:
            pass
        self.qps_delay = DEFAULT_QPS_DELAY if qps_delay is None else qps_delay
        self.session = requests.Session()
        self.cache = {}
        self._last_call_ts = 0.0
        self._qps_delay = self.qps_delay

    # ---------- 基础设施 ----------
    def _throttle(self):
        """简单节流，避免触发高德 QPS 限制（个人认证默认较低）。"""
        if self._qps_delay <= 0:
            return
        elapsed = time.time() - self._last_call_ts
        if elapsed < self._qps_delay:
            time.sleep(self._qps_delay - elapsed)
        self._last_call_ts = time.time()

    def _request(self, path: str, params: dict, retries: int = 3) -> Optional[dict]:
        """
        发起 GET 请求。
        高德鉴权：params["key"] = KEY；若 key 设置了 sig（数字签名），
        还需 sig 参数——本项目用普通 key + IP 白名单，不启用 sig。

        对限流类错误（CUQPS_HAS_EXCEEDED_THE_LIMIT 等）做指数退避重试；
        若最终仍失败返回 None，由调用方决定如何处理空洞。
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

        last_info = ""
        for attempt in range(retries):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=8)
                data = resp.json()
                if str(data.get("status")) == "1":
                    return data
                info = str(data.get("info", ""))
                last_info = info
                # 限流/配额类错误：退避后重试（个人认证 Key 很容易触发 QPS 上限）
                if any(code in info.upper() for code in _THROTTLE_CODES):
                    wait = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
                    print(f"  [高德限流] {info}，{wait:.1f}s 后重试 "
                          f"({attempt + 1}/{retries - 1})")
                    time.sleep(wait)
                    continue
                print(f"  [高德API] {path} status={data.get('status')} {info}")
                return None
            except Exception as e:
                print(f"  [高德API异常] {path}: {e}")
                if attempt < retries - 1:
                    time.sleep(0.3)
                    continue
                return None
        if last_info:
            print(f"  [高德API放弃] {path}: {last_info}（已重试 {retries} 次）")
        return None

    # ---------- 地理编码 ----------
    def geocode(self, address: str, city: Optional[str] = None) -> Optional[Tuple[float, float]]:
        """
        地址 → (lng, lat)，**GCJ-02** 坐标（高德原生坐标系）。
        若项目需要 BD09，请用 bd09_from_amap() 转换。

        city 仅作为「检索偏好」而非硬限制：高德 city 参数会拒绝跨市结果
        （实测「河南博物院」实际在开封，传 city=郑州市 直接无结果）。
        因此策略是——先按 city 检索，无结果再放宽到全国，最后用
        province/city 字段做合理性提示（供调用方决定是否采信）。
        """
        cache_key = ("geo", address, city)
        if cache_key in self.cache:
            return self.cache[cache_key]
        params = {"address": address}
        if city:
            params["city"] = city
        data = self._request(GEOCODE_PATH, params)
        loc = self._extract_loc(data)
        if loc is None and city:
            # city 限定下无结果（多为跨市地址），放宽到全国再试一次
            data = self._request(GEOCODE_PATH, {"address": address})
            loc = self._extract_loc(data)
            if loc is not None:
                city_used = "全国（city 限定无结果，已放宽）"
            else:
                city_used = None
        else:
            city_used = city
        if loc is None:
            return None
        out = (loc[0], loc[1])
        self.cache[cache_key] = out
        self.last_geocode_city = city_used
        return out

    @staticmethod
    def _extract_loc(data: Optional[dict]) -> Optional[Tuple[float, float]]:
        """从 geocode 响应中取出首个 location，转为 (lng, lat)。"""
        if data and data.get("geocodes"):
            raw = data["geocodes"][0].get("location", "")
            if "," in raw:
                lng, lat = raw.split(",", 1)
                try:
                    return (float(lng), float(lat))
                except ValueError:
                    return None
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

        # 拼接各 step 的折线 → GCJ-02 转回 BD09
        # 注意：相邻 step 的边界点会重复出现，需去重，否则折线出现折返抖动
        pts: List[Tuple[float, float]] = []
        steps = p0.get("steps") or []
        for st in steps:
            poly = st.get("polyline") or ""
            if not poly:
                continue
            seg_pts = decode_amap_polyline(poly)
            for pt in seg_pts:
                if pts and abs(pts[-1][0] - pt[0]) < 1e-7 and abs(pts[-1][1] - pt[1]) < 1e-7:
                    continue
                pts.append(pt)
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

        # ---- A7：先吃 SQLite 里的点对缓存（跨任务复用、进程重启后仍有效）----
        cached = _cache_load(points_bd09, matrix)
        pending = []          # 本次新取到的点对，稍后统一落库
        if cached:
            print(f"  高德距离矩阵：{n} 个点，其中 {cached} 对命中本地缓存（不消耗配额）")

        def _fetch_column(j: int) -> int:
            """以 j 为终点，一次请求取回其余全部起点，返回成功填充的对数。"""
            d_lng, d_lat = pts_gcj[j]
            filled = 0
            # 已经缓存到的点对不再请求——这是缓存节省配额的关键
            origins_all = [(i, pts_gcj[i]) for i in range(n)
                           if i != j and matrix[i][j] is None]
            if not origins_all:
                return 0
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
                    pending.append((points_bd09[i][0], points_bd09[i][1],
                                    points_bd09[j][0], points_bd09[j][1],
                                    dist_f, dur_f))
                    filled += 1
            return filled

        for j in range(n):
            _fetch_column(j)
            print(f"  高德距离矩阵进度: {j + 1}/{n}")

        # ---- 空洞补齐：限流/网络抖动导致的缺失对逐个重试 ----
        missing = [(i, j) for i in range(n) for j in range(n)
                   if i != j and matrix[i][j] is None]
        if missing:
            print(f"  高德矩阵有 {len(missing)} 对缺失（多为限流），开始逐点补齐...")
            by_dest = {}
            for i, j in missing:
                by_dest.setdefault(j, []).append(i)
            still_missing = 0
            for j, origins_idx in by_dest.items():
                d_lng, d_lat = pts_gcj[j]
                origins_str = "|".join(f"{pts_gcj[i][0]},{pts_gcj[i][1]}" for i in origins_idx)
                data = self._request(DISTANCE_PATH, {
                    "origins": origins_str,
                    "destination": f"{d_lng},{d_lat}",
                    "type": 1,
                }, retries=4)
                if not data:
                    still_missing += len(origins_idx)
                    continue
                for idx, res in enumerate(data.get("results") or []):
                    if idx >= len(origins_idx):
                        break
                    i = origins_idx[idx]
                    try:
                        dist_f = float(res.get("distance"))
                        dur_f = float(res.get("duration") or 0)
                    except (TypeError, ValueError):
                        still_missing += 1
                        continue
                    if dist_f < 0:
                        still_missing += 1
                        continue
                    final_dur = dur_f if dur_f > 0 else dist_f / 1000.0 / 30.0 * 3600
                    matrix[i][j] = (dist_f, final_dur)
                    pending.append((points_bd09[i][0], points_bd09[i][1],
                                    points_bd09[j][0], points_bd09[j][1],
                                    dist_f, final_dur))
            recovered = len(missing) - still_missing
            print(f"  补齐完成：恢复 {recovered}/{len(missing)} 对"
                  + (f"，仍缺失 {still_missing} 对" if still_missing else "，矩阵完整"))
            if still_missing:
                print("  提示：持续缺失通常意味着 QPS 超限，可在 .env 中调大 "
                      "AMAP_QPS_DELAY（默认 0.35 秒）")

        _cache_save(pending)
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
