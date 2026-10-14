# -*- coding: utf-8 -*-
"""
共享配置：项目根目录 .env 加载
================================
各地图客户端共用，避免重复实现。
不依赖第三方库（python-dotenv 非必需依赖）。
"""

import os
from pathlib import Path


def load_env_file(filename=".env"):
    """
    加载项目根目录下的 .env（若存在）。
    已存在的环境变量优先，不会被 .env 覆盖。
    返回实际生效的键值对，便于调试。
    """
    env_path = Path(__file__).resolve().parent.parent / filename
    loaded = {}
    try:
        if not env_path.exists():
            return loaded
        with env_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if not k:
                    continue
                loaded[k] = v
                if k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass
    return loaded


# 模块导入时自动加载一次
load_env_file()
