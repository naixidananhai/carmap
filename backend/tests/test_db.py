# -*- coding: utf-8 -*-
"""
A7 单元测试 —— SQLite 持久化层
===============================
覆盖（全部离线，用临时库，不触碰 data/carpool.db）：

  1. 建表幂等 + 表齐全
  2. 老库补列（模拟缺 coordinates/leg_durations 的历史库）
  3. 任务 / 名单 / 作业 写入与状态跃迁
  4. 终态自动补 end_time
  5. 方案落库后 load_result 能**无损**重建（含 polyline / coordinates /
     leg_durations / 乘客分摊明细）
  6. list_tasks 关联出最近一次作业状态
  7. 矩阵点对缓存的写/读/未命中/跨进程复用/过期清理
  8. 「精确到 6 位小数的坐标」在两套客户端间产生相同缓存键（跨任务复用的前提）
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def fresh_db():
    """每个测试用独立临时库（CARPOOL_DB 环境变量切换，db 模块按线程缓存连接）。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="carpool_test_")
    os.close(fd)
    os.unlink(path)
    os.environ["CARPOOL_DB"] = path
    import db
    import importlib
    importlib.reload(db)
    db.init_db()
    return db, path


def main():
    db, path = fresh_db()
    print(f"临时库：{path}\n")

    # ---- 1. 建表 ----
    print("=== 1. 建表幂等与表齐全 ===")
    db.init_db()
    db.init_db()   # 再来两次不应抛错
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    want = {"carpool_task", "passenger", "map_matrix", "optimization_job",
            "solution", "vehicle_route", "route_passenger", "algorithm_log"}
    check("8 张表齐全（含 A7 决策里删掉 user 表）", want <= names,
          f"缺 {sorted(want - names)}" if want - names else f"{len(want)} 张")
    check("不存在 user 表（V1.0 不做登录）", "user" not in names)

    # ---- 2. 老库补列 ----
    print("\n=== 2. 老库升级：补列 ===")
    conn.execute("DROP TABLE vehicle_route")
    conn.execute("""CREATE TABLE vehicle_route (
        id INTEGER PRIMARY KEY AUTOINCREMENT, solution_id INTEGER, vehicle_no TEXT,
        vehicle_type INTEGER, distance_meter INTEGER, duration_second INTEGER,
        cost REAL, polyline TEXT, route_order TEXT, valid INTEGER)""")
    conn.commit()
    conn.close()
    import importlib
    importlib.reload(db)
    db.init_db()
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(vehicle_route)")}
    check("老库自动补出 coordinates / leg_durations", {"coordinates", "leg_durations"} <= cols,
          str(sorted(cols)))

    # ---- 3. 任务与作业 ----
    print("\n=== 3. 任务 / 名单 / 作业状态 ===")
    code = "t7a1b2c3"
    tid = db.save_task({"task_code": code, "task_name": "单测任务", "passenger_count": 2,
                        "destination_name": "郑州东站", "destination_lng": 113.7536,
                        "destination_lat": 34.7421, "optimization_mode": "cost",
                        "direction": "pickup", "budget_second": 10})
    check("任务已落库且拿到自增 id", isinstance(tid, int) and tid > 0, f"id={tid}")
    db.save_passengers(tid, [{"name": "张三", "address": "A", "lat": 34.70, "lng": 113.70,
                              "status": "ok"},
                             {"name": "李四", "address": "B", "lat": 34.71, "lng": 113.71,
                              "status": "ok"}])
    prows = sqlite3.connect(path).execute(
        "SELECT name, pax_code, lat FROM passenger WHERE task_id=? ORDER BY seq", (tid,)).fetchall()
    check("名单按提交顺序落库", [r[0] for r in prows] == ["张三", "李四"],
          str([tuple(r) for r in prows]))

    db.set_task_status(code, db.TASK_OPTIMIZING)
    jid = db.create_job(tid, 10)
    check("作业创建即 INIT", db.load_job(code)["status"] == db.JOB_INIT)

    db.update_job(jid, status=db.JOB_FEASIBLE, best_cost=250.0)
    j = db.load_job(code)
    check("FEASIBLE 状态可写可读", j["status"] == "FEASIBLE" and j["best_cost"] == 250.0)
    check("非终态不写 end_time", not j["end_time"], str(j["end_time"]))

    db.update_job(jid, status=db.JOB_STOP_STABLE, iteration=900, candidate_count=900,
                  elapsed_seconds=4.0, lower_bound=47.55, gap=1.8516,
                  stop_reason="连续 3 秒无改善")
    j = db.load_job(code)
    check("8 态之一的 STOP_STABLE 可写可读", j["status"] == "STOP_STABLE")
    check("终态自动补 end_time", bool(j["end_time"]), str(j["end_time"]))
    check("gap / lower_bound / stop_reason 落库", j["gap"] == 1.8516
          and j["lower_bound"] == 47.55 and "无改善" in j["stop_reason"])

    db.log_iteration(jid, 100, 250.0)
    n_log = sqlite3.connect(path).execute(
        "SELECT COUNT(*) FROM algorithm_log WHERE job_id=?", (jid,)).fetchone()[0]
    check("算法日志可写入", n_log == 1, f"{n_log} 行")

    # ---- 4. 方案无损重建 ----
    print("\n=== 4. 方案落库 → 无损重建 ===")
    result = {
        "total_cost": 114.88, "baseline_cost": 201.62, "saving_rate": 43.0,
        "vehicle_count": 1, "direction": "pickup", "warning": None,
        "vehicles": [{
            "vehicle_id": "V01", "distance_km": 27.3, "duration_min": 75.8, "cost": 114.88,
            "order": ["张三", "李四"],
            "coordinates": [{"lat": 34.70, "lng": 113.70, "name": "张三"},
                            {"lat": 34.71, "lng": 113.71, "name": "李四"},
                            {"lat": 34.7421, "lng": 113.7536, "name": "终点"}],
            "path": [{"lat": 34.70, "lng": 113.70}, {"lat": 34.7421, "lng": 113.7536}],
            "leg_durations": [12.5, 63.3],
            "passengers": [
                {"name": "张三", "sequence_no": 1, "direct_duration_s": 1177,
                 "ride_duration_s": 4546, "increase_duration_s": 3369, "detour_ratio": 3.86,
                 "allocated_cost": 57.45, "warning": "DETOUR_WARNING"},
                {"name": "李四", "sequence_no": 2, "direct_duration_s": 1300,
                 "ride_duration_s": 3369, "increase_duration_s": 2069, "detour_ratio": 2.59,
                 "allocated_cost": 57.43, "warning": "DETOUR_WARNING"},
            ],
        }],
    }
    sid = db.save_solution(tid, jid, result)
    check("方案已落库", isinstance(sid, int) and sid > 0, f"solution_id={sid}")

    got = db.load_result(code, db.load_job(code))
    check("重建后顶层字段一致",
          got["total_cost"] == 114.88 and got["vehicle_count"] == 1
          and got["baseline_cost"] == 201.62, json.dumps(
              {k: got[k] for k in ("total_cost", "vehicle_count", "baseline_cost")}))
    v = got["vehicles"][0]
    check("重建后车辆信息一致",
          v["vehicle_id"] == "V01" and v["order"] == ["张三", "李四"]
          and v["distance_km"] == 27.3 and v["cost"] == 114.88)
    check("折线（polyline）完整还原", v["path"] == result["vehicles"][0]["path"],
          f'{len(v["path"])} 点')
    check("站点坐标完整还原", v["coordinates"] == result["vehicles"][0]["coordinates"],
          f'{len(v["coordinates"])} 点')
    check("逐段耗时完整还原", v["leg_durations"] == [12.5, 63.3], str(v["leg_durations"]))
    check("乘客分摊明细完整还原",
          [p["name"] for p in v["passengers"]] == ["张三", "李四"]
          and abs(sum(p["allocated_cost"] for p in v["passengers"]) - 114.88) < 0.005,
          str([p["allocated_cost"] for p in v["passengers"]]))
    check("A8 状态透传进结果",
          got["job_status"] == "STOP_STABLE" and got["gap"] == 1.8516)
    check("标记结果来自 SQLite", got.get("_restored_from_db") is True)

    # is_best 语义：再存一版，只有最新一版是 best
    db.save_solution(tid, jid, dict(result, total_cost=110.0))
    got2 = db.load_result(code, db.load_job(code))
    check("重存方案后只有最新一版 is_best", got2["total_cost"] == 110.0,
          f'{got2["total_cost"]}')
    n_sol = sqlite3.connect(path).execute(
        "SELECT COUNT(*) FROM solution WHERE task_id=? AND is_best=1", (tid,)).fetchone()[0]
    check("is_best=1 唯一", n_sol == 1, f"{n_sol} 条")

    # ---- 5. list_tasks ----
    print("\n=== 5. 历史任务列表 ===")
    rows = db.list_tasks(10)
    check("列表返回任务", len(rows) == 1 and rows[0]["task_code"] == code)
    r0 = rows[0]
    check("列表带最近一次作业的 8 态状态",
          r0["job_status"] == "STOP_STABLE" and r0["sub_status"] is None, str(r0["job_status"]))
    check("列表带方案摘要（费用/车数/差额）",
          r0["total_cost"] == 110.0 and r0["vehicle_count"] == 1,
          f'{r0["total_cost"]} / {r0["vehicle_count"]}')
    check("列表带 gap 与下界", r0["gap"] == 1.8516 and r0["lower_bound"] == 47.55)

    # ---- 6. 矩阵缓存 ----
    print("\n=== 6. 地图矩阵点对缓存 ===")
    db.matrix_put_many("amap", [(34.70, 113.70, 34.7421, 113.7536, 8200.0, 1200.0)])
    hits = db.matrix_lookup("amap", [(34.70, 113.70, 34.7421, 113.7536),
                                     (1.0, 2.0, 3.0, 4.0)])
    check("命中已缓存点对", hits[0] == (8200.0, 1200.0), str(hits[0]))
    check("未命中返回 None", hits[1] is None, str(hits[1]))
    db.matrix_put_many("amap", [(34.70, 113.70, 34.7421, 113.7536, 8300.0, 1210.0)])
    hits2 = db.matrix_lookup("amap", [(34.70, 113.70, 34.7421, 113.7536)])
    check("同点对重复写是 UPSERT（不新增行）",
          hits2[0] == (8300.0, 1210.0) and db.matrix_stats("amap")["pairs"] == 1,
          str(hits2[0]))
    check("provider 隔离", db.matrix_lookup("baidu", [(34.70, 113.70, 34.7421, 113.7536)])[0]
          is None)
    check("6 位小数坐标在两侧生成同一缓存键",
          db.point_key(34.7, 113.7) == db.point_key(34.6999999999, 113.7000000001)
          == "34.700000,113.700000", db.point_key(34.7, 113.7))
    check("prune 不误删新鲜数据", db.prune_temp_data(7)["map_matrix"] == 0)

    # 跨进程复用：重连后仍命中
    importlib.reload(db)
    db.init_db()
    hits3 = db.matrix_lookup("amap", [(34.70, 113.70, 34.7421, 113.7536)])
    check("模拟进程重启后缓存仍命中", hits3[0] == (8300.0, 1210.0), str(hits3[0]))

    # 过期清理
    old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 9 * 86400))
    conn = sqlite3.connect(path)
    conn.execute("UPDATE map_matrix SET data_time=?", (old,))
    conn.commit()
    conn.close()
    removed = db.prune_temp_data(7)
    check("过期临时数据被清理（设计文档 7 天）", removed["map_matrix"] == 1, str(removed))
    check("清理后查不到", db.matrix_lookup("amap", [(34.70, 113.70, 34.7421, 113.7536)])[0]
          is None)

    # ---- 7. 持久化失败不打断主流程 ----
    print("\n=== 7. 容错 ===")
    os.environ["CARPOOL_DB"] = "Z:/nonexistent_dir/x.db"
    importlib.reload(db)
    try:
        ok = True
        db.save_task({"task_code": "x"})
        db.update_job(None, status="FAILED")
    except Exception as e:  # noqa: BLE001
        ok = False
        print("     异常:", e)
    check("写库失败只打印、不上抛（不打断优化主流程）", ok)

    print("\n" + "=" * 60)
    print(f"结果：{len(PASS)}/{len(PASS) + len(FAIL)} 项通过")
    if FAIL:
        print("失败项：" + "，".join(FAIL))
    print("=" * 60)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
