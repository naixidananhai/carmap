# -*- coding: utf-8 -*-
import time
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from baidu_map import BaiduMapClient

client = BaiduMapClient()
print("开始调用...", flush=True)
start = time.time()
r = client.driving_route(34.748, 113.625, 34.7421, 113.7536)
print(f"耗时: {time.time()-start:.2f}秒", flush=True)
print(f"结果: {r}", flush=True)
