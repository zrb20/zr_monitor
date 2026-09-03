#!/usr/bin/env python3
import requests
# -*- coding: utf-8 -*-

import os
import json
import csv
import datetime
import subprocess
import re
import sys
import hashlib
from urllib.parse import unquote

# ================= OUI 厂商识别 =================
from oui_mapping import lookup_oui, format_device_name

# ================= 环境配置 =================
from env_config import get as env_get

# ================= 核心配置区 =================
WORK_DIR = os.path.expanduser("~/zr_monitor")
DEP_MARKER = os.path.join(WORK_DIR, ".deps_installed")
JSON_DB_PATH = os.path.join(WORK_DIR, "device_info.json")
CSV_DIR_BASE = os.path.join(WORK_DIR, "matrix_data")
PVE_LOG_PATH = os.path.join(WORK_DIR, "pve_link_status.log")
MAC_NAME_MAPPING_PATH = os.path.join(WORK_DIR, "mac_name_mapping.json")

# ================= 飞书推送 =================
from feishu_push import send_feishu_message

# 推送状态标记（避免重复推送）
PUSHED_DEVICES_FILE = os.path.join(WORK_DIR, "pushed_devices.json")

def send_telegram_message(message: str) -> bool:
    """发送飞书消息（替代原 Telegram 推送）"""
    return send_feishu_message(message)


