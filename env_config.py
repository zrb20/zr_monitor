#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZR Monitor 统一配置加载模块
从仓库本地 .env 文件读取凭据（IP/用户名/密码等敏感配置不入库）。
.env 文件格式: KEY=VALUE 每行一条，# 开头为注释。
"""

import os


def _find_env_file():
    """定位 .env 文件：优先仓库目录，回退用户主目录。"""
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),  # 仓库内
        os.path.expanduser("~/zr_monitor/.env"),  # 运行目录
        os.path.expanduser("~/.hermes/.env"),  # Hermes 全局
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]


def load_env():
    """读取 .env 到 dict，不存在返回空 dict。"""
    env = {}
    path = _find_env_file()
    if not os.path.exists(path):
        return env
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        pass
    return env


_ENV_CACHE = None


def get(key, default=""):
    """读取配置项（带缓存）。"""
    global _ENV_CACHE
    if _ENV_CACHE is None:
        _ENV_CACHE = load_env()
    return _ENV_CACHE.get(key, default)
