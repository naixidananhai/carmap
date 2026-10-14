# -*- coding: utf-8 -*-
"""
A1 费用分摊 与 个人乘车时长 的纯函数单元测试（不需要启动服务器）。

覆盖：
  1. 设计文档 §十一 样例：100 元 / 3 人 -> 33.34 / 33.33 / 33.33（尾差归第一位）
  2. 金额守恒不变式：sum(分摊) 恒等于 round(车费, 2)，20000 组随机用例
  3. 尾差可为负（第一位承担全部舍入偏差），如 16 元 / 6 人
  4. 边界：1 人 / 整除 / 极小金额 / 0 人
  5. 绕路预警阈值：> 80 分钟 或 绕行比例 > 1.3
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from optimizer import (allocate_cost, detour_warning, DETOUR_WARNING,
                       DETOUR_RIDE_LIMIT_S, DETOUR_RATIO_LIMIT)

fails = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        fails.append(msg)


print("=" * 70)
print("【1】设计文档样例")
print("=" * 70)
a = allocate_cost(100, 3)
check(a == [33.34, 33.33, 33.33], f"100 元 / 3 人 -> {a}（期望 [33.34, 33.33, 33.33]）")
check(round(sum(a), 2) == 100.0, f"100 元 / 3 人 合计 = {round(sum(a), 2)}")

print()
print("=" * 70)
print("【2】金额守恒不变式（2 万组随机）")
print("=" * 70)
random.seed(20261014)
bad = []
for _ in range(20000):
    n = random.randint(1, 6)
    total = round(random.uniform(16.0, 500.0), 2)
    alloc = allocate_cost(total, n)
    if len(alloc) != n or round(sum(alloc), 2) != round(total, 2):
        bad.append((total, n, alloc))
check(not bad, f"2 万组随机用例全部满足「分摊合计 == 车费」，不一致 {len(bad)} 组")
if bad:
    for b in bad[:5]:
        print("     ", b)

print()
print("=" * 70)
print("【3】尾差符号（第一位承担全部舍入偏差，可为负）")
print("=" * 70)
a = allocate_cost(16, 6)  # 16/6 = 2.6667 -> 各 2.67，合计 16.02，超 0.02
check(a[0] == 2.65 and a[1] == 2.67, f"16 元 / 6 人 -> {a}，第一位 2.65 < 其余 2.67（承担 -0.02）")
check(round(sum(a), 2) == 16.0, f"16 元 / 6 人 合计 = {round(sum(a), 2)}")

print()
print("=" * 70)
print("【4】边界")
print("=" * 70)
check(allocate_cost(16, 1) == [16.0], "1 人：全额归本人")
check(allocate_cost(50, 4) == [12.5] * 4, "整除：无尾差")
a = allocate_cost(0.01, 3)
check(round(sum(a), 2) == 0.01, f"极小金额 0.01 元 / 3 人 -> {a}，合计仍为 0.01")
check(allocate_cost(100, 0) == [], "0 人：返回空列表")

print()
print("=" * 70)
print("【5】绕路预警阈值")
print("=" * 70)
check(detour_warning(1800, 1500) is None, "30 分 / 25 分（比例 1.2）不预警")
check(detour_warning(5000, 3000) == DETOUR_WARNING, "83 分 / 50 分（比例 1.67）预警")
check(detour_warning(3000, 3000) is None, "直达 == 实际不预警")
check(detour_warning(DETOUR_RIDE_LIMIT_S + 1, 100000) == DETOUR_WARNING,
      f"实际乘车 {DETOUR_RIDE_LIMIT_S + 1}s > 80 分钟，即使比例极低也预警")
check(detour_warning(1800, 0) is None, "直达时间为 0 时不因比例除零误报")
check(detour_warning(int(3000 * DETOUR_RATIO_LIMIT) + 1, 3000) == DETOUR_WARNING,
      f"绕行比例刚过 {DETOUR_RATIO_LIMIT} 即预警")

print()
print("=" * 70)
if fails:
    print(f"结果：{len(fails)} 项失败")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("结果：全部通过")
