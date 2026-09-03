#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外挂同步脚本：将 ZR Monitor 的 CSV/JSON 数据同步到 MySQL
不影响原有文件系统，独立运行
用法：python3 ~/zr_monitor/sync_to_mysql.py
推荐：放在 crontab 每 1-5 分钟执行一次
"""

import os
import json
import csv
import re
import subprocess
import datetime
import hashlib

WORK_DIR = os.path.expanduser("~/zr_monitor")
CSV_DIR_BASE = os.path.join(WORK_DIR, "matrix_data")
MYSQL_DB = "zr_monitor"
STATE_FILE = os.path.join(WORK_DIR, ".sync_state.json")

def mysql_exec(sql):
    r = subprocess.run(["mariadb", "-e", sql],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        err = r.stderr.strip()
        if err and "Duplicate" not in err and "already exists" not in err:
            print(f"[WARN] {err[:200]}")
        return False
    return True

def mysql_exec_many(sql_template, values_list, batch_size=1000):
    for i in range(0, len(values_list), batch_size):
        batch = values_list[i:i+batch_size]
        sql = sql_template % ", ".join(batch)
        r = subprocess.run(["mariadb", "-e", sql],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            err = r.stderr.strip()
            if err and "Duplicate" not in err:
                print(f"[WARN] batch {i}: {err[:200]}")

def q(v):
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "\\'") + "'"

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {"csv_processed": {}, "json_mtime": 0, "pve_position": 0}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def sync_devices():
    """同步 device_info.json → devices 表"""
    path = os.path.join(WORK_DIR, "device_info.json")
    if not os.path.exists(path):
        return
    
    state = load_state()
    mtime = os.path.getmtime(path)
    if mtime <= state.get("json_mtime", 0):
        return  # 没变化
    
    with open(path, 'r', encoding='utf-8') as f:
        device_db = json.load(f)
    
    def is_random(mac):
        if not mac or ':' not in mac:
            return False
        try:
            return bool(int(mac.split(':')[0], 16) & 0b10)
        except:
            return False
    
    values = []
    for mac, info in device_db.items():
        values.append(
            f"({q(mac)}, {q(info.get('设备名称',''))}, {q(info.get('设备当前IP',''))}, "
            f"{q(info.get('首次上线时间',''))}, {q(info.get('最后在线时间',''))}, "
            f"{'1' if info.get('实时在线状态')=='在线' else '0'}, "
            f"{'1' if is_random(mac) else '0'}, {q(info.get('mac_type',''))})"
        )
    
    if values:
        sql = ("INSERT INTO %s.devices (mac,name,ip,first_seen,last_seen,is_online,is_randomized,mac_type) "
               "VALUES %%s ON DUPLICATE KEY UPDATE name=VALUES(name), ip=VALUES(ip), "
               "last_seen=VALUES(last_seen), is_online=VALUES(is_online)") % MYSQL_DB
        mysql_exec_many(sql, values)
        print(f"  [设备表] 同步 {len(values)} 台设备")
    
    state["json_mtime"] = mtime
    save_state(state)

def sync_mac_mapping():
    """同步 mac_name_mapping"""
    path = os.path.join(WORK_DIR, "mac_name_mapping.json")
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            mapping = json.load(f)
        values = [f"({q(mac)}, {q(name)})" for mac, name in mapping.items()]
        if values:
            sql = "INSERT INTO %s.mac_name_mapping (mac,name) VALUES %%s ON DUPLICATE KEY UPDATE name=VALUES(name)" % MYSQL_DB
            mysql_exec_many(sql, values)

def sync_pve_status():
    """同步 pve_link_status.log 新增行"""
    path = os.path.join(WORK_DIR, "pve_link_status.log")
    if not os.path.exists(path):
        return
    
    state = load_state()
    pos = state.get("pve_position", 0)
    
    with open(path, 'r', encoding='utf-8') as f:
        f.seek(pos)
        new_lines = f.readlines()
        new_pos = f.tell()
    
    if not new_lines:
        return
    
    pat = re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*PVE 链路状态：(.+)')
    values = []
    for line in new_lines:
        m = pat.search(line)
        if m:
            values.append(f"({q(m.group(1))}, {q(m.group(2))})")
    
    if values:
        sql = "INSERT INTO %s.pve_link_status (check_time, status_text) VALUES %%s" % MYSQL_DB
        mysql_exec_many(sql, values)
    
    state["pve_position"] = new_pos
    save_state(state)
    print(f"  [PVE链路] 同步 {len(values)} 条")

def sync_csv():
    """同步新增的 CSV 数据到 online_records"""
    if not os.path.exists(CSV_DIR_BASE):
        return
    
    state = load_state()
    csv_processed = state.get("csv_processed", {})
    batch = []
    
    # 加载 MAC 名称映射（列头→MAC 反查）
    mac_mapping = {}
    mpath = os.path.join(WORK_DIR, "mac_name_mapping.json")
    if os.path.exists(mpath):
        with open(mpath, 'r', encoding='utf-8') as f:
            mac_mapping = json.load(f)
    name_to_mac = {v: k for k, v in mac_mapping.items()}
    
    for root, dirs, files in os.walk(CSV_DIR_BASE):
        for fname in sorted(files):
            if not fname.endswith('.csv'):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, CSV_DIR_BASE).replace('\\', '/').split('/')
            if len(rel) < 4:
                continue
            
            # 已处理过的文件，跳过
            file_key = "/".join(rel)
            last_size = csv_processed.get(file_key, 0)
            current_size = os.path.getsize(fpath)
            
            if current_size <= last_size:
                continue
            
            try:
                year = rel[0].replace('年', '')
                month = rel[1].replace('月', '').zfill(2)
                day = rel[2].replace('日', '').zfill(2)
                hour = rel[3].replace('时.csv', '').replace('.csv', '').zfill(2)
            except:
                continue
            
            with open(fpath, 'r', encoding='utf-8-sig') as f:
                # 跳过已处理的行数：估算行数 from 文件大小
                reader = csv.DictReader(f)
                cols = [h for h in reader.fieldnames if h != 'Time']
                
                new_rows = []
                for row in reader:
                    t = row.get('Time', '')
                    if ':' not in t:
                        continue
                    minute = t.split(':')[1].zfill(2)
                    record_dt = f"{year}-{month}-{day} {hour}:{minute}:00"
                    
                    for dev_name in cols:
                        if row.get(dev_name, '0').strip() == '1':
                            mac = dev_name if ':' in dev_name else name_to_mac.get(dev_name, dev_name)
                            new_rows.append(f"({q(mac)}, {q(record_dt)}, 1)")
                
                if new_rows:
                    # 用 IGNORE 避免重复
                    batch.extend(new_rows)
                    if len(batch) >= 1000:
                        sql = "INSERT IGNORE INTO %s.online_records (mac, record_time, is_online) VALUES %%s" % MYSQL_DB
                        mysql_exec_many(sql, batch)
                        batch = []
            
            csv_processed[file_key] = current_size
    
    if batch:
        sql = "INSERT IGNORE INTO %s.online_records (mac, record_time, is_online) VALUES %%s" % MYSQL_DB
        mysql_exec_many(sql, batch)
    
    state["csv_processed"] = csv_processed
    save_state(state)

def main():
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] ZR Monitor → MySQL 同步")
    sync_mac_mapping()
    sync_devices()
    sync_csv()
    sync_pve_status()
    print(f"  ✅ 完成")

if __name__ == "__main__":
    main()