def load_pushed_devices():
    """加载已推送的设备列表"""
    if os.path.exists(PUSHED_DEVICES_FILE):
        try:
            with open(PUSHED_DEVICES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_pushed_devices(push_history):
    """保存已推送的设备列表"""
    try:
        with open(PUSHED_DEVICES_FILE, 'w', encoding='utf-8') as f:
            json.dump(push_history, f, ensure_ascii=False, indent=4)
    except IOError:
        pass


def load_mac_name_mapping():
    """加载 MAC 地址到设备名称的映射"""
    mapping = {}
    if os.path.exists(MAC_NAME_MAPPING_PATH):
        try:
            with open(MAC_NAME_MAPPING_PATH, 'r', encoding='utf-8') as f:
                mapping = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return mapping


# 全局设备名称映射
DEVICE_NAME_MAP = load_mac_name_mapping()

# ================= 爱快 API 配置 =================
# 从 .env 读取（模板见 .env.example）
ROUTER_URL = env_get("IKUAI_URL", "http://192.168.203.1")
ROUTER_USER = env_get("IKUAI_USER", "admin")
ROUTER_PASS = env_get("IKUAI_PASS")


def get_ikuai_terminal_data() -> list:
    """
    通过爱快 HTTP API 获取终端设备列表
    返回: 设备列表 [{mac, ip, comment, username}, ...]
    """
    s = requests.Session()

    # 登录
    md5_pass = hashlib.md5(ROUTER_PASS.encode()).hexdigest()
    r = s.post(f"{ROUTER_URL}/Action/login", json={"username": ROUTER_USER, "passwd": md5_pass}, timeout=10)
    if r.json().get('code') != 0:
        return []

    # 获取终端数据
    r = s.post(f"{ROUTER_URL}/Action/call", json={"func_name": "monitor_lanip", "action": "show"}, timeout=10)
    data = r.json()

    if data.get('code') != 0:
        return []

    return data.get('results', {}).get('data', [])


def lookup_ikuai_device_name(mac: str) -> str:
    """
    实时查询单个 MAC 在爱快后台的设备名称
    返回: 设备名称字符串，查不到返回空字符串
    优先级: comment > username > client_model > hostname > client_vendor
    """
    try:
        terminals = get_ikuai_terminal_data()
        mac_lower = mac.lower()
        for t in terminals:
            if t.get('mac', '').lower() == mac_lower:
                comment = t.get('comment', '')
                username = t.get('username', '')
                client_model = t.get('client_model', '')
                hostname = t.get('hostname', '')
                client_vendor = t.get('client_vendor', '')

                # 按优先级取第一个非空值
                for candidate in [comment, username, client_model, hostname, client_vendor]:
                    if candidate:
                        return unquote(candidate)
    except Exception:
        pass
    return ""


# ================= 爱快查名队列 =================
# 未知 MAC 写入此队列，由独立的 fetch_ikuai_names.py 定期处理（兜底）
QUEUE_FILE = os.path.join(WORK_DIR, "ikuai_lookup_queue.json")


def enqueue_unknown_macs(macs):
    """将未知 MAC 加入爱快查名队列"""
    if not macs:
        return
    # 加载现有队列
    existing = []
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                existing = data.get("pending", [])
        except (json.JSONDecodeError, IOError):
            pass
    # 去重追加
    for mac in macs:
        if mac not in existing:
            existing.append(mac)
    # 保存队列
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump({"pending": existing, "last_updated": datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False, indent=4)
    print(f"[silent_collector] 已加入 {len(macs)} 个未知 MAC 到查名队列，队列总数：{len(existing)}")

# ================= 环境自检与依赖安装 =================
def install_dependencies():
    """自动化依赖自检与安装（仅首次执行）"""
    if os.path.exists(DEP_MARKER):
        return

    packages_needed = []

    # 检查系统命令
    if subprocess.run(["which", "snmpwalk"], capture_output=True).returncode != 0:
        packages_needed.extend(["snmp", "snmp-mibs-downloader"])
    if subprocess.run(["which", "sshpass"], capture_output=True).returncode != 0:
        packages_needed.append("sshpass")

    if packages_needed:
        print(f"检测到缺失系统依赖，正在尝试安装: {', '.join(packages_needed)}")
        subprocess.run(["apt-get", "update"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["apt-get", "install", "-y"] + packages_needed, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 标记已安装
        os.makedirs(WORK_DIR, exist_ok=True)
        with open(DEP_MARKER, 'w') as f:
            f.write("installed\n")

    # 检查 Python 库
    try:
        import pytz
    except ImportError:
        print("检测到缺失 pytz 库，正在安装...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pytz"], stdout=subprocess.DEVNULL)

install_dependencies()
import pytz

# 强制时区约束
TZ = pytz.timezone('Asia/Shanghai')


def get_current_time():
    """获取当前时区的时间对象和格式化字符串"""
    now = datetime.datetime.now(TZ)
    return now, now.strftime("%Y-%m-%d %H:%M:%S")


def poll_snmp_arp(now_str):
    """Task A: 通过 iKuai API 获取在线设备"""
    active_devices = {}
    try:
        # 使用 iKuai HTTP API 获取终端数据
        terminals = get_ikuai_terminal_data()
        
        for t in terminals:
            mac = t.get('mac', '').lower()
            ip = t.get('ip_addr', '')
            connect_num = t.get('connect_num', 0)
            
            # 有连接数就算在线
            if connect_num > 0 and mac and ip:
                # 严格边界隔离：仅处理 192.168.203.100 - 200 段
                if ip.startswith('192.168.203.'):
                    last_octet = int(ip.split('.')[3])
                    if 100 <= last_octet <= 200:
                        active_devices[mac] = ip

    except Exception as e:
        # 健壮性：超时或异常时静默写入错误日志
        with open(os.path.join(WORK_DIR, "error.log"), "a", encoding="utf-8") as f:
            f.write(f"[{now_str}] iKuai API 轮询失败：{str(e)}\n")

    return active_devices


def is_in_target_range(ip):
    """检查 IP 是否在 192.168.203.100-200 范围内"""
    if ip.startswith('192.168.203.'):
        try:
            last_octet = int(ip.split('.')[3])
            return 100 <= last_octet <= 200
        except ValueError:
            return False
    return False

def update_json_db(active_devices, current_time_str):
    """Task A: 基础信息 JSON 静默持久化，检测新设备并推送"""
    if os.path.exists(JSON_DB_PATH):
        with open(JSON_DB_PATH, 'r', encoding='utf-8') as f:
            try:
                device_db = json.load(f)
            except json.JSONDecodeError:
                device_db = {}
    else:
        device_db = {}

    # 过滤：仅保留 IP 在 192.168.203.100-200 范围内的设备
    filtered_db = {}
    for mac, info in device_db.items():
        current_ip = info.get("设备当前IP", "")
        if is_in_target_range(current_ip):
            filtered_db[mac] = info
    device_db = filtered_db

    # 智能检测逻辑（按天判断）
    new_devices = []  # 真正的新设备（从未出现）→ 推送
    offline_devices = []  # 已知设备今日未出现 → 仅日志，不推送
    now_obj = datetime.datetime.now(TZ)
    today_key = now_obj.strftime("%Y-%m-%d")

    # 1. 已知设备：检查是否今天未出现（仅记录日志，不推送）
    for mac in device_db:
        last_online = device_db[mac].get("最后在线时间", "")
        if last_online:
            try:
                last_time = datetime.datetime.strptime(last_online, "%Y-%m-%d %H:%M:%S")
                last_date = last_time.strftime("%Y-%m-%d")
                if last_date != today_key and mac not in active_devices:
                    # 今天未出现 → 仅记录日志，不推送
                    device_name = device_db[mac].get("设备名称", "未知")
                    offline_devices.append({
                        "mac": mac,
                        "ip": device_db[mac].get("设备当前IP", ""),
                        "device_name": device_name,
                        "status": f"今日未出现 (上次：{last_online})"
                    })
            except ValueError:
                pass

    # 2. 新设备：MAC 不在 device_info.json 中 → 随机MAC静默处理，其余查名+推送
    unknown_macs = []
    for mac in active_devices:
        if mac not in device_db:
            device_name = DEVICE_NAME_MAP.get(mac, "未知")

            # 检测是否为随机化MAC（本地管理地址，如 d2:*、76:*）
            is_random = is_randomized_mac(mac)

            if is_random:
                # 随机化MAC：跳过 iKuai API 查名（必然查不到，DHCP署名MAC不一致）
                # 不入查名队列，不推送新设备通知
                device_name = "📱 随机MAC（隐私地址）"
                print(f"[silent_collector] 🔒 跳过随机MAC处理: {mac} -> {active_devices[mac]}")
            elif device_name == "未知":
                # 非随机MAC且未知 → 立即通过 iKuai API 实时查名
                ikuai_name = lookup_ikuai_device_name(mac)
                if ikuai_name:
                    device_name = ikuai_name
                    # 更新映射和队列文件，避免重复查名
                    DEVICE_NAME_MAP[mac] = ikuai_name
                    # 持久化到 mac_name_mapping.json
                    mapping = load_mac_name_mapping()
                    mapping[mac] = ikuai_name
                    try:
                        with open(MAC_NAME_MAPPING_PATH, "w", encoding="utf-8") as f:
                            json.dump(mapping, f, ensure_ascii=False, indent=4)
                    except IOError:
                        pass
                    print(f"[silent_collector] ✅ 实时查名成功: {mac} -> {ikuai_name}")
                else:
                    # iKuai 查不到 → 尝试 OUI 厂商识别兜底
                    oui_name = lookup_oui(mac)
                    if oui_name:
                        device_name = f"{oui_name}设备"
                        print(f"[silent_collector] 🔍 OUI识别: {mac} -> {device_name}")
                        # 也缓存到映射中，避免重复查
                        DEVICE_NAME_MAP[mac] = device_name
                        mapping = load_mac_name_mapping()
                        mapping[mac] = device_name
                        try:
                            with open(MAC_NAME_MAPPING_PATH, "w", encoding="utf-8") as f:
                                json.dump(mapping, f, ensure_ascii=False, indent=4)
                        except IOError:
                            pass
                    else:
                        unknown_macs.append(mac)

            new_devices.append({
                "mac": mac,
                "ip": active_devices[mac],
                "device_name": device_name,
                "status": "从未出现",
                "is_randomized": is_random
            })

    # 非随机MAC且查不到名的加入爱快查名队列（由独立的 fetch_ikuai_names.py 每12h兜底）
    if unknown_macs:
        enqueue_unknown_macs(unknown_macs)

    # 3. 重新上线检测：设备离线1-3天后重新出现 → 推送提醒
    rejoin_devices = []
    for mac in active_devices:
        if mac in device_db:
            prev_status = device_db[mac].get("实时在线状态", "")
            if prev_status == "离线":
                last_online = device_db[mac].get("最后在线时间", "")
                if last_online:
                    try:
                        last_time = datetime.datetime.strptime(last_online, "%Y-%m-%d %H:%M:%S")
                        last_time = pytz.timezone('Asia/Shanghai').localize(last_time)
                        offline_duration = now_obj - last_time
                        offline_hours = offline_duration.total_seconds() / 3600
                        if offline_hours >= 24:
                            device_name = device_db[mac].get("设备名称", "未知")
                            rejoin_devices.append({
                                "mac": mac,
                                "ip": active_devices[mac],
                                "device_name": device_name,
                                "offline_hours": round(offline_hours, 1),
                                "last_online": last_online
                            })
                    except ValueError:
                        pass

    # 更新数据库
    for mac in device_db:
        device_db[mac]["实时在线状态"] = "离线"

    for mac, ip in active_devices.items():
        if mac not in device_db:
            device_name = format_device_name(mac, DEVICE_NAME_MAP.get(mac, "未知"))
            entry = {
                "设备名称": device_name,
                "历史IP列表": [ip],
                "设备当前IP": ip,
                "实时在线状态": "在线",
                "首次上线时间": current_time_str,
                "最后在线时间": current_time_str
            }
            # 随机化MAC标记，便于日报和后续处理识别
            if is_randomized_mac(mac):
                entry["设备名称"] = "📱 随机MAC（隐私地址）"
                entry["mac_type"] = "randomized"
            device_db[mac] = entry
        else:
            # 更新已存在设备的名称（如果映射中存在，或可用 OUI 兜底）
            if mac in DEVICE_NAME_MAP:
                device_db[mac]["设备名称"] = DEVICE_NAME_MAP[mac]
            else:
                # 现有设备名称仍是"未知"且未在映射中 → 尝试 OUI 兜底
                existing_name = device_db[mac].get("设备名称", "")
                if existing_name in ("未知", "未知设备", ""):
                    oui_name = lookup_oui(mac)
                    if oui_name:
                        new_name = f"{oui_name}设备"
                        device_db[mac]["设备名称"] = new_name
                        # 缓存到映射
                        DEVICE_NAME_MAP[mac] = new_name
                        mapping = load_mac_name_mapping()
                        mapping[mac] = new_name
                        try:
                            with open(MAC_NAME_MAPPING_PATH, "w", encoding="utf-8") as f:
                                json.dump(mapping, f, ensure_ascii=False, indent=4)
                        except IOError:
                            pass
                        print(f"[silent_collector] 🔍 OUI识别（已有设备）: {mac} -> {new_name}")
            device_db[mac]["实时在线状态"] = "在线"
            device_db[mac]["设备当前IP"] = ip
            device_db[mac]["最后在线时间"] = current_time_str
            if ip not in device_db[mac]["历史IP列表"]:
                device_db[mac]["历史IP列表"].append(ip)

    with open(JSON_DB_PATH, 'w', encoding='utf-8') as f:
        json.dump(device_db, f, ensure_ascii=False, indent=4)

    # 如果有新设备，触发推送（避免重复推送）
    if new_devices:
        # 加载已推送记录
        push_history = load_pushed_devices()
        today_key = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
        
        # 清理过期记录（只保留今天的）
        push_history = {k: v for k, v in push_history.items() if k == today_key}
        
        # 检查是否有未推送的新设备（排除随机MAC，静默处理）
        unpushed_devices = []
        for device in new_devices:
            if device.get("is_randomized"):
                continue  # 随机MAC：静默处理，不推送也不记录推送历史
            mac = device["mac"]
            if mac not in push_history.get(today_key, []):
                unpushed_devices.append(device)
        
        if unpushed_devices:
            # 更新推送记录
            if today_key not in push_history:
                push_history[today_key] = []
            for device in unpushed_devices:
                push_history[today_key].append(device["mac"])
            save_pushed_devices(push_history)
            
            # 构建推送消息
            message = "🔔 **新设备检测提醒**\n\n"
            message += f"检测到 **{len(unpushed_devices)}** 个新设备：\n\n"
            
            identified_count = 0
            unknown_count = 0
            for i, device in enumerate(unpushed_devices, 1):
                name = device['device_name']
                if name == "未知":
                    unknown_count += 1
                else:
                    identified_count += 1
                message += f"{i}. 🆕 **{name}**\n"
                message += f"   MAC: `{device['mac']}`\n"
                message += f"   IP: `{device['ip']}`\n"
                message += f"   状态：{device['status']}\n\n"
            
            if identified_count > 0 and unknown_count == 0:
                message += "💡 设备名称已通过爱快后台自动识别。"
            elif identified_count > 0 and unknown_count > 0:
                message += f"💡 其中 {identified_count} 个设备名称通过爱快后台识别，{unknown_count} 个设备无法识别（可能未使用DHCP）。"
            else:
                message += "💡 设备名称无法通过爱快后台识别（设备可能未使用DHCP或为静态IP）。已加入查名队列等待后续处理。"
            
            send_telegram_message(message)

    # 重新上线提醒：离线1-3天的设备重新上线 → Telegram 推送（去重，每天每个MAC只提醒一次）
    if rejoin_devices:
        rejoin_file = os.path.join(WORK_DIR, "rejoin_notified.json")
        rejoin_notified = {}
        if os.path.exists(rejoin_file):
            try:
                with open(rejoin_file, 'r', encoding='utf-8') as f:
                    rejoin_notified = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

        today_key = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
        unpushed_rejoin = []
        for d in rejoin_devices:
            mac = d["mac"]
            if rejoin_notified.get(mac) != today_key:
                unpushed_rejoin.append(d)
                rejoin_notified[mac] = today_key

        if unpushed_rejoin:
            with open(rejoin_file, 'w', encoding='utf-8') as f:
                json.dump(rejoin_notified, f, ensure_ascii=False, indent=4)

            message = "🔄 **设备重新上线提醒**\n\n"
            message += f"有 **{len(unpushed_rejoin)}** 个离线超过1天的设备重新上线：\n\n"
            for i, d in enumerate(unpushed_rejoin, 1):
                days = int(d["offline_hours"] // 24)
                hours = int(d["offline_hours"] % 24)
                message += f"{i}. **{d['device_name']}**\n"
                message += f"   MAC: `{d['mac']}`\n"
                message += f"   IP: `{d['ip']}`\n"
                message += f"   离线时长：{days}天{hours}小时\n"
                message += f"   最后在线：{d['last_online']}\n\n"

            message += "💡 该设备已离线1-3天，现已恢复连接。"
            send_telegram_message(message)

    # 离线设备仅记录日志，不推送
    if offline_devices:
        for device in offline_devices:
            print(f"[silent_collector] 离线设备（未推送）：{device['device_name']} MAC={device['mac']} 上次在线={device['status'].split('(')[1].rstrip(')')}")

    return device_db


def mac_to_name(mac):
    """将 MAC 地址转换为设备名称"""
    mac_lower = mac.lower()
    return DEVICE_NAME_MAP.get(mac_lower, mac)

def is_randomized_mac(mac: str) -> bool:
    """检测是否是随机化 MAC（本地管理地址）
    
    随机化 MAC 特征：MAC 第一个字节的第二位 (bit 1, 从0开始) 为1。
    常见前缀：d2:*、76:*、c6:*、7a:*、f2:*、82:*、56:*、92:* 等。
    此类 MAC 在 iKuai 上永远查不到名（DHCP 使用的是真实 MAC），
    无需浪费 API 调用和队列重试。
    """
    if not mac or ':' not in mac:
        return False
    try:
        first_byte = int(mac.split(':')[0], 16)
        return bool(first_byte & 0b10)  # bit 1 = 1 → locally administered
    except (ValueError, IndexError):
        return False

def ensure_utf8_bom(file_path):
    """确保文件包含 UTF-8 BOM 标记，确保 Excel 正确识别中文"""
    if os.path.exists(file_path):
        with open(file_path, 'rb') as f:
            header = f.read(3)
            if header != b'\xef\xbb\xbf':
                # 文件不存在 BOM，读取现有内容并重新写入带 BOM 的文件
                with open(file_path, 'r', encoding='utf-8') as rf:
                    content = rf.read()
                with open(file_path, 'wb') as wf:
                    wf.write(b'\xef\xbb\xbf')
                    wf.write(content.encode('utf-8'))
    else:
        # 文件不存在，创建时写入 BOM
        with open(file_path, 'wb') as f:
            f.write(b'\xef\xbb\xbf')

def update_csv_matrix(device_db, active_devices, now_obj):
    """Task A: 二维时间序列状态矩阵 (多级目录切片)"""
    year_str = now_obj.strftime("%Y") + "年"
    month_str = str(int(now_obj.strftime("%m"))) + "月"
    day_str = now_obj.strftime("%d") + "日"
    hour_str = now_obj.strftime("%H")
    minute_str = now_obj.strftime("%H:%M")

    # 建立多级嵌套目录树 YYYY年/MM月/DD日/
    day_dir = os.path.join(CSV_DIR_BASE, year_str, month_str, day_str)
    os.makedirs(day_dir, exist_ok=True)

    csv_path = os.path.join(day_dir, f"{hour_str}时.csv")

    # 将 MAC 地址转换为设备名称作为列头（排除随机化MAC，不记录CSV矩阵）
    all_known_macs = [mac for mac in device_db 
                      if device_db[mac].get("mac_type") != "randomized"]
    # 使用设备名称作为列头，如果映射中存在则使用名称，否则使用 MAC
    headers = ["Time"] + [mac_to_name(mac) for mac in all_known_macs]
    # 同时保留 MAC 到名称的映射，用于写入数据时查找
    mac_to_col = {mac: mac_to_name(mac) for mac in all_known_macs}

    file_exists = os.path.exists(csv_path)
    rewrite_needed = False
    existing_rows = []

    if file_exists:
        with open(csv_path, 'r', encoding='utf-8-sig') as f:  # 使用 utf-8-sig 自动处理 BOM
            reader = csv.reader(f)
            try:
                old_headers = next(reader)
                if old_headers != headers:
                    rewrite_needed = True
                    for row in reader:
                        row_dict = dict(zip(old_headers, row))
                        existing_rows.append(row_dict)
            except StopIteration:
                pass

    current_row = {"Time": minute_str}
    for mac in all_known_macs:
        col_name = mac_to_col[mac]
        current_row[col_name] = "1" if mac in active_devices else "0"

    # 执行写入（使用 utf-8-sig 自动处理 BOM）
    if not file_exists or rewrite_needed:
        # 重写文件：先写 BOM，再写所有行
        with open(csv_path, 'wb') as f:
            f.write(b'\xef\xbb\xbf')  # 写入 BOM
        # 以追加模式打开，写入 CSV 内容
        with open(csv_path, 'a', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            for old_row in existing_rows:
                # 只保留与新 headers 匹配的列
                filtered_row = {h: old_row.get(h, "0") for h in headers}
                writer.writerow(filtered_row)
            writer.writerow(current_row)
    else:
        # 直接追加，使用 utf-8-sig 确保 BOM 存在
        with open(csv_path, 'a', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writerow(current_row)


def poll_pve_link_status(now_str):
    """Task B: x86 PVE 链路状态巡检"""
    pve_host = env_get("PVE_HOST", "192.168.203.25")
    pve_user = env_get("PVE_USER", "root")
    pve_pass = env_get("PVE_PASS")
    cmd = [
        "sshpass", "-p", pve_pass,
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
        f"{pve_user}@{pve_host}",
        "ethtool nic0 2>&1; echo '===SPLIT==='; ethtool nic1 2>&1"
    ]

    status_msg = ""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout:
            parts = result.stdout.split("===SPLIT===")
            nic0_output = parts[0].strip() if len(parts) > 0 else ""
            nic1_output = parts[1].strip() if len(parts) > 1 else ""

            nic0_info = _parse_ethtool(nic0_output)
            nic1_info = _parse_ethtool(nic1_output)

            nic0_status = _nic_status_display(nic0_info)
            nic1_status = _nic_status_display(nic1_info)

            if nic0_info.get("link") and nic1_info.get("link"):
                status_msg = f"正常 (nic0: {nic0_status}, nic1: {nic1_status})"
            elif nic0_info.get("link") and not nic1_info.get("link"):
                status_msg = f"⊘ nic0 正常 ({nic0_status}), nic1 未连接"
            elif not nic0_info.get("link") and nic1_info.get("link"):
                status_msg = f"⊘ nic0 未连接, nic1 正常 ({nic1_status})"
            else:
                status_msg = f"⊘ nic0 未连接, nic1 未连接"
        else:
            status_msg = "采集链路异常 (SSH 命令执行失败或无返回)"
    except (subprocess.TimeoutExpired, Exception):
        status_msg = "采集链路异常 (SSH 连接超时或受阻)"

    # 静默追加写入本地日志
    with open(PVE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{now_str}] PVE 链路状态：{status_msg}\n")


def _parse_ethtool(output: str) -> dict:
    """解析 ethtool 输出，提取 link 状态和速度"""
    info = {"link": False, "speed": "Unknown", "duplex": "Unknown"}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Speed:"):
            info["speed"] = line.split(":", 1)[1].strip()
        elif line.startswith("Duplex:"):
            info["duplex"] = line.split(":", 1)[1].strip()
        elif line.startswith("Link detected:"):
            info["link"] = "yes" in line.lower()
    return info


def _nic_status_display(info: dict) -> str:
    """格式化单个网卡的显示信息"""
    if not info.get("link"):
        return "⊘ 未连接"
    speed = info.get("speed", "Unknown")
    duplex = info.get("duplex", "Unknown")
    return f"{speed}, {duplex}"


def main():
    """入口：Task A (每分钟) + Task B (每 30 分钟)"""
    os.makedirs(WORK_DIR, exist_ok=True)
    now_obj, current_time_str = get_current_time()

    # 执行 Task A: 高频采集 (每分钟)
    active_devices = poll_snmp_arp(current_time_str)
    device_db = update_json_db(active_devices, current_time_str)
    update_csv_matrix(device_db, active_devices, now_obj)

    # 执行 Task B: 低频巡检 (仅在分钟数为 0 或 30 时执行)
    if now_obj.minute % 30 == 0:
        poll_pve_link_status(current_time_str)


if __name__ == "__main__":
    main()
