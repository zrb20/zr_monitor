#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性脚本：将 ZR Monitor 现有数据导入 MySQL
不影响原有 CSV/JSON 文件系统
用法：python3 ~/zr_monitor/import_to_mysql.py
"""

import os
import json
import csv
import datetime
import re
import subprocess
from collections import defaultdict

WORK_DIR = os.path.expanduser("~/zr_monitor")
CSV_DIR_BASE = os.path.join(WORK_DIR, "matrix_data")
MYSQL_CMD = "mariadb"
MYSQL_DB = "zr_monitor"

def mysql_exec_many(sql_template, values_list, batch_size=500):
    """批量插入"""
    total = len(values_list)
    for i in range(0, total, batch_size):
        batch = values_list[i:i+batch_size]
        values_sql = ", ".join(batch)
        sql = sql_template % values_sql
        r = subprocess.run([MYSQL_CMD, "-e", sql],
                          capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            err = r.stderr.strip()
            if err and "Duplicate" not in err:
                print(f"[WARN] batch {i}: {err[:200]}")
    print(f"  写入 {total} 行")

def mysql_quote(val):
    if val is None:
        return "NULL"
    return "'" + str(val).replace("'", "\\'") + "'"

# ──────────────────────────────────────────────
# 1. MAC 名称映射
# ──────────────────────────────────────────────
def import_mac_mapping():
    path = os.path.join(WORK_DIR, "mac_name_mapping.json")
    if not os.path.exists(path):
        print("[MAC映射] 无文件，跳过")
        return {}
    
    with open(path, 'r', encoding='utf-8') as f:
        mapping = json.load(f)
    
    values = [f"({mysql_quote(mac)}, {mysql_quote(name)})"
              for mac, name in mapping.items()]
    
    if values:
        sql = "INSERT INTO %s.mac_name_mapping (mac, name) VALUES %%s ON DUPLICATE KEY UPDATE name=VALUES(name)" % MYSQL_DB
        mysql_exec_many(sql, values)
        print(f"[MAC映射] 导入 {len(values)} 条")
    
    return mapping  # 返回供后续使用

# ──────────────────────────────────────────────
# 2. 设备表
# ──────────────────────────────────────────────
def import_devices(mac_mapping):
    path = os.path.join(WORK_DIR, "device_info.json")
    if not os.path.exists(path):
        print("[设备表] 无文件，跳过")
        return 0
    
    with open(path, 'r', encoding='utf-8') as f:
        device_db = json.load(f)
    
    def is_random(mac):
        if not mac or ':' not in mac:
            return False
        try:
            return bool(int(mac.split(':')[0], 16) & 0b10)
        except ValueError:
            return False
    
    values = []
    for mac, info in device_db.items():
        name = mysql_quote(info.get("设备名称", ""))
        ip = mysql_quote(info.get("设备当前IP", ""))
        first = mysql_quote(info.get("首次上线时间", ""))
        last = mysql_quote(info.get("最后在线时间", ""))
        online = "1" if info.get("实时在线状态") == "在线" else "0"
        rand = "1" if (is_random(mac) or info.get("mac_type") == "randomized") else "0"
        mtype = mysql_quote(info.get("mac_type", ""))
        
        values.append(
            f"({mysql_quote(mac)}, {name}, {ip}, {first}, {last}, "
            f"{online}, {rand}, {mtype})"
        )
    
    if values:
        sql = ("INSERT INTO %s.devices "
               "(mac, name, ip, first_seen, last_seen, is_online, is_randomized, mac_type) "
               "VALUES %%s "
               "ON DUPLICATE KEY UPDATE name=VALUES(name), ip=VALUES(ip), "
               "last_seen=VALUES(last_seen), is_online=VALUES(is_online)") % MYSQL_DB
        mysql_exec_many(sql, values)
        print(f"[设备表] 导入 {len(values)} 条")

# ──────────────────────────────────────────────
# 3. PVE 链路状态
# ──────────────────────────────────────────────
def import_pve_status():
    path = os.path.join(WORK_DIR, "pve_link_status.log")
    if not os.path.exists(path):
        print("[PVE链路] 无文件，跳过")
        return
    
    pat = re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*PVE 链路状态：(.+)')
    values = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            m = pat.search(line)
            if m:
                values.append(f"({mysql_quote(m.group(1))}, {mysql_quote(m.group(2))})")
    
    if values:
        sql = "INSERT INTO %s.pve_link_status (check_time, status_text) VALUES %%s" % MYSQL_DB
        mysql_exec_many(sql, values)
        print(f"[PVE链路] 导入 {len(values)} 条")

# ──────────────────────────────────────────────
# 4. 在线记录（从 CSV 矩阵导入）
# ──────────────────────────────────────────────
def import_online_records(mac_mapping):
    if not os.path.exists(CSV_DIR_BASE):
        print("[在线记录] 无 matrix_data 目录，跳过")
        return
    
    # 构建反向映射：设备名 → MAC（多个 MAC 可能同名，选一个）
    name_to_mac = {}
    for mac, name in mac_mapping.items():
        if name not in name_to_mac:
            name_to_mac[name] = mac
    
    total = 0
    csv_files = []
    for root, dirs, files in os.walk(CSV_DIR_BASE):
        for fname in sorted(files):
            if fname.endswith('.csv'):
                csv_files.append(os.path.join(root, fname))
    
    print(f"[在线记录] 找到 {len(csv_files)} 个 CSV 文件")
    
    batch = []
    for fpath in csv_files:
        rel = os.path.relpath(fpath, CSV_DIR_BASE).replace('\\', '/').split('/')
        if len(rel) < 4:
            continue
        
        try:
            year = rel[0].replace('年', '')
            month = rel[1].replace('月', '').zfill(2)
            day = rel[2].replace('日', '').zfill(2)
            hour = rel[3].replace('时.csv', '').replace('.csv', '').zfill(2)
        except:
            continue
        
        with open(fpath, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            cols = [h for h in reader.fieldnames if h != 'Time']
            
            for row in reader:
                time_str = row.get('Time', '')
                if ':' not in time_str:
                    continue
                minute = time_str.split(':')[1].zfill(2)
                record_dt = f"{year}-{month}-{day} {hour}:{minute}:00"
                
                for dev_name in cols:
                    val = row.get(dev_name, '0').strip()
                    if val != '1':
                        continue
                    
                    # 列头可能是 MAC 地址，也可能是设备名
                    if ':' in dev_name:
                        mac = dev_name
                    else:
                        mac = name_to_mac.get(dev_name, dev_name)
                    
                    batch.append(f"({mysql_quote(mac)}, {mysql_quote(record_dt)}, 1)")
                    total += 1
                    
                    if len(batch) >= 2000:
                        sql = "INSERT IGNORE INTO %s.online_records (mac, record_time, is_online) VALUES %%s" % MYSQL_DB
                        mysql_exec_many(sql, batch)
                        batch = []
        
        print(f"  ✓ {rel[0]}/{rel[1]}/{rel[2]} {rel[3]} — 累计 {total} 行")
    
    if batch:
        sql = "INSERT IGNORE INTO %s.online_records (mac, record_time, is_online) VALUES %%s" % MYSQL_DB
        mysql_exec_many(sql, batch)
    
    print(f"[在线记录] 共导入 {total} 条")

# ════════════════════════════════════════════
def main():
    print("=" * 50)
    print("   ZR Monitor → MySQL 数据导入")
    print("   不影响原有 CSV / JSON 文件系统")
    print("=" * 50)
    
    # 1. MAC 映射（最基础，被其他表引用）
    mac_mapping = import_mac_mapping()
    
    # 2. 设备表
    import_devices(mac_mapping)
    
    # 3. PVS 链路
    import_pve_status()
    
    # 4. 在线记录（数据量最大，最后执行）
    import_online_records(mac_mapping)
    
    print("\n" + "=" * 50)
    print("✅ 导入完成！试试这些 SQL 查询：")
    print("=" * 50)
    print()
    print("  mariadb -e 'USE zr_monitor; SELECT COUNT(*) AS 设备总数 FROM devices;'")
    print("  mariadb -e 'USE zr_monitor; SELECT name, ip FROM devices WHERE is_online=1;'")
    print("  mariadb -e \"USE zr_monitor; SELECT COUNT(*) AS 历史记录条数 FROM online_records;\"")
    print("  mariadb -e \"USE zr_monitor;")
    print("    SELECT d.name, COUNT(*) AS 在线分钟数")
    print("    FROM devices d")
    print("    JOIN online_records r ON d.mac = r.mac")
    print("    WHERE r.record_time LIKE '2026-06-15%'")
    print("    GROUP BY d.name")
    print("    ORDER BY 在线分钟数 DESC;\"")
    print()
    print("进入交互模式：")
    print("  mariadb -D zr_monitor")

if __name__ == "__main__":
    main()
