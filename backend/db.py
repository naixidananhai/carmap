# -*- coding: utf-8 -*-
"""
SQLite 持久化层（V1.1 / A7）
=============================
依据《拼车路线优化系统数据库设计 + API接口设计 V1.1》：

- 存储引擎：SQLite 3，单文件，随项目目录走（`<项目根>/data/carpool.db`），不装服务；
  路径可用环境变量 `CARPOOL_DB` 覆盖（测试用临时库即靠它）。
- 设计类型映射：`bigint → INTEGER`、`decimal(10,2) → REAL`、
  `datetime → TEXT(ISO8601)`、`json → TEXT(json.dumps)`。
- user 表按 P0/A7 决策整表删除（V1.0 不做登录）。
- 表：carpool_task / passenger / optimization_job / solution /
  vehicle_route / route_passenger / map_matrix。
  （candidate_route 属 A5「Top-K 候选池」，A5 未实现，暂不建表。）

并发模型
--------
FastAPI 的 BackgroundTasks 在线程池里跑 `run_optimization`，而接口在读——
会同时有「事件循环线程」与「工作线程」访问同一个库。因此：

- 每线程一个连接（`threading.local`），不跨线程复用 Connection；
- `journal_mode=WAL` + `busy_timeout`，读写不互相阻塞，偶发写冲突自动重试；
- 写操作统一走 `_execute()`，异常只打印、不向上抛——**持久化失败不应打断优化主流程**。

读写语义
--------
本模块是「写穿缓存」的下层：内存 `TASKS` 仍是读路径的一级来源，
进程重启后由 `load_task()` 从表里恢复（对应设计文档「进程崩溃后从表里恢复状态」）。
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ============ 常量 ============
# 作业状态（optimization_job.status，A8 全量 8 种）
JOB_INIT = "INIT"
JOB_FEASIBLE = "FEASIBLE"
JOB_OPTIMIZING = "OPTIMIZING"
JOB_STOP_GAP = "STOP_GAP"
JOB_STOP_TIMEOUT = "STOP_TIMEOUT"
JOB_STOP_STABLE = "STOP_STABLE"
JOB_OPTIMAL = "OPTIMAL"
JOB_FAILED = "FAILED"

JOB_STATUSES = (JOB_INIT, JOB_FEASIBLE, JOB_OPTIMIZING, JOB_STOP_GAP,
                JOB_STOP_TIMEOUT, JOB_STOP_STABLE, JOB_OPTIMAL, JOB_FAILED)
# 终态：到达后不再变化
JOB_TERMINAL = (JOB_STOP_GAP, JOB_STOP_TIMEOUT, JOB_STOP_STABLE, JOB_OPTIMAL, JOB_FAILED)
JOB_TERMINAL_OK = (JOB_STOP_GAP, JOB_STOP_TIMEOUT, JOB_STOP_STABLE, JOB_OPTIMAL)

# FAILED 细分（A8）
SUB_PROVEN_INFEASIBLE = "PROVEN_INFEASIBLE"
SUB_SEARCH_FAILED = "SEARCH_FAILED"
SUB_STATUSES = (SUB_PROVEN_INFEASIBLE, SUB_SEARCH_FAILED)

# 任务状态（carpool_task.status，设计文档 5 种）
TASK_CREATED = "CREATED"
TASK_MAPPING = "MAPPING"
TASK_OPTIMIZING = "OPTIMIZING"
TASK_COMPLETED = "COMPLETED"
TASK_FAILED = "FAILED"

# 临时数据保留期（设计文档：候选人/矩阵等临时数据 7 天）
TEMP_RETENTION_DAYS = 7

_DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "carpool.db")

_local = threading.local()


def db_path() -> str:
    """当前生效的数据库路径（测试通过 CARPOOL_DB 覆盖）。"""
    return os.environ.get("CARPOOL_DB") or os.path.abspath(_DEFAULT_DB_PATH)


def get_conn() -> sqlite3.Connection:
    """取本线程的连接（惰性创建）。"""
    path = db_path()
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == path:
        return conn
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    _local.conn = conn
    _local.path = path
    return conn


# ============ 建表 ============
SCHEMA = """
-- 2. 拼车任务表
CREATE TABLE IF NOT EXISTS carpool_task (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_code        TEXT    NOT NULL UNIQUE,          -- 对外 8 位短 id
    task_name        TEXT,
    status           TEXT    NOT NULL DEFAULT 'CREATED',
    passenger_count  INTEGER NOT NULL DEFAULT 0,
    destination_name TEXT,
    destination_lng  REAL,
    destination_lat  REAL,
    coordinate_type  TEXT    NOT NULL DEFAULT 'BD09',
    vehicle_mode     TEXT    NOT NULL DEFAULT 'auto',
    optimization_mode TEXT   NOT NULL DEFAULT 'cost',
    direction        TEXT    NOT NULL DEFAULT 'pickup',
    budget_second    INTEGER,
    max_vehicles     INTEGER,
    baseline_cost    REAL,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_created ON carpool_task(created_at DESC);

-- 3. 乘客表
CREATE TABLE IF NOT EXISTS passenger (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  INTEGER NOT NULL REFERENCES carpool_task(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,                          -- 提交顺序（0 起）
    pax_code TEXT,                                      -- P001 / P002 ...
    name     TEXT    NOT NULL,
    address  TEXT,
    lat      REAL,
    lng      REAL,
    status   TEXT                                       -- 提交时解析状态 ok/fail/''
);
CREATE INDEX IF NOT EXISTS idx_passenger_task ON passenger(task_id, seq);

-- 4. 地图矩阵表（点到点距离/时间）
--    V1.1 扩展：键改为「点对」而非 (task_id, from, to)，从而跨任务复用；
--    task_id 仅记录首个写入该点对的任务，便于溯源。
CREATE TABLE IF NOT EXISTS map_matrix (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER,
    from_point      TEXT    NOT NULL,                   -- 'lat,lng' 定点字符串
    to_point        TEXT    NOT NULL,
    distance_meter  REAL,
    duration_second REAL,
    provider        TEXT,
    data_time       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_matrix_pair
    ON map_matrix(provider, from_point, to_point);

-- 6. 优化任务表
CREATE TABLE IF NOT EXISTS optimization_job (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL REFERENCES carpool_task(id) ON DELETE CASCADE,
    algorithm       TEXT    NOT NULL DEFAULT 'ALNS',
    start_time      TEXT,
    end_time        TEXT,
    budget_second   INTEGER,
    status          TEXT    NOT NULL,                   -- 8 态
    sub_status      TEXT,                               -- FAILED 细分
    best_cost       REAL,
    iteration       INTEGER DEFAULT 0,
    candidate_count INTEGER DEFAULT 0,
    gap             REAL,                               -- 相对下界的差距（0.05 = 5%）
    lower_bound     REAL,                               -- 费用下界（gap 的分母，扩展字段）
    stop_reason     TEXT,                               -- 终止原因中文说明（扩展字段）
    error           TEXT,
    elapsed_seconds REAL
);
CREATE INDEX IF NOT EXISTS idx_job_task ON optimization_job(task_id);

-- 7. 方案表
CREATE TABLE IF NOT EXISTS solution (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL REFERENCES carpool_task(id) ON DELETE CASCADE,
    job_id          INTEGER,
    version         INTEGER NOT NULL DEFAULT 1,
    total_cost      REAL,
    total_distance  INTEGER,
    total_duration  INTEGER,
    vehicle_count   INTEGER,
    four_seat_count INTEGER DEFAULT 0,   -- A2：4 座车辆数（按实际载客 ≤4 派车）
    six_seat_count  INTEGER DEFAULT 0,   -- A2：6 座车辆数（载客 5~6 必为 6 座）
    baseline_cost   REAL,
    saving_rate     REAL,
    is_best         INTEGER NOT NULL DEFAULT 1,
    status          TEXT,
    direction       TEXT,
    warning         TEXT,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_solution_task ON solution(task_id, is_best);

-- 8. 车辆路线表
CREATE TABLE IF NOT EXISTS vehicle_route (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    solution_id     INTEGER NOT NULL REFERENCES solution(id) ON DELETE CASCADE,
    vehicle_no      TEXT    NOT NULL,
    vehicle_type    INTEGER DEFAULT 6,                  -- A2：车型 4 / 6 座
    distance_meter  INTEGER,
    duration_second INTEGER,
    cost            REAL,
    polyline        TEXT,                               -- [[lng,lat], ...] json
    route_order     TEXT,                               -- 乘客名顺序 json
    -- V1.1 扩展：让「进程重启后恢复结果」是无损的。
    -- 设计文档只要求 polyline，但前端画站点标记要 coordinates、画时间线要 leg_durations，
    -- 缺这两项时恢复出来的结果只能看折线、看不到站点。
    coordinates     TEXT,                               -- [{lat,lng,name}, ...] json
    leg_durations   TEXT,                               -- [分钟, ...] json
    valid           INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_vehicle_solution ON vehicle_route(solution_id);

-- 9. 路线乘客关联表
CREATE TABLE IF NOT EXISTS route_passenger (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id          INTEGER NOT NULL REFERENCES vehicle_route(id) ON DELETE CASCADE,
    passenger_id      INTEGER,
    passenger_name    TEXT    NOT NULL,
    sequence_no       INTEGER NOT NULL,
    direct_duration   INTEGER,
    ride_duration     INTEGER,
    increase_duration INTEGER,
    detour_ratio      REAL,
    allocated_cost    REAL,
    warning           TEXT
);
CREATE INDEX IF NOT EXISTS idx_rp_route ON route_passenger(route_id, sequence_no);

-- 算法日志（设计文档 §十七）：每次作业的收敛快照
CREATE TABLE IF NOT EXISTS algorithm_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL,
    iteration   INTEGER,
    best_cost   REAL,
    timestamp   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alglog_job ON algorithm_log(job_id, iteration);
"""


# 建表后需要保证存在的「追加列」（老库升级用）：
# 单机工具没有迁移框架，用 PRAGMA table_info + ALTER TABLE 就地补齐即可。
_EXTRA_COLUMNS = {
    "vehicle_route": {
        "coordinates": "TEXT",
        "leg_durations": "TEXT",
    },
    "optimization_job": {
        "lower_bound": "REAL",
        "stop_reason": "TEXT",
    },
}


def _migrate(conn) -> None:
    """给已存在的老库补齐后加的列（幂等）。"""
    for table, cols in _EXTRA_COLUMNS.items():
        try:
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        except Exception:  # noqa: BLE001
            continue
        for col, decl in cols.items():
            if col in have:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                print(f"  [db] 已为老库补充列 {table}.{col}")
            except Exception as e:  # noqa: BLE001
                print(f"  [db] 补列失败 {table}.{col}: {e}")


def init_db() -> str:
    """建表（幂等）。返回当前库路径。"""
    conn = get_conn()
    with conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    return db_path()


# ============ 工具 ============
def now_iso() -> str:
    """ISO8601（本地时区带偏移），设计文档要求 datetime 存 TEXT。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _execute(sql: str, params: Tuple = ()) -> Optional[sqlite3.Cursor]:
    """
    执行写语句并提交。**持久化异常不上抛**——磁盘满/库损坏不应让优化任务崩掉，
    只在控制台留痕（读路径同理，见 _query）。
    """
    try:
        conn = get_conn()
        with conn:
            return conn.execute(sql, params)
    except Exception as e:  # noqa: BLE001
        print(f"  [db] 写入失败: {e} | SQL={sql.splitlines()[0][:60]}...")
        return None


def _query(sql: str, params: Tuple = ()) -> List[sqlite3.Row]:
    try:
        return list(get_conn().execute(sql, params).fetchall())
    except Exception as e:  # noqa: BLE001
        print(f"  [db] 查询失败: {e}")
        return []


def _num(v) -> float:
    """None / 非法值 / NaN 统一成 0.0，避免 'nan' 污染缓存键与 SQL。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if f != f else f


def _point_key(lat: float, lng: float) -> str:
    """坐标 → 'lat,lng' 定点字符串（6 位小数）。用字符串而非 REAL 做键，
    避免浮点比较带来的缓存命中不确定性。"""
    return "%.6f,%.6f" % (_num(lat), _num(lng))


# ============ 任务 ============
def save_task(meta: Dict[str, Any]) -> Optional[int]:
    """插入任务，返回自增主键（失败返回 None）。"""
    cur = _execute(
        """INSERT INTO carpool_task
           (task_code, task_name, status, passenger_count, destination_name,
            destination_lng, destination_lat, coordinate_type, vehicle_mode,
            optimization_mode, direction, budget_second, max_vehicles,
            baseline_cost, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (meta["task_code"], meta.get("task_name"), meta.get("status", TASK_CREATED),
         meta.get("passenger_count", 0), meta.get("destination_name"),
         meta.get("destination_lng"), meta.get("destination_lat"),
         meta.get("coordinate_type", "BD09"), meta.get("vehicle_mode", "auto"),
         meta.get("optimization_mode", "cost"), meta.get("direction", "pickup"),
         meta.get("budget_second"), meta.get("max_vehicles"),
         meta.get("baseline_cost"), now_iso(), now_iso()))
    return cur.lastrowid if cur is not None else None


def set_task_status(task_code: str, status: str, **extra) -> None:
    sets = ["status = ?", "updated_at = ?"]
    params: List[Any] = [status, now_iso()]
    for k, v in extra.items():
        sets.append(f"{k} = ?")
        params.append(v)
    params.append(task_code)
    _execute(f"UPDATE carpool_task SET {', '.join(sets)} WHERE task_code = ?",
             tuple(params))


def save_passengers(task_id: int, passengers: List[Dict[str, Any]]) -> None:
    """整批写乘客（先删后插，保证幂等）。"""
    if task_id is None:
        return
    _execute("DELETE FROM passenger WHERE task_id = ?", (task_id,))
    conn = get_conn()
    try:
        with conn:
            conn.executemany(
                """INSERT INTO passenger
                   (task_id, seq, pax_code, name, address, lat, lng, status)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [(task_id, i, f"P{i+1:03d}", p.get("name") or "",
                  p.get("address"), p.get("lat"), p.get("lng"),
                  p.get("status", "")) for i, p in enumerate(passengers)])
    except Exception as e:  # noqa: BLE001
        print(f"  [db] 乘客写入失败: {e}")


def task_id_of(task_code: str) -> Optional[int]:
    rows = _query("SELECT id FROM carpool_task WHERE task_code = ?", (task_code,))
    return rows[0]["id"] if rows else None


# ============ 优化作业 ============
def create_job(task_id: int, budget_second: int, algorithm: str = "ALNS") -> Optional[int]:
    cur = _execute(
        """INSERT INTO optimization_job
           (task_id, algorithm, start_time, budget_second, status, iteration,
            candidate_count)
           VALUES (?,?,?,?,?,0,0)""",
        (task_id, algorithm, now_iso(), budget_second, JOB_INIT))
    return cur.lastrowid if cur is not None else None


def update_job(job_id: Optional[int], **fields) -> None:
    """更新作业字段。status 变化时自动补 end_time（终态）。"""
    if job_id is None or not fields:
        return
    if fields.get("status") in JOB_TERMINAL:
        fields.setdefault("end_time", now_iso())
    keys = list(fields.keys())
    sets = ", ".join(f"{k} = ?" for k in keys)
    _execute(f"UPDATE optimization_job SET {sets} WHERE id = ?",
             tuple(fields[k] for k in keys) + (job_id,))


def log_iteration(job_id: Optional[int], iteration: int, best_cost: Optional[float]) -> None:
    """算法日志：记录收敛点（设计文档 §十七）。"""
    if job_id is None:
        return
    _execute("INSERT INTO algorithm_log (job_id, iteration, best_cost, timestamp) "
             "VALUES (?,?,?,?)", (job_id, iteration, best_cost, now_iso()))


# ============ 方案 / 车辆 / 乘客明细 ============
def save_solution(task_id: int, job_id: Optional[int], result: Dict[str, Any]) -> Optional[int]:
    """
    写入最终方案（solution + vehicle_route + route_passenger）。
    result 为 TaskResult 的 dict 形式（含 vehicles[].passengers[]）。
    """
    if task_id is None:
        return None
    vehicles = result.get("vehicles") or []
    total_distance = 0.0
    total_duration = 0.0
    for v in vehicles:
        total_distance += float(v.get("distance_km") or 0) * 1000
        total_duration += float(v.get("duration_min") or 0) * 60

    # A2 车型：以 optimize 派定的车型为准（= 按各车实际载客数派车）。
    # 缺字段时（老客户端 / 老结果）按「载客 5~6 为 6 座、其余 6 座」兜底，
    # 即退化为 A2 之前的「全 6 座」口径，不会凭空造出 4 座车。
    vtypes = [int(v.get("vehicle_type") or 6) for v in vehicles]
    four_cnt = result.get("four_seat_count")
    six_cnt = result.get("six_seat_count")
    if four_cnt is None:
        four_cnt = sum(1 for t in vtypes if t == 4)
    if six_cnt is None:
        six_cnt = sum(1 for t in vtypes if t == 6)

    _execute("UPDATE solution SET is_best = 0 WHERE task_id = ?", (task_id,))
    cur = _execute(
        """INSERT INTO solution
           (task_id, job_id, version, total_cost, total_distance, total_duration,
            vehicle_count, four_seat_count, six_seat_count, baseline_cost,
            saving_rate, is_best, status, direction, warning, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
        (task_id, job_id, 3,  # version 3 = 最终方案（V1 初始 / V2 优化 / V3 最终）
         result.get("total_cost"), int(round(total_distance)),
         int(round(total_duration)), result.get("vehicle_count"),
         int(four_cnt), int(six_cnt),
         result.get("baseline_cost"), result.get("saving_rate"),
         "COMPLETED", result.get("direction"), result.get("warning"), now_iso()))
    solution_id = cur.lastrowid if cur is not None else None
    if solution_id is None:
        return None

    conn = get_conn()
    try:
        with conn:
            for v, vt in zip(vehicles, vtypes):
                vcur = conn.execute(
                    """INSERT INTO vehicle_route
                       (solution_id, vehicle_no, vehicle_type, distance_meter,
                        duration_second, cost, polyline, route_order,
                        coordinates, leg_durations, valid)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (solution_id, v.get("vehicle_id"), vt,
                     int(round(float(v.get("distance_km") or 0) * 1000)),
                     int(round(float(v.get("duration_min") or 0) * 60)),
                     v.get("cost"),
                     json.dumps(v.get("path") or [], ensure_ascii=False),
                     json.dumps(v.get("order") or [], ensure_ascii=False),
                     json.dumps(v.get("coordinates") or [], ensure_ascii=False),
                     json.dumps(v.get("leg_durations") or [], ensure_ascii=False),
                     1))
                route_id = vcur.lastrowid
                conn.executemany(
                    """INSERT INTO route_passenger
                       (route_id, passenger_id, passenger_name, sequence_no,
                        direct_duration, ride_duration, increase_duration,
                        detour_ratio, allocated_cost, warning)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    [(route_id, None, p.get("name"), p.get("sequence_no"),
                      p.get("direct_duration_s"), p.get("ride_duration_s"),
                      p.get("increase_duration_s"), p.get("detour_ratio"),
                      p.get("allocated_cost"), p.get("warning"))
                     for p in (v.get("passengers") or [])])
    except Exception as e:  # noqa: BLE001
        print(f"  [db] 方案写入失败: {e}")
    return solution_id


# ============ 读取 / 恢复 ============
def _task_row(task_code: str) -> Optional[sqlite3.Row]:
    rows = _query("SELECT * FROM carpool_task WHERE task_code = ?", (task_code,))
    return rows[0] if rows else None


def load_job(task_code: str) -> Optional[Dict[str, Any]]:
    """取该任务最近一次作业（含 8 态状态），供进程重启后恢复。"""
    rows = _query(
        """SELECT j.* FROM optimization_job j
           JOIN carpool_task t ON t.id = j.task_id
           WHERE t.task_code = ? ORDER BY j.id DESC LIMIT 1""", (task_code,))
    return dict(rows[0]) if rows else None


def load_result(task_code: str, job: Optional[Dict[str, Any]] = None
                ) -> Optional[Dict[str, Any]]:
    """
    从表里重建 TaskResult（用于进程重启后继续对外提供结果）。
    job 为 optimization_job 行时，把 A8 的 8 态状态一并带进结果里。
    """
    rows = _query(
        """SELECT s.* FROM solution s
           JOIN carpool_task t ON t.id = s.task_id
           WHERE t.task_code = ? AND s.is_best = 1
           ORDER BY s.id DESC LIMIT 1""", (task_code,))
    if not rows:
        return None
    sol = dict(rows[0])
    vrows = _query("SELECT * FROM vehicle_route WHERE solution_id = ? ORDER BY id",
                   (sol["id"],))
    vehicles = []
    for v in vrows:
        prows = _query(
            "SELECT * FROM route_passenger WHERE route_id = ? ORDER BY sequence_no",
            (v["id"],))
        vehicles.append({
            "vehicle_id": v["vehicle_no"],
            # A2：车型（4 / 6 座）。老库该列为 NULL 时退化为 6 座
            "vehicle_type": v["vehicle_type"] or 6,
            "passenger_count": len(prows),
            "order": json.loads(v["route_order"] or "[]"),
            "coordinates": json.loads(v["coordinates"] or "[]"),
            "path": json.loads(v["polyline"] or "[]"),
            "leg_durations": json.loads(v["leg_durations"] or "[]"),
            "passengers": [{
                "name": p["passenger_name"],
                "sequence_no": p["sequence_no"],
                "direct_duration_s": p["direct_duration"],
                "ride_duration_s": p["ride_duration"],
                "increase_duration_s": p["increase_duration"],
                "detour_ratio": p["detour_ratio"],
                "allocated_cost": p["allocated_cost"],
                "warning": p["warning"],
            } for p in prows],
            "distance_km": round((v["distance_meter"] or 0) / 1000, 1),
            "duration_min": round((v["duration_second"] or 0) / 60, 1),
            "cost": v["cost"],
        })
    job = job or {}
    return {
        "task_id": task_code,
        "status": "DONE",
        "job_status": job.get("status"),
        "sub_status": job.get("sub_status"),
        "gap": job.get("gap"),
        "lower_bound": job.get("lower_bound"),
        "direction": sol["direction"] or "pickup",
        "total_cost": sol["total_cost"],
        "baseline_cost": sol["baseline_cost"],
        "savings": round((sol["baseline_cost"] or 0) - (sol["total_cost"] or 0), 2),
        "saving_rate": sol["saving_rate"],
        "vehicle_count": sol["vehicle_count"],
        # A2：车型统计随方案一起恢复；老库为 NULL 时按 vehicle_count 全 6 座兜底
        "four_seat_count": sol["four_seat_count"] or 0,
        "six_seat_count": (sol["six_seat_count"]
                           if sol["six_seat_count"] is not None
                           else sol["vehicle_count"] or 0),
        "vehicles": vehicles,
        "error": None,
        "warning": sol["warning"],
        "_restored_from_db": True,   # 标记：结果来自 SQLite 而非进程内存
    }


def load_task(task_code: str) -> Optional[Dict[str, Any]]:
    """任务 + 作业 + 结果（有则带上），供接口在内存缺失时恢复。"""
    trow = _task_row(task_code)
    if trow is None:
        return None
    task = dict(trow)
    job = load_job(task_code)
    return {
        "task": task,
        "job": job,
        "result": load_result(task_code, job),
    }


def list_tasks(limit: int = 20) -> List[Dict[str, Any]]:
    """历史任务列表（按创建时间倒序），带最近一次作业状态与最优费用。"""
    rows = _query(
        """SELECT t.task_code, t.task_name, t.status AS task_status, t.passenger_count,
                  t.optimization_mode, t.vehicle_mode, t.direction, t.created_at,
                  t.max_vehicles, t.budget_second, t.baseline_cost,
                  j.status AS job_status, j.sub_status, j.gap, j.lower_bound,
                  j.best_cost, j.iteration, j.elapsed_seconds, j.stop_reason,
                  s.total_cost, s.vehicle_count, s.four_seat_count, s.six_seat_count,
                  s.saving_rate
           FROM carpool_task t
           LEFT JOIN optimization_job j
                  ON j.id = (SELECT MAX(id) FROM optimization_job WHERE task_id = t.id)
           LEFT JOIN solution s
                  ON s.id = (SELECT MAX(id) FROM solution WHERE task_id = t.id AND is_best = 1)
           ORDER BY t.created_at DESC, t.id DESC
           LIMIT ?""", (int(limit),))
    out = []
    for r in rows:
        out.append(dict(r))
    return out


# ============ 地图矩阵缓存 ============
def matrix_get_many(provider: str,
                    pairs: List[Tuple[float, float, float, float]]
                    ) -> Dict[Tuple[str, str], Tuple[float, float]]:
    """
    批量查询点对缓存。pairs 为 [(o_lat, o_lng, d_lat, d_lng), ...]。
    返回 {(from_key, to_key): (distance_m, duration_s)}。
    """
    if not pairs:
        return {}
    keys = []
    for o_lat, o_lng, d_lat, d_lng in pairs:
        keys.append((provider, _point_key(o_lat, o_lng), _point_key(d_lat, d_lng)))
    out: Dict[Tuple[str, str], Tuple[float, float]] = {}
    # 分块 IN 查询，避免 SQLite 变量上限（默认 999）
    CHUNK = 200
    for start in range(0, len(keys), CHUNK):
        chunk = keys[start:start + CHUNK]
        marks = ",".join(["(?,?,?)"] * len(chunk))
        params: List[Any] = []
        for k in chunk:
            params.extend(k)
        rows = _query(
            f"""SELECT from_point, to_point, distance_meter, duration_second
                FROM map_matrix WHERE (provider, from_point, to_point)
                IN (VALUES {marks})""", tuple(params))
        for r in rows:
            out[(r["from_point"], r["to_point"])] = (
                float(r["distance_meter"] or 0), float(r["duration_second"] or 0))
    return out


def matrix_put_many(provider: str,
                    rows: List[Tuple[float, float, float, float, float, float]],
                    task_id: Optional[int] = None) -> None:
    """写入点对缓存（rows: [(o_lat, o_lng, d_lat, d_lng, dist_m, dur_s), ...]）。"""
    if not rows:
        return
    ts = now_iso()
    data = [(task_id, _point_key(o_lat, o_lng), _point_key(d_lat, d_lng),
             dist, dur, provider, ts)
            for o_lat, o_lng, d_lat, d_lng, dist, dur in rows]
    conn = get_conn()
    try:
        with conn:
            conn.executemany(
                """INSERT INTO map_matrix
                   (task_id, from_point, to_point, distance_meter, duration_second,
                    provider, data_time)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(provider, from_point, to_point) DO UPDATE SET
                     distance_meter = excluded.distance_meter,
                     duration_second = excluded.duration_second,
                     data_time = excluded.data_time""", data)
    except Exception as e:  # noqa: BLE001
        print(f"  [db] 矩阵缓存写入失败: {e}")


def matrix_stats(provider: Optional[str] = None) -> Dict[str, int]:
    sql = "SELECT COUNT(*) AS n FROM map_matrix"
    params: Tuple = ()
    if provider:
        sql += " WHERE provider = ?"
        params = (provider,)
    rows = _query(sql, params)
    return {"pairs": rows[0]["n"] if rows else 0}


# ============ 清理 ============
def prune_temp_data(days: int = TEMP_RETENTION_DAYS) -> Dict[str, int]:
    """
    清理临时数据（设计文档 §十五：候选路线/矩阵等临时数据保留 7 天）。
    永久数据（最终方案、费用、路线）不动。
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc).astimezone() - timedelta(days=days)) \
        .isoformat(timespec="seconds")
    removed = {}
    c = _execute("DELETE FROM map_matrix WHERE data_time < ?", (cutoff,))
    removed["map_matrix"] = c.rowcount if c is not None else 0
    c = _execute("DELETE FROM algorithm_log WHERE timestamp < ?", (cutoff,))
    removed["algorithm_log"] = c.rowcount if c is not None else 0
    return removed


def matrix_lookup(provider: str,
                  pairs: List[Tuple[float, float, float, float]]
                  ) -> List[Optional[Tuple[float, float]]]:
    """
    按输入顺序批量查点对，未命中返回 None。
    pairs 为 [(o_lat, o_lng, d_lat, d_lng), ...]；返回等长列表。
    """
    if not pairs:
        return []
    hits = matrix_get_many(provider, pairs)
    out: List[Optional[Tuple[float, float]]] = []
    for o_lat, o_lng, d_lat, d_lng in pairs:
        out.append(hits.get((point_key(o_lat, o_lng), point_key(d_lat, d_lng))))
    return out


def point_key(lat: float, lng: float) -> str:
    """对外开放的坐标键格式化（与缓存写入完全一致，供上层拼接查询键）。"""
    return _point_key(lat, lng)


if __name__ == "__main__":  # 手工建库：python db.py
    p = init_db()
    print(f"数据库已就绪: {p}")
