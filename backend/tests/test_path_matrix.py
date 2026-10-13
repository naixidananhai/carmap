# -*- coding: utf-8 -*-
"""验证：parse_path_points 解析 + build_full_matrix 返回 (matrix, path_matrix)"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from baidu_map import parse_path_points, BaiduMapClient

# 1. 解析函数单测（含真实样本与边界情况）
real = "113.619515,34.754402;113.619794,34.754396;113.619993,34.754350"
pts = parse_path_points(real)
assert len(pts) == 3, pts
assert pts[0] == (113.619515, 34.754402), pts[0]
assert parse_path_points("") == []
assert parse_path_points(None) == []
assert len(parse_path_points("113.6,34.7|113.7,34.8")) == 2          # 兼容 | 分隔
assert len(parse_path_points("113.6,34.7;垃圾;113.7,34.8")) == 2      # 跳过坏数据
print("[1] parse_path_points 通过")

# 2. 真实调用 build_full_matrix
client = BaiduMapClient()
points = [
    (34.7545, 113.6195),  # 中原区
    (34.7750, 113.7400),  # 金水区东
    (34.7421, 113.7536),  # 郑州东站
]
matrix, path_matrix = client.build_full_matrix(points)

assert len(matrix) == 3 and len(path_matrix) == 3
for i in range(3):
    assert matrix[i][i] == (0, 0)
    assert path_matrix[i][i] == []
for i in range(3):
    for j in range(3):
        if i == j:
            continue
        cell = matrix[i][j]
        assert cell is not None and len(cell) == 2, (i, j, cell)
        seg = path_matrix[i][j]
        assert seg and len(seg) >= 2, (i, j, seg and len(seg))
        # 每个点都是 (lng, lat) 且量级合理（郑州范围）
        for lng, lat in seg:
            assert 113.0 < lng < 114.0 and 34.0 < lat < 35.0, (lng, lat)
print(f"[2] build_full_matrix 通过：matrix 为 2 元组，path_matrix 每段 "
      f"{min(len(path_matrix[i][j]) for i in range(3) for j in range(3) if i != j)}"
      f"-{max(len(path_matrix[i][j]) for i in range(3) for j in range(3) if i != j)} 个点")

# 3. 验证路径首尾与起终点吻合
seg = path_matrix[0][2]  # 中原区 -> 郑州东站
print(f"    中原区->东站: {matrix[0][2][0]/1000:.1f}km, 路径点 {len(seg)} 个, "
      f"起点 {seg[0]}, 终点 {seg[-1]}")
print("ALL OK")
