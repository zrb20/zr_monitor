#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
独立的爱快设备名称抓取模块
通过爱快 /Action/call API 获取终端设备名称
独立运行，不阻塞 silent_collector.py 的主循环
"""

import json
import os
import sys
import time
import hashlib
import requests

# OUI 厂商识别兜底
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from oui_mapping import lookup_oui
from env_config import get as env_get

MAX_ATTEMPTS = 5  # 同一 MAC 查名失败 5 次后自动移除

WORK_DIR = os.path.expanduser("~/zr_monitor")
QUEUE_FILE = os.path.join(WORK_DIR, "ikuai_lookup_queue.json")
MAC_NAME_MAPPING_PATH = os.path.join(WORK_DIR, "mac_name_mapping.json")
JSON_DB_PATH = os.path.join(WORK_DIR, "device_info.json")

# 从 .env 读取（模板见 .env.example）
ROUTER_URL = env_get("IKUAI_URL", "http://192.168.203.1")
ROUTER_USER = env_get("IKUAI_USER", "admin")
ROUTER_PASS = env_get("IKUAI_PASS")


def load_queue():
    """加载待查队列，返回 (pending_macs, attempts_dict)"""
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                pending = data.get("pending", [])
                attempts = data.get("attempts", {})
                return pending, attempts
        except (json.JSONDecodeError, IOError):
            pass
    return [], {}


def save_queue(pending, attempts=None):
    """保存待查队列"""
    data = {"pending": pending, "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")}
    if attempts:
        data["attempts"] = attempts
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def load_mac_name_mapping():
    """加载 MAC 地址到设备名称的映射"""
    if os.path.exists(MAC_NAME_MAPPING_PATH):
        try:
            with open(MAC_NAME_MAPPING_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_mac_name_mapping(mapping):
    """保存 MAC 地址到设备名称的映射"""
    with open(MAC_NAME_MAPPING_PATH, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=4)


def load_device_db():
    """加载设备数据库"""
    if os.path.exists(JSON_DB_PATH):
        try:
            with open(JSON_DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_device_db(db):
    """保存设备数据库"""
    with open(JSON_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=4)


def get_ikuai_terminal_data() -> list:
    """
    通过爱快 HTTP API 获取终端设备列表
    返回: 设备列表 [{mac, ip, comment, username}, ...]
    """
    s = requests.Session()
    
    # 登录
    md5_pass = hashlib.md5(ROUTER_PASS.encode()).hexdigest()
    r = s.post(f"{ROUTER_URL}/Action/login", json={"username": ROUTER_USER, "passwd": md5_pass})
    if r.json().get('code') != 0:
        print("[fetch_ikuai_names] 登录失败", file=sys.stderr)
        return []
    
    # 获取终端数据
    r = s.post(f"{ROUTER_URL}/Action/call", json={"func_name": "monitor_lanip", "action": "show"})
    data = r.json()
    
    if data.get('code') != 0:
        print(f"[fetch_ikuai_names] API 调用失败: {data.get('message')}", file=sys.stderr)
        return []
    
    return data.get('results', {}).get('data', [])


def fetch_ikuai_device_names(macs_to_lookup: list) -> dict:
    """
    通过爱快 API 获取指定 MAC 的设备名称
    macs_to_lookup: 需要查询名称的 MAC 地址列表
    返回: {mac: device_name} 映射
    """
    if not macs_to_lookup:
        return {}

    mapping = {}
    
    try:
        terminals = get_ikuai_terminal_data()
        if not terminals:
            return mapping
        
        lookup_set = set(m.lower() for m in macs_to_lookup)
        
        for t in terminals:
            mac = t.get('mac', '').lower()
            comment = t.get('comment', '')
            username = t.get('username', '')
            client_model = t.get('client_model', '')
            hostname = t.get('hostname', '')
            client_vendor = t.get('client_vendor', '')

            # 按优先级取第一个非空值
            name = ''
            for candidate in [comment, username, client_model, hostname, client_vendor]:
                if candidate:
                    name = candidate
                    break

            # URL decode comment (爱快会 encode 空格等字符)
            from urllib.parse import unquote
            name = unquote(name)

            if mac in lookup_set:
                mapping[mac] = name

    except Exception as e:
        print(f"[fetch_ikuai_names] API 获取名称失败: {e}", file=sys.stderr)

    return mapping


def main():
    """主流程：加载队列 → API查名 → 更新映射和数据库"""
    print(f"[fetch_ikuai_names] 开始执行，时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")

    # 加载待查队列（含尝试次数记录）
    pending_macs, attempts = load_queue()
    if not pending_macs:
        print("[fetch_ikuai_names] 队列为空，无需处理")
        return

    print(f"[fetch_ikuai_names] 待查 MAC 数量：{len(pending_macs)}")

    # 通过 API 获取名称
    ikuai_names = fetch_ikuai_device_names(pending_macs)

    if not ikuai_names:
        # 本轮未查到任何名称 → 递增尝试次数，淘汰超限的
        evicted = []
        remaining = []
        for mac in pending_macs:
            attempts[mac] = attempts.get(mac, 0) + 1
            if attempts[mac] >= MAX_ATTEMPTS:
                # 淘汰前尝试 OUI 兜底（iKuai始终查不到，至少标厂商）
                oui_name = lookup_oui(mac)
                if oui_name:
                    name = f"{oui_name}设备"
                    print(f"[fetch_ikuai_names] 🔍 OUI兜底: {mac} -> {name}")
                    # 直接写入映射和数据库
                    current_mapping = load_mac_name_mapping()
                    if mac not in current_mapping:
                        current_mapping[mac] = name
                        save_mac_name_mapping(current_mapping)
                    device_db = load_device_db()
                    if mac in device_db:
                        device_db[mac]["设备名称"] = name
                        save_device_db(device_db)
                else:
                    evicted.append(mac)
                    print(f"[fetch_ikuai_names] ✗ {mac} 已尝试 {attempts[mac]} 次仍失败，自动移除（可能不在爱快DHCP中）")
            else:
                remaining.append(mac)

        if evicted:
            print(f"[fetch_ikuai_names] 已淘汰 {len(evicted)} 个无法查名的 MAC")
        if remaining:
            print(f"[fetch_ikuai_names] 剩余 {len(remaining)} 个 MAC 等待下次重试")
            save_queue(remaining, attempts)
        else:
            save_queue([], attempts)
            print("[fetch_ikuai_names] 队列已清空")
        return

    print(f"[fetch_ikuai_names] 获取到 {len(ikuai_names)} 个设备名称")

    # 更新 mac_name_mapping.json
    current_mapping = load_mac_name_mapping()
    updated_macs = []
    for mac, name in ikuai_names.items():
        if mac not in current_mapping:
            current_mapping[mac] = name
            updated_macs.append(mac)

    if updated_macs:
        save_mac_name_mapping(current_mapping)
        print(f"[fetch_ikuai_names] 已更新 {len(updated_macs)} 个设备名称到 mac_name_mapping.json")

    # 更新 device_info.json 中的设备名称
    device_db = load_device_db()
    for mac in updated_macs:
        if mac in device_db:
            device_db[mac]["设备名称"] = ikuai_names[mac]
            print(f"[fetch_ikuai_names] 已更新设备名称：{mac} -> {ikuai_names[mac]}")

    if updated_macs:
        save_device_db(device_db)

    # 从队列中移除已处理的 MAC，重置尝试次数
    remaining = [mac for mac in pending_macs if mac not in ikuai_names]
    # 重置剩余 MAC 的尝试次数（保留但不递增）
    save_queue(remaining, attempts)

    if remaining:
        print(f"[fetch_ikuai_names] 剩余 {len(remaining)} 个未处理的 MAC（可能离线），保留在队列中")
        for mac in remaining:
            print(f"  未找到: {mac}")
    else:
        print("[fetch_ikuai_names] 所有 MAC 已处理，队列清空")

    print("[fetch_ikuai_names] 执行完成")


if __name__ == "__main__":
    main()
