#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书推送工具模块 — 通过飞书 API 向指定对话发送消息

从 /root/.hermes/.env 读取 FEISHU_APP_ID / FEISHU_APP_SECRET
以及 FEISHU_HOME_CHANNEL（目标 receive_id）

用法:
    from feishu_push import send_feishu_message
    send_feishu_message("你好，世界")
"""

import os
import json
import time
import logging

logger = logging.getLogger(__name__)

# 缓存 token
_token_cache: dict = {"token": "", "expires_at": 0}

# 默认配置
FEISHU_BASE_URL = "https://open.feishu.cn"
FEISHU_CHAT_ID = ""
FEISHU_APP_ID = ""
FEISHU_APP_SECRET = ""


def _load_credentials():
    """从 .env 加载飞书凭据和 home channel（.env 模板见 .env.example）"""
    global FEISHU_CHAT_ID, FEISHU_APP_ID, FEISHU_APP_SECRET

    if FEISHU_APP_ID and FEISHU_APP_SECRET:
        return  # 已加载

    env_file = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    if key == "FEISHU_APP_ID" and not FEISHU_APP_ID:
                        FEISHU_APP_ID = value
                    elif key == "FEISHU_APP_SECRET" and not FEISHU_APP_SECRET:
                        FEISHU_APP_SECRET = value
                    elif key == "FEISHU_HOME_CHANNEL" and not FEISHU_CHAT_ID:
                        FEISHU_CHAT_ID = value
        except Exception as e:
            logger.error(f"读取 .env 失败: {e}")


def _get_tenant_token() -> str:
    """获取飞书 tenant_access_token（带缓存）"""
    _load_credentials()

    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        logger.error("FEISHU_APP_ID 或 FEISHU_APP_SECRET 未配置")
        return ""

    import requests

    url = f"{FEISHU_BASE_URL}/open-apis/auth/v3/tenant_access_token/internal"
    payload = {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}

    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            token = data["tenant_access_token"]
            expire = data.get("expire", 7200)
            _token_cache["token"] = token
            _token_cache["expires_at"] = now + expire
            return token
        else:
            logger.error(f"获取 token 失败: {data}")
            return ""
    except Exception as e:
        logger.error(f"获取 token 异常: {e}")
        return ""


def send_feishu_message(message: str) -> bool:
    """
    发送飞书文本消息到 home channel

    参数:
        message: 消息文本（纯文本，不支持 Markdown 格式）

    返回:
        是否发送成功
    """
    _load_credentials()

    if not FEISHU_CHAT_ID:
        logger.error("FEISHU_HOME_CHANNEL 未配置")
        return False

    token = _get_tenant_token()
    if not token:
        logger.error("无法获取飞书 token")
        return False

    import requests

    url = f"{FEISHU_BASE_URL}/open-apis/im/v1/messages?receive_id_type=chat_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "receive_id": FEISHU_CHAT_ID,
        "msg_type": "text",
        "content": json.dumps({"text": message}, ensure_ascii=False),
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            logger.info("飞书推送成功")
            return True
        else:
            logger.error(f"飞书推送失败: {data}")
            return False
    except Exception as e:
        logger.error(f"飞书推送异常: {e}")
        return False
