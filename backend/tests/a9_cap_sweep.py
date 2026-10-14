# -*- coding: utf-8 -*-
"""
A9 上限扫描实测：同一份名单在不同 `per_capita_cap` 下会怎样
=============================================================
回答业务问题：「上限做成下拉（¥2 步进）后，不同上限值下到底有什么区别？」

- 名单：前端「载入示例」的 15 人（与 e2e / 演示完全一致）；
- 终点：郑州东站（34.7421, 113.7536）；
- 距离矩阵：离线 Haversine（零 API 成本、可复现）；
- 对每个 cap 各跑一次 `suggest_drops`，输出「是否可达 / 建议剔几人 / 剩余 / 车数 / 总费用 / 人均」。

用法：`python tests/a9_cap_sweep.py [> 结果.txt]`
"""
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))
sys.path.insert(0, _HERE)

from optimizer import Passenger, optimize, suggest_drops  # noqa: E402
from p1_alns_benefit import make_haversine_dm             # noqa: E402

# 与 suggest_drops 内部验证求解完全同口径，保证「基准 vs 候选」可比：
# 同种子、同预算（见 optimizer.suggest_drops 里 random.seed(20260101)）
BASE_SEED = 20260101
BASE_BUDGET_S = 1.0

# 与 frontend/index.html 的 DEMO 完全一致
DEMO = [
    ('乘客01', '汉庭酒店郑州东站店', 34.745, 113.740),
    ('乘客02', '全季酒店郑州东站店', 34.742, 113.745),
    ('乘客03', '郑州悦栖酒店', 34.750, 113.735),
    ('乘客04', '郑州大学主校区', 34.813, 113.535),
    ('乘客05', '二七广场', 34.748, 113.658),
    ('乘客06', '郑州火车站', 34.752, 113.657),
    ('乘客07', '紫荆山公园', 34.755, 113.690),
    ('乘客08', '河南博物院', 34.753, 113.618),
    ('乘客09', '凯德广场兴达路店', 34.740, 113.720),
    ('乘客10', '郑州东站西广场', 34.747, 113.760),
    ('乘客11', '绿地新都会', 34.735, 113.745),
    ('乘客12', '郑州国际会展中心', 34.738, 113.730),
    ('乘客13', '曼哈顿商业广场', 34.730, 113.700),
    ('乘客14', '航海体育场', 34.720, 113.680),
    ('乘客15', '市体育中心', 34.770, 113.740),
]
DEST = (34.7421, 113.7536)
CAPS = [4, 6, 8, 10, 12, 14, 16, 18, 20, 22]


def main():
    ps = [Passenger(id=f"p{i+1}", name=name, lat=lat, lng=lng)
          for i, (name, _addr, lat, lng) in enumerate(DEMO)]
    dest = Passenger(id="DEST", name="终点", lat=DEST[0], lng=DEST[1])
    dm = make_haversine_dm([(p.lat, p.lng) for p in ps] + [DEST])

    print("=" * 88)
    print("A9 上限扫描实测（示例名单 15 人 · 郑州东站 · 离线 Haversine）")
    print("=" * 88)

    random.seed(BASE_SEED)
    best, _ = optimize(ps, dest, dm, mode="cost", time_budget_s=BASE_BUDGET_S)
    total = sum(v.cost for v in best)
    n = len(ps)
    print(f"\n基准（不剔人）：{len(best)} 辆车，总费用 ¥{total:.2f}，"
          f"人均 ¥{total / n:.2f}")
    print(f"（基准与候选同口径：seed={BASE_SEED}、预算 {BASE_BUDGET_S}s，"
          f"故「剔前 / 剔后」可直接对比）\n")

    hdr = (f"{'cap':>6} {'是否可达':>8} {'剔N':>4} {'剩余':>4} {'车数':>4} "
           f"{'总费用':>9} {'人均':>8}  策略")
    print(hdr)
    print("-" * 88)
    rows = []
    for cap in CAPS:
        r = suggest_drops(ps, dest, dm, float(cap), current_vehicles=best,
                          current_total=total, verify_top=4, verify_budget_s=1.0)
        if not r or not r.get("best"):
            print(f"{'¥' + str(cap):>6} {'—':>9}   （无候选）")
            continue
        b = r["best"]
        ok = "✔ 可达" if r["achievable"] else "✗ 不可达"
        print(f"{'¥' + str(cap):>6} {ok:>10} {b['drop_count']:>4} {b['keep_count']:>4} "
              f"{b['vehicle_count']:>4} {'¥%.2f' % b['total_cost']:>9} "
              f"{'¥%.2f' % b['per_capita']:>8}  {b['strategy']}")
        rows.append((cap, r["achievable"], b, len(r["suggestions"])))

    print("-" * 88)
    print("\n各上限下的可选项数量（前端「备选方案区」会摊开的部分）：")
    for cap, ok, b, k in rows:
        print(f"  cap ¥{cap:<3} → 已验证候选 {k} 个，"
              f"{'可达' if ok else '不可达'}，最优建议 = 剔 {b['drop_count']} 人 / "
              f"人均 ¥{b['per_capita']:.2f}")

    print("\n结论要点（全部由本次实测数据得出，不写死结论）：")
    reach = [c for c, ok, _, _ in rows if ok]
    unreach = [c for c, ok, _, _ in rows if not ok]
    if reach:
        line = f"  · 本份名单在 cap ≥ ¥{min(reach)} 时即可达标"
        if unreach:
            line += f"；cap ≤ ¥{max(unreach)} 时**不可达**（低于该出行的物理下限）"
        print(line + "。")
    jumps = [(rows[i][0], rows[i][2]["drop_count"],
              rows[i + 1][0], rows[i + 1][2]["drop_count"])
             for i in range(len(rows) - 1)
             if rows[i][2]["drop_count"] != rows[i + 1][2]["drop_count"]]
    if jumps:
        c0, d0, c1, d1 = jumps[0]
        print(f"  · **阶梯效应**：cap 从 ¥{c0} 放宽到 ¥{c1}，建议就从「剔 {d0} 人」"
              f"回到「剔 {d1} 人」——总费用是车数的阶梯函数，跨过阈值只能靠压车数，"
              f"建议会突变而不是平滑变化。")
    print("  · 上限越低，算法越倾向多剔人 / 整车剔除（唯一能大幅降总费用的是减少车数）。")
    print("  · 数值口径：本脚本用离线 Haversine 矩阵，绝对值与真实路网矩阵有差异；"
          "但「阶梯」与「不可达」这些结构性结论与矩阵无关。")


if __name__ == "__main__":
    main()
