# -*- coding: utf-8 -*-
"""
前端地图无头验证脚本（Edge CDP）
==================================
用真实浏览器加载页面，执行完整交互链路，逐项断言：
  1. 高德 JS API 是否加载成功（AMap 存在）
  2. 底图 canvas / 瓦片是否真的渲染出来（不是白屏）
  3. 活动点小地图能否初始化、标记是否落在郑州东站一带
  4. 地址检索是否成功（高德 Geocoder）
  5. 提交任务后主地图是否画出真实路网折线
  6. 坐标系一致性：BD09 转换后的点是否落在高德底图正确位置
     （对比"未转换直接用 BD09"的偏移量，验证转换确实生效）
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
URL = "http://localhost:8000/"
DEBUG_PORT = 9333

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


def cdp_capture():
    """建立 CDP 连接，返回可调用的命令执行器"""
    import websocket  # websocket-client

    ws_url = None
    for _ in range(30):
        try:
            r = urllib.request.urlopen(
                f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=2)
            tabs = json.loads(r.read().decode())
            for t in tabs:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    ws_url = t["webSocketDebuggerUrl"]
                    break
            if ws_url:
                break
        except Exception:
            pass
        time.sleep(0.4)
    if not ws_url:
        raise RuntimeError("无法连接 CDP")

    ws = websocket.create_connection(ws_url, timeout=40, suppress_origin=True)
    msg_id = [0]
    console_errors = []

    def send(method, params=None, wait=True):
        msg_id[0] += 1
        mid = msg_id[0]
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        if not wait:
            return None
        deadline = time.time() + 40
        while time.time() < deadline:
            ws.settimeout(max(1, deadline - time.time()))
            try:
                msg = json.loads(ws.recv())
            except Exception:
                continue
            # 采集控制台错误（供第 7 项断言使用）
            if msg.get("method") == "Log.entryAdded":
                e = msg.get("params", {}).get("entry", {})
                if e.get("level") == "error":
                    # favicon.ico 的 404 与业务无关，记录 URL 以便精确过滤
                    console_errors.append(
                        f'{e.get("text", "")} :: {e.get("url", "")}')
            elif msg.get("method") == "Runtime.exceptionThrown":
                det = msg.get("params", {}).get("exceptionDetails", {})
                console_errors.append(det.get("text", "") or str(det))
            if msg.get("id") == mid:
                return msg.get("result", {})
        return None

    return ws, send, console_errors


def evaluate(send, expr, await_promise=False):
    r = send("Runtime.evaluate", {
        "expression": expr,
        "returnByValue": True,
        "awaitPromise": await_promise,
    })
    if not r:
        return None
    if r.get("exceptionDetails"):
        return {"__error__": str(r["exceptionDetails"])}
    return r.get("result", {}).get("value")


def main():
    print("=== 启动无头 Edge ===")
    proc = subprocess.Popen([
        EDGE, "--headless=new", f"--remote-debugging-port={DEBUG_PORT}",
        "--remote-allow-origins=*",
        "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", "--window-size=1440,1000",
        "--user-data-dir=C:/Temp/edge_carmap_verify",
        URL,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        ws, send, console_errors = cdp_capture()
        print("CDP 已连接\n")

        # 先开启日志采集，再等页面就绪，避免漏掉早期错误
        send("Log.enable")
        send("Runtime.enable")
        send("Page.enable")

        # 等待页面与地图脚本就绪（高德 JS API 体积较大，首次加载可能要好几秒）
        print("=== 1. 页面与高德 API 加载 ===")
        loaded = False
        for _ in range(60):
            st = evaluate(send, "document.readyState")
            has = evaluate(send, "typeof AMap !== 'undefined'")
            # AMap 加载完成标志：既定义了对象，也挂上了 Map 构造器
            ready = evaluate(send,
                "typeof AMap !== 'undefined' && typeof AMap.Map === 'function'")
            if st == "complete" and ready is True:
                loaded = True
                break
            time.sleep(0.6)
        check("高德 JS API 已加载 (AMap.Map 可用)", loaded)
        if not loaded:
            return

        # 等待底图瓦片
        time.sleep(4)

        version = evaluate(send, "typeof AMap !== 'undefined' ? AMap.version : null")
        check("AMap 版本可读", bool(version), version or "N/A")

        # 主地图容器初始 display:none，canvas 尺寸此时为 0，
        # 因此这里只校验小地图（pick_map 一直可见），主地图在提交任务后再验
        pick_canvas = evaluate(send, """(() => {
          const cs = Array.from(document.querySelectorAll('#pick_map canvas'));
          return cs.map(c => c.width + 'x' + c.height);
        })()""")
        check("选点小地图 canvas 已渲染", len(pick_canvas or []) > 0,
              f'{len(pick_canvas or [])} 个: {", ".join(pick_canvas or [])[:80]}')

        # 底图 tile 图片是否真的加载（高德用瓦片图）
        tiles = evaluate(send, """(() => {
          const imgs = Array.from(document.querySelectorAll('img'))
            .filter(i => i.src && /amap|autonavi/.test(i.src));
          return {total: imgs.length, loaded: imgs.filter(i => i.complete && i.naturalWidth > 0).length};
        })()""")
        check("底图瓦片图片已加载", (tiles or {}).get("loaded", 0) > 0,
              f"{(tiles or {}).get('loaded', 0)}/{(tiles or {}).get('total', 0)} 张")

        print("\n=== 2. 坐标系转换正确性（核心）===")
        coord = evaluate(send, """(() => {
          const bd = [113.7536, 34.7421];              // 郑州东站 BD09
          const gcj = bd09ToGcj02(bd[0], bd[1]);
          const back = gcj02ToBd09(gcj[0], gcj[1]);
          const shiftM = Math.hypot(gcj[0]-bd[0], gcj[1]-bd[1]) * 111320 *
                         Math.cos(bd[1]*Math.PI/180);
          const roundTripM = Math.hypot(back[0]-bd[0], back[1]-bd[1]) * 111320 *
                         Math.cos(bd[1]*Math.PI/180);
          return {gcj, back, shiftM, roundTripM};
        })()""")
        if coord:
            check("BD09->GCJ-02 偏移量合理 (500~900m)",
                  500 < coord["shiftM"] < 900, f'{coord["shiftM"]:.0f} m')
            check("往返误差 < 1m", coord["roundTripM"] < 1,
                  f'{coord["roundTripM"]:.4f} m')
            print(f"       BD09 {coord['gcj'] and [113.7536,34.7421]} -> "
                  f'GCJ [{coord["gcj"][0]:.6f}, {coord["gcj"][1]:.6f}]')

        print("\n=== 3. 活动点小地图 ===")
        pick = evaluate(send, """(() => {
          if (!pickMap) return {ok: false, reason: 'pickMap 未初始化'};
          const c = pickMap.getCenter();
          return {ok: true, center: [c.lng, c.lat],
                  zoom: pickMap.getZoom(),
                  dest: destPoint ? [destPoint.lng, destPoint.lat] : null,
                  markerExists: !!destMarker,
                  coordText: document.getElementById('dest_coord').textContent};
        })()""")
        check("小地图已初始化", bool(pick and pick.get("ok")),
              str(pick.get("reason")) if pick and not pick.get("ok") else "")
        if pick and pick.get("ok"):
            check("活动点已设为郑州东站",
                  abs(pick["dest"][0] - 113.7536) < 0.001 and
                  abs(pick["dest"][1] - 34.7421) < 0.001,
                  pick["coordText"])
            check("活动点标记已创建", pick["markerExists"])
            # 地图中心（GCJ-02）与 destPoint（BD09）应是同一物理位置，
            # 因此数值上应相差约 700m——若为 0 则说明忘转换，视野会偏
            off = abs(pick["center"][0] - pick["dest"][0])
            check("地图中心已做 GCJ-02 转换（与 BD09 差约 700m）", off > 0.003,
                  f'中心 lng {pick["center"][0]:.4f} vs BD09 {pick["dest"][0]:.4f}, '
                  f'偏移 {off * 111320 * 0.85:.0f}m')

        print("\n=== 4. 地址检索（高德 Geocoder）===")
        evaluate(send, """(() => {
          document.getElementById('dest_addr').value = '郑州东站';
          document.getElementById('dest_search_btn').click();
        })()""")
        ok_geo = False
        for _ in range(25):
            tag = evaluate(send, "document.getElementById('dest_status').textContent")
            if tag in ("已定位", "未找到该地址"):
                ok_geo = (tag == "已定位")
                break
            time.sleep(0.5)
        geo_res = evaluate(send, """(() => ({
          tag: document.getElementById('dest_status').textContent,
          coord: document.getElementById('dest_coord').textContent,
          addr: document.getElementById('dest_addr').value
        }))()""")
        check("高德地址检索成功", ok_geo,
              f'{geo_res["tag"]} -> {geo_res["coord"]}')
        # 郑州东站实际 BD09 ≈ 113.7536,34.7421，允许 3km 误差
        if ok_geo:
            import re as _re
            nums = _re.findall(r"[\d.]+", geo_res["coord"])
            lat, lng = float(nums[0]), float(nums[1])
            d = ((lat - 34.7421) ** 2 + ((lng - 113.7536) * 0.88) ** 2) ** 0.5 * 111
            check("检索结果落在郑州东站附近 (<3km)", d < 3, f"{d:.2f} km")

        print("\n=== 5. 名单解析状态 ===")
        rows = evaluate(send, """(() => ({
          count: document.getElementById('rows_count').textContent,
          ok: passengers.filter(p => p.status === 'ok').length,
          total: passengers.length
        }))()""")
        check("示例名单已载入", (rows or {}).get("total", 0) == 15,
              (rows or {}).get("count", ""))

        print("\n=== 6. 提交任务并验证地图渲染 ===")
        evaluate(send, """(() => {
          document.getElementById('submit_btn').click();
        })()""")
        time.sleep(0.8)
        # 确认弹层
        confirm_shown = evaluate(send,
            "document.getElementById('confirm_mask').style.display === 'block'")
        check("提交前确认弹层出现", confirm_shown)
        evaluate(send, "document.getElementById('cf_ok').click()")

        done = False
        for _ in range(90):
            st = evaluate(send, "document.getElementById('progress').style.display")
            err = evaluate(send, "document.getElementById('err_bar').textContent")
            if err and "失败" in (err or ""):
                check("任务执行无错误", False, err)
                break
            if st == "none" and evaluate(send,
                    "document.getElementById('result_card').style.display === 'block'"):
                done = True
                break
            time.sleep(1.5)
        check("任务完成并展示结果", done)
        if not done:
            return

        time.sleep(4)
        res = evaluate(send, """(() => {
          const r = currentResult;
          if (!r) return null;
          return {
            vehicles: r.vehicle_count,
            totalCost: r.total_cost,
            rate: r.saving_rate,
            hasPath: r.vehicles.filter(v => v.path && v.path.length > 1).length,
            pathPoints: r.vehicles.reduce((s,v) => s + ((v.path||[]).length), 0),
            timeline: document.querySelectorAll('.tl-chip').length
          };
        })()""")
        check("结果含真实路网折线", (res or {}).get("hasPath", 0) > 0,
              f'{res["vehicles"]} 辆车, {res["pathPoints"]} 个折线点')
        check("时间线已渲染", (res or {}).get("timeline", 0) > 0,
              f'{res["timeline"]} 个时间线节点')
        print(f'       总费用 ¥{res["totalCost"]}，下降率 {res["rate"]}%')

        # 检查高德覆盖物是否真的画到地图上。
        # 注意：高德 2.0 默认用 WebGL 渲染，canvas.getContext('2d') 拿不到像素，
        # 因此改用「覆盖物数量 + 折线点数」这类可测量的接口状态来判断。
        overlay = evaluate(send, """(() => {
          const c = currentResult ? currentResult.vehicles : [];
          return {
            vehicles: c.length,
            pathPoints: c.reduce((s, v) => s + ((v.path || []).length), 0),
            visible: visibleVehicles.size
          };
        })()""")
        check("覆盖物状态正常（车辆可见且有折线）",
              (overlay or {}).get("visible", 0) > 0 and
              (overlay or {}).get("pathPoints", 0) > 0,
              f'{(overlay or {}).get("visible")} 辆车可见, '
              f'{(overlay or {}).get("pathPoints")} 个折线点')

        # 主地图容器此时已显示，尺寸应已生效
        map_size = evaluate(send, """(() => {
          const e = document.getElementById('map');
          const cs = Array.from(e.querySelectorAll('canvas'));
          return { w: e.offsetWidth, h: e.offsetHeight, canvases: cs.length };
        })()""")
        check("主地图容器已布局且有 canvas",
              (map_size or {}).get("w", 0) > 400 and (map_size or {}).get("canvases", 0) > 0,
              f'{(map_size or {}).get("w")}x{(map_size or {}).get("h")}, '
              f'{(map_size or {}).get("canvases")} 个 canvas')

        # 图例
        legend = evaluate(send, "document.querySelectorAll('#legend .legend-item').length")
        check("图例已渲染", (legend or 0) > 0, f"{legend} 项")

        # 关键：折线首点与站点是否重合（验证坐标转换没搞错）
        align = evaluate(send, """(() => {
          const r = currentResult;
          const v = r.vehicles[0];
          if (!v.path || v.path.length < 2) return null;
          const first = v.path[0], last = v.path[v.path.length-1];
          const c0 = v.coordinates[0], cn = v.coordinates[v.coordinates.length-1];
          const m = (a,b) => Math.hypot(a.lng-b.lng, a.lat-b.lat) * 111320 *
                            Math.cos(c0.lat*Math.PI/180);
          return {startGap: m(first, c0), endGap: m(last, cn)};
        })()""")
        if align:
            check("折线起点与首站重合 (<100m)", align["startGap"] < 100,
                  f'{align["startGap"]:.1f} m')
            check("折线终点与末站重合 (<100m)", align["endGap"] < 100,
                  f'{align["endGap"]:.1f} m')

        # 控制台错误（真实采集，忽略 favicon 这类无害 404）
        print("\n=== 7. 控制台错误检查 ===")
        real_errors = [e for e in console_errors
                       if e and "favicon" not in e.lower()]
        check("无高德相关 JS 错误", len(real_errors) == 0,
              " | ".join(real_errors[:3])[:220] if real_errors else "无")

        # 截图留证
        send("Page.enable")
        send("Emulation.setDeviceMetricsOverride", {
            "width": 1440, "height": 1400, "deviceScaleFactor": 1, "mobile": False})
        time.sleep(1.5)
        evaluate(send, "window.scrollTo(0,0); if (map) map.resize();")
        time.sleep(2)
        shot = send("Page.captureScreenshot", {"format": "png"})
        if shot and shot.get("data"):
            out = Path(__file__).resolve().parent.parent / "debug" / "frontend_amap.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(__import__("base64").b64decode(shot["data"]))
            print(f"\n截图已保存: {out}")

    finally:
        try:
            ws.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()

    print("\n" + "=" * 56)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"结果: {passed}/{total} 项通过")
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print("失败项: " + ", ".join(failed))
    print("=" * 56)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())