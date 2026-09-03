#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成前一天设备在线日报并推送到 Telegram
"""

import os
import sys
import json
import csv
import datetime
import pytz
from collections import defaultdict

# ================= 配置 =================
WORK_DIR = os.path.expanduser("~/zr_monitor")
CSV_DIR_BASE = os.path.join(WORK_DIR, "matrix_data")
MAC_NAME_MAPPING_PATH = os.path.join(WORK_DIR, "mac_name_mapping.json")
DEVICE_INFO_PATH = os.path.join(WORK_DIR, "device_info.json")

# ================= 飞书推送 =================
from feishu_push import send_feishu_message

def send_telegram_message(message: str) -> bool:
    """发送飞书消息（替代原 Telegram 推送）"""
    return send_feishu_message(message)

# ================= 工具函数 =================
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

# ================= 日报生成 =================
def generate_daily_report(year, month, day):
    """
    生成指定日期的设备在线日报
    
    参数:
        year, month, day: 日期整数
    返回:
        报告文本
    """
    month_str = str(month) + "月"
    day_str = f"{day:02d}日"
    date_str = f"{year:04d}年/{month_str}/{day_str}"
    csv_dir = os.path.join(CSV_DIR_BASE, date_str)
    
    if not os.path.exists(csv_dir):
        return None, f"日期 {date_str} 无数据"
    
    # 收集所有小时的 CSV 文件
    hour_files = []
    for fname in os.listdir(csv_dir):
        if fname.endswith('.csv'):
            hour_files.append(os.path.join(csv_dir, fname))
    
    if not hour_files:
        return None, f"日期 {date_str} 无 CSV 文件"
    
    # 加载 MAC 地址到名称的映射
    mac_mapping = load_mac_name_mapping()
    
    # 读取所有小时的数据
    all_data = {}  # {设备名: [在线分钟数]}
    total_hours = 0
    
    for fpath in sorted(hour_files):
        try:
            with open(fpath, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                headers = reader.fieldnames
                
                # 跳过第一列（Time），只处理设备列
                device_cols = [h for h in headers if h != 'Time']
                
                for row in reader:
                    for device in device_cols:
                        if device not in all_data:
                            all_data[device] = 0
                        if row.get(device, '0').strip() == '1':
                            all_data[device] += 1
        except Exception as e:
            print(f"读取 {fpath} 失败: {e}")
            continue
    
    # 将 MAC 地址替换为友好名称（相同名称的合并在线时长）
    remapped = {}
    for device, minutes in all_data.items():
        name = mac_mapping.get(device, device)
        remapped[name] = remapped.get(name, 0) + minutes
    all_data = remapped
    
    if not all_data:
        return None, f"日期 {date_str} 无设备数据"
    
    # 统计随机化MAC设备数量（从 device_info.json 中读取）
    random_mac_count = 0
    if os.path.exists(DEVICE_INFO_PATH):
        try:
            with open(DEVICE_INFO_PATH, 'r', encoding='utf-8') as f:
                device_db = json.load(f)
            for mac, info in device_db.items():
                if info.get("mac_type") == "randomized":
                    last_online = info.get("最后在线时间", "")
                    if last_online.startswith(f"{year:04d}-{month:02d}-{day:02d}"):
                        random_mac_count += 1
        except (json.JSONDecodeError, IOError):
            pass

    # 计算在线时长（分钟 -> 小时）
    device_online = []
    for device, minutes in all_data.items():
        hours = minutes / 60.0
        device_online.append((device, hours, minutes))
    
    # 过滤掉离线设备，按在线时长排序（降序）
    device_online = [(d, h, m) for d, h, m in device_online if m > 0]
    device_online.sort(key=lambda x: x[1], reverse=True)
    
    # 生成报告文本
    report_lines = []
    report_lines.append(f"📊 **{year}年{month}月{day}日 设备在线报告**\n")
    report_lines.append(f"在线设备数：{len(device_online)} 台\n")
    report_lines.append(f"数据时间范围：00:00 - 23:59\n")
    report_lines.append(f"总记录文件：{len(hour_files)} 个（每小时一个 CSV）\n")
    report_lines.append("━" * 60 + "\n")
    report_lines.append("**设备在线情况：**\n")
    
    rank = 0
    for device, hours, minutes in device_online:
        rank += 1
        report_lines.append(f"{rank}. {device}")
        report_lines.append(f"   在线时长：{hours:.1f} 小时（{minutes} 分钟）\n")
    
    report_lines.append("━" * 60)
    if random_mac_count > 0:
        report_lines.append(f"\n📱 **随机MAC设备（隐私地址）**：{random_mac_count} 台")
        report_lines.append("（手机/设备 MAC 随机化产生的临时地址，不计入上方设备列表）")
    report_lines.append("")
    
    report = "\n".join(report_lines)
    return report, None

# ================= 主函数 =================
def main():
    """入口"""
    TZ = pytz.timezone('Asia/Shanghai')
    now = datetime.datetime.now(TZ)
    
    # 计算前一天的日期
    yesterday = now - datetime.timedelta(days=1)
    year, month, day = yesterday.year, yesterday.month, yesterday.day
    
    print(f"生成 {year}-{month:02d}-{day:02d} 的设备在线日报")
    
    # 生成报告
    report, error = generate_daily_report(year, month, day)
    
    if error:
        print(f"错误: {error}")
        sys.exit(1)
    
    # 输出报告（由 cron 系统捕获并投递）
    print(report)

if __name__ == "__main__":
    main()
