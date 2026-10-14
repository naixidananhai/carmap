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
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
URL = "http://localhost:8000/"


def free_port():
    """取一个当前空闲的本地端口，避免与上一轮遗留的 Edge 调试端口碰撞。
    曾因固定 9333 被上一轮残留进程占着，新 Edge 抢不到端口、CDP 连上了旧实例的
    陈旧页面（那页早已跑完第 4 节、destPoint 被改成检索结果），导致「活动点已设为
    郑州东站」间歇性误报失败。"""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


DEBUG_PORT = free_port()
# 每次运行用独立配置目录，避免 Edge 单实例机制把 URL 交给上一轮的旧实例进程
PROFILE_DIR = os.path.join(tempfile.gettempdir(), f"edge_carmap_verify_{os.getpid()}")

results = []


def kill_edge_tree(pid):
    """按配置目录彻底清理 Edge 进程树。

    Edge 启动进程常会退化为 launcher 后自行退出，真实浏览器进程另有 PID，
    因此仅 taskkill <pid> 不足以清理，必须按命令行里的 profile 目录兜底匹配。
    残留进程会占住调试端口，污染下一次运行。
    """
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{PROFILE_DIR}*' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
        f"--user-data-dir={PROFILE_DIR}",
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

        # 守卫：确认连上的是「本次新启动」的页面，而不是上一轮遗留实例的陈旧页面。
        # 陈旧页面的典型特征是已经跑完过完整流程（currentResult 非空、destPoint 已被
        # 地址检索覆盖）。若不先拦住，后续断言会出现难以定位的误报。
        fresh = evaluate(send, """(() => {
          const d = destPoint;
          const tag = document.getElementById('dest_status');
          return {
            hasResult: typeof currentResult !== 'undefined' && !!currentResult,
            submitted: typeof TASK_ID !== 'undefined' && !!TASK_ID,
            destDefault: !!d && Math.abs(d.lng - 113.7536) < 1e-9 && Math.abs(d.lat - 34.7421) < 1e-9,
            destTag: tag ? tag.textContent : null,
            destCoord: document.getElementById('dest_coord').textContent
          };
        })()""") or {}
        stale = fresh.get("hasResult") or fresh.get("submitted") or not fresh.get("destDefault")
        check("连到的是全新加载的页面（无上一轮残留状态）", not stale,
              f"hasResult={fresh.get('hasResult')}, destDefault={fresh.get('destDefault')}")
        if stale:
            print("  !! 疑似连上了遗留实例的陈旧页面，本次结果不可信，直接中止")
            return

        # 回归守卫：初始状态不得谎报「已定位」。
        # 旧版把 dest_status 硬编码成 st-ok/已定位 + 写死坐标，但加载时根本没调用过
        # /api/geocode；用户会以为坐标是检索出来的。默认点必须标记为「默认点」。
        check("初始状态未谎报「已定位」（默认点须如实标注）",
              fresh.get("destTag") == "默认点",
              f'初始 dest_status={fresh.get("destTag")!r}, coord={fresh.get("destCoord")!r}')

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

        # 地址框被改动却没重新检索时，落点仍是旧坐标——状态必须如实提示，
        # 否则用户会以为「地址框里写的就是当前落点」而直接提交
        def _type(addr):
            evaluate(send, """(() => {
              const inp = document.getElementById('dest_addr');
              const setter = Object.getOwnPropertyDescriptor(
                  HTMLInputElement.prototype, 'value').set;
              setter.call(inp, %s);
              inp.dispatchEvent(new Event('input', {bubbles: true}));
            })()""" % json.dumps(addr, ensure_ascii=False))

        _type('二七广场')
        dirty_tag = evaluate(send, "document.getElementById('dest_status').textContent")
        check("地址改动未检索时状态转为「地址已改」", dirty_tag == "地址已改", repr(dirty_tag))
        _type('郑州东站')   # 还原，交由第 4 节点「检索」走完整流程

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
        # A9：先设一个人均上限（示例名单 15 人，人均必然超过 ¥10），
        # 用于验证「超上限 → 给出建议 → 人工确认 → 重算」这条闭环
        evaluate(send, "document.getElementById('per_capita_cap').value = '10'")
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

        # A8：前端要能看到 8 态作业状态（进度卡片的状态标签 + 结果卡片的状态脚注）
        a8 = evaluate(send, """(() => ({
          tag: document.getElementById('job_status_tag').textContent,
          tagTitle: document.getElementById('job_status_tag').title,
          statusText: document.getElementById('job_status_text').textContent,
          bar: document.getElementById('job_bar').style.width,
          note: (document.querySelector('#vehicles_list p') || {}).textContent || ''
        }))()""") or {}
        check("进度卡片展示了 8 态作业状态（已收敛/预算用尽等）",
              a8.get("tag") in ("准备中", "已找到可行解", "优化中", "提前收敛",
                                "预算用尽", "已收敛", "全局最优", "未找到可行方案"),
              f'{a8.get("tag")!r} / {a8.get("tagTitle")!r}')
        check("终止原因文案已回显", bool(a8.get("statusText")),
              str(a8.get("statusText"))[:60])
        check("进度条走完（100%）", a8.get("bar") == "100%", str(a8.get("bar")))
        check("结果卡片带求解状态脚注（含 gap）",
              "求解状态" in (a8.get("note") or "") and "下界" in (a8.get("note") or ""),
              (a8.get("note") or "")[:90])

        # A2：4/6 座车型——下拉可选、列表头有统计、每辆车标出座位数、导出文本同步
        a2 = evaluate(send, """(() => {
          const sel = document.getElementById('vehicle_pref');
          const r = currentResult || {};
          const vs = r.vehicles || [];
          return {
            options: sel ? Array.from(sel.options).map(o => o.value) : [],
            head: (document.querySelector('#vehicles_list p') || {}).textContent || '',
            titles: Array.from(document.querySelectorAll(
                        '#vehicles_list .vehicle-item > div:first-child > b'))
                        .map(b => b.textContent),
            types: vs.map(v => v.vehicle_type),
            four: r.four_seat_count, six: r.six_seat_count,
            text: resultText()
          };
        })()""") or {}
        check("车型下拉提供 自动 / 仅4座 / 仅6座 三项",
              a2.get("options") == ["auto", "four", "six"], str(a2.get("options")))
        check("结果列表头回显车型统计（4 座 ×n / 6 座 ×m）",
              "座 ×" in (a2.get("head") or ""), (a2.get("head") or "")[:90])
        check("每辆车标题标出座位数",
              bool(a2.get("titles")) and all("座）" in t for t in a2.get("titles")),
              str(a2.get("titles")))
        check("车型取值只可能是 4 或 6，且统计自洽",
              bool(a2.get("types"))
              and set(a2.get("types")) <= {4, 6}
              and (a2.get("four") or 0) + (a2.get("six") or 0) == len(a2.get("types")),
              f'types={a2.get("types")} 4座={a2.get("four")} 6座={a2.get("six")}')
        check("导出文本包含车型", "座）" in (a2.get("text") or ""))

        # A9：人均车费展示 + 超限时的「建议剔除」卡片。
        # 关键语义：算法**只给建议、不自动剔除**——必须人工点「确认剔除并重算」。
        a9 = evaluate(send, """(() => {
          const r = currentResult || {};
          const btn = Array.from(document.querySelectorAll('#vehicles_list button'))
                          .find(b => (b.textContent || '').indexOf('确认剔除并重算') >= 0);
          const s = r.drop_suggestion || null;
          return {
            head: (document.querySelector('#vehicles_list p') || {}).textContent || '',
            hasBtn: !!btn,
            cap: r.per_capita_cap, pc: r.per_capita_cost, total: r.total_cost,
            served: r.served_count, exceeded: r.exceeded,
            achievable: s ? s.achievable : null,
            names: (s && s.best) ? s.best.drop_names : [],
            note: s ? s.note : '',
            pending: window.PENDING_DROP_NAMES || []
          };
        })()""") or {}
        check("结果列表头展示人均车费与上限",
              "人均" in (a9.get("head") or "") and "上限 ¥" in (a9.get("head") or ""),
              (a9.get("head") or "")[:120])
        check("人均车费是「总费用 ÷ 参与人数」",
              a9.get("pc") is not None and a9.get("served") == 15
              and abs(a9.get("pc") * a9.get("served") - a9.get("total")) < 0.05,
              f'人均 ¥{a9.get("pc")} × {a9.get("served")} 人 '
              f'≈ ¥{a9.get("total")}')
        check("超上限被如实标出（exceeded=True）", a9.get("exceeded") is True)
        check("给出「建议剔除」卡片与确认按钮", a9.get("hasBtn") is True)
        check("建议名单非空且不是把人全剔了",
              bool(a9.get("names")) and len(a9.get("names")) < 15,
              str(a9.get("names")))
        check("建议文案说清了可达性与根因", bool(a9.get("note")),
              (a9.get("note") or "")[:70])
        check("建议名单已挂到 PENDING_DROP_NAMES（点按钮才生效，不自动剔除）",
              a9.get("pending") == a9.get("names"),
              str(a9.get("pending")))
        # 备选方案区：达标方案未必是业务最优，取舍必须摊开给人看
        a9alt = evaluate(send, """(() => {
          const opts = window.PENDING_DROP_OPTIONS || [];
          const rows = Array.from(document.querySelectorAll('#vehicles_list button'))
              .filter(b => (b.textContent || '').indexOf('采用') >= 0);
          return { opts: opts.map(o => [o.drop_count, o.per_capita, o.meets_cap]),
                   rows: rows.length };
        })()""") or {}
        if len(a9alt.get("opts") or []) > 1:
            check("摊开了备选方案（含各自的剔除人数与人均）",
                  (a9alt.get("rows") or 0) == len(a9alt["opts"]) - 1,
                  f'备选 {a9alt["opts"]}，按钮 {a9alt.get("rows")} 个')
        else:
            check("候选不足时不给备选区（不崩）", (a9alt.get("rows") or 0) == 0,
                  f'备选 {a9alt["opts"]}')

        # A7：任务与结果已写穿 SQLite（前端不需刷新即可被别的客户端读到）
        #
        # 缺陷修复（2026-10-14）：原先这里取 `typeof TASK_ID !== 'undefined' ? TASK_ID : null`，
        # 但前端**根本没有 TASK_ID 这个变量**（task_id 只存在于 submitTask 的局部作用域），
        # 于是恒取到 null、整块断言被静默跳过——测试照报「全部通过」，实际一条都没跑。
        # 现改为从页面已有的全局状态 currentResult.task_id 取（showResult 里赋值）。
        import urllib.request as _url
        tid = evaluate(send,
                       "(typeof currentResult !== 'undefined' && currentResult) "
                       "? currentResult.task_id : null")
        check("能从前端状态取到 task_id（SQLite 断言块不再被跳过）", bool(tid), str(tid))
        if tid:
            with _url.urlopen(f"http://localhost:8000/api/tasks/{tid}") as resp:
                import json as _json
                saved = _json.loads(resp.read().decode("utf-8"))
            check("结果已持久化到 SQLite（job_status/terminal 可查）",
                  saved.get("job_status") in ("STOP_GAP", "STOP_TIMEOUT", "STOP_STABLE",
                                              "OPTIMAL", "FAILED")
                  and saved.get("terminal") is True,
                  f'{saved.get("job_status")} / terminal={saved.get("terminal")}')
            # A2：车型偏好与车型统计都已落库并可读回
            r0 = saved.get("result") or {}
            check("接口回读车型偏好与车型统计",
                  saved.get("vehicle_pref") in ("auto", "six", "four")
                  and r0.get("four_seat_count") is not None
                  and r0.get("six_seat_count") is not None,
                  f'pref={saved.get("vehicle_pref")} 4座={r0.get("four_seat_count")} '
                  f'6座={r0.get("six_seat_count")}')
            check("每辆车都带合法车型（4 / 6）",
                  bool(r0.get("vehicles"))
                  and all(v.get("vehicle_type") in (4, 6) for v in r0.get("vehicles")),
                  str([v.get("vehicle_type") for v in (r0.get("vehicles") or [])]))

        # 6a. A9：人工确认「剔除并重算」→ 被剔者真的不再参与，人均随之重算
        drop_names = a9.get("names") or []
        old_tid = tid
        if drop_names:
            print("\n=== 6a. A9：人工确认剔除后重算 ===")
            evaluate(send, "applyPendingDrops()")
            ok2 = False
            for _ in range(90):
                st2 = evaluate(send, "document.getElementById('progress').style.display")
                cur = evaluate(send, "(currentResult||{}).task_id || null")
                if st2 == "none" and cur and cur != old_tid:
                    ok2 = True
                    break
                time.sleep(1.5)
            check("点「确认剔除并重算」后发起了新一轮求解并完成", ok2,
                  f"task_id {old_tid} → {cur}")
            if ok2:
                after = evaluate(send, """(() => { const r = currentResult || {}; return {
                    served: r.served_count,
                    excluded: r.excluded_names || [],
                    names: (r.vehicles || []).reduce(
                        (a, v) => a.concat((v.passengers || []).map(p => p.name)), []),
                    total: r.total_cost, pc: r.per_capita_cost
                }; })()""") or {}
                check("参与人数 = 原人数 − 剔除数",
                      after.get("served") == 15 - len(drop_names),
                      f'{after.get("served")} vs 15 - {len(drop_names)}')
                still = sorted(set(drop_names) & set(after.get("names") or []))
                check("被剔除者确实不在方案里（不再参加活动）", not still,
                      f'剔除 {drop_names}；仍在方案 {still}')
                check("excluded_names 回显给前端",
                      set(after.get("excluded") or []) == set(drop_names),
                      str(after.get("excluded")))
                check("人均按「新总费用 ÷ 新参与人数」重算",
                      after.get("pc") is not None and after.get("served")
                      and abs(after["pc"] * after["served"] - after["total"]) < 0.05,
                      f'¥{after.get("total")} ÷ {after.get("served")} 人 '
                      f'= ¥{after.get("pc")}')

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

        # 6b. 乘客分摊明细（A1 费用分摊 + 个人乘车时间）
        pax = evaluate(send, """(() => {
          const r = currentResult;
          const rows = Array.from(document.querySelectorAll('.pax-table tbody tr'));
          const parsed = rows.map(tr => {
            const tds = tr.querySelectorAll('td');
            return { seq: parseInt(tds[0].textContent, 10),
                     cost: parseFloat(tds[2].textContent.replace('¥', '')) };
          });
          const r2 = x => Math.round(x * 100) / 100;
          return {
            tables: document.querySelectorAll('.pax-table').length,
            feet: document.querySelectorAll('.pax-foot').length,
            rows: parsed.length,
            firstSeq: parsed.length ? parsed[0].seq : null,
            anyCost: parsed.filter(p => p.cost > 0).length,
            sums: (r.vehicles || []).map(v => r2((v.passengers || []).reduce((s, p) => s + p.allocated_cost, 0))),
            costs: (r.vehicles || []).map(v => r2(v.cost)),
            paxTotal: (r.vehicles || []).reduce((s, v) => s + (v.passengers || []).length, 0)
          };
        })()""") or {}
        n_veh = (res or {}).get("vehicles", 0)
        check("乘客分摊明细表数量与车数一致", pax.get("tables") == n_veh,
              f'{pax.get("tables")} 表 / {n_veh} 车')
        check("每辆车分摊金额合计等于该车车费", pax.get("sums") == pax.get("costs"),
              f'{pax.get("sums")} vs {pax.get("costs")}')
        check("分摊明细行数等于乘客总数",
              pax.get("rows", 0) > 0 and pax.get("rows") == pax.get("paxTotal"),
              f'{pax.get("rows")} 行 / {pax.get("paxTotal")} 人')
        check("分摊金额已渲染为金额文本", pax.get("anyCost", 0) > 0,
              f'{pax.get("anyCost")} 行含金额')
        check("每车均有分摊脚注（尾差说明）", pax.get("feet") == n_veh,
              f'{pax.get("feet")} 条脚注')

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

        # 8. 降级路径：地图不可用时，地址检索仍应能落点。
        #    旧版 setDestPoint 会直接 pickMap.add / pickMap.setCenter，
        #    地图缺失时抛异常 → 被 geocodeDest 的 catch 吞掉 → 明明检索成功
        #    却显示「未找到该地址」。修复后应只跳过标记绘制，坐标照常写入。
        #    注意：本节会临时把 pickMap 置空，因此放在截图之后，并在 finally 里还原。
        print("\n=== 8. 降级路径：地图缺失时地址检索仍能落点 ===")
        pre = evaluate(send, """(() => ({
          hasMap: typeof pickMap !== 'undefined' && !!pickMap,
          tag: document.getElementById('dest_status').textContent
        }))()""") or {}
        check("前置条件：pickMap 已初始化", bool(pre.get("hasMap")), str(pre))

        evaluate(send, """(() => {
          window.__savedPickMap = pickMap;
          window.__savedDestMarker = destMarker;
          pickMap = null; destMarker = null;          // 模拟高德 JS API 加载失败
          document.getElementById('dest_addr').value = '郑州东站';
        })()""")
        try:
            evaluate(send, "geocodeDest()")
            geo2 = {}
            for _ in range(25):
                geo2 = evaluate(send, """(() => ({
                  tag: document.getElementById('dest_status').textContent,
                  pt: destPoint ? [destPoint.lng, destPoint.lat] : null
                }))()""") or {}
                if geo2.get("tag") in ("已定位", "未找到该地址"):
                    break
                time.sleep(0.5)
            check("无地图时地址检索仍报「已定位」（不再误判为未找到）",
                  geo2.get("tag") == "已定位", f'status={geo2.get("tag")!r}')
            pt = geo2.get("pt")
            check("无地图时仍写入了合法坐标",
                  bool(pt) and abs(pt[0] - 113.7536) < 0.1 and abs(pt[1] - 34.7421) < 0.1,
                  str(pt))
        finally:
            evaluate(send, "pickMap = window.__savedPickMap; "
                           "destMarker = window.__savedDestMarker;")

    finally:
        try:
            ws.close()
        except Exception:
            pass
        kill_edge_tree(proc.pid)
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)

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