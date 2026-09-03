#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZR Monitor 统一同步/备份脚本
支持模式: sync(仅同步CSV), backup(仅备份DB), both(同步+备份)
"""

import os
import sys
import datetime
import json
import pytz
import shutil
from smbclient import register_session, mkdir, open_file

# ================= 配置 =================
# 从 .env 读取（模板见 .env.example）
from env_config import get as env_get
NAS_IP = env_get("NAS_IP", "192.168.203.21")
NAS_SHARE = env_get("NAS_SHARE", "设备在线日志")
NAS_USER = env_get("NAS_USER", "zrb20")
NAS_PASSWORD = env_get("NAS_PASSWORD")

WORK_DIR = os.path.expanduser("~/zr_monitor")
MATRIX_DIR = os.path.join(WORK_DIR, "matrix_data")
JSON_DB_PATH = os.path.join(WORK_DIR, "device_info.json")
LOG_FILE = os.path.join(WORK_DIR, "zr_sync_backup.log")
SYNC_STATE_FILE = os.path.join(WORK_DIR, "sync_state.json")
BACKUP_STATE_FILE = os.path.join(WORK_DIR, "backup_state.json")

TZ = pytz.timezone('Asia/Shanghai')


# ================= 工具函数 =================
def log(msg):
    """日志记录"""
    ts = datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    print(line.strip())
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line)


def _init_smb():
    """初始化 SMB 会话"""
    register_session(NAS_IP, username=NAS_USER, password=NAS_PASSWORD)


def _smb_path(remote_path):
    """构造 SMB 远程路径"""
    sep = "\\"
    return f"\\\\{NAS_IP}\\{NAS_SHARE}\\{remote_path.replace('/', sep)}"


def smbclient_upload(local_path, remote_path, retries=3):
    """使用 smbprotocol 上传单个文件到 NAS"""
    _init_smb()
    for attempt in range(retries):
        try:
            with open(local_path, 'rb') as lf, open_file(_smb_path(remote_path), mode='wb') as rf:
                shutil.copyfileobj(lf, rf)
            return True, ""
        except Exception as e:
            if attempt < retries - 1:
                import time
                time.sleep(1)
                continue
            return False, str(e)
    return False, f"重试 {retries} 次后仍失败"


def ensure_smb_directory(path, retries=3):
    """确保 SMB 远程目录存在（逐级创建）"""
    _init_smb()
    dir_path = os.path.dirname(path)
    if not dir_path:
        return True

    parts = dir_path.split('/')
    current_path = ""

    for part in parts:
        if not current_path:
            current_path = part
        else:
            current_path = f"{current_path}/{part}"

        for attempt in range(retries):
            try:
                mkdir(_smb_path(current_path))
                break
            except Exception as e:
                if "already exists" in str(e).lower() or "exists" in str(e).lower():
                    break
                if attempt < retries - 1:
                    import time
                    time.sleep(1)
                    continue
                return False
        else:
            return False

    return True


# ================= 同步功能 =================
def scan_csv_files():
    """递归扫描 matrix_data 目录，返回所有 .csv 文件列表"""
    csv_files = []
    for root, dirs, files in os.walk(MATRIX_DIR):
        for fname in files:
            if fname.endswith('.csv'):
                csv_files.append(os.path.join(root, fname))
    return sorted(csv_files)


def get_remote_path(local_path):
    """计算远程路径：直接使用相对于 matrix_data 的子路径（原样保留中文字段）"""
    basename = os.path.basename(local_path)
    parts = local_path.split('/')
    try:
        matrix_idx = parts.index('matrix_data')
        relative_parts = parts[matrix_idx+1:]
        return '/'.join(relative_parts)
    except (ValueError, IndexError):
        return basename


def sync_files(files_to_sync=None):
    """增量同步新文件到 NAS（跳过已同步过的）"""
    if files_to_sync is None:
        files_to_sync = scan_csv_files()

    if not files_to_sync:
        log("未找到需要同步的 CSV 文件")
        return 0, 0

    # 加载已同步记录
    synced = load_state(SYNC_STATE_FILE)
    log(f"已同步记录: {len(synced)} 个文件")

    # 筛选出新文件
    new_files = []
    for local_path in files_to_sync:
        remote_path = get_remote_path(local_path)
        if remote_path not in synced:
            new_files.append(local_path)

    if not new_files:
        log("没有新文件需要同步 ✓")
        return 0, 0

    log(f"发现 {len(new_files)} 个新文件，开始增量同步")
    success_count = 0
    fail_count = 0

    for local_path in new_files:
        remote_path = get_remote_path(local_path)
        ensure_smb_directory(remote_path)
        success, error = smbclient_upload(local_path, remote_path)

        if success:
            success_count += 1
            synced.add(remote_path)
            log(f"+ {os.path.basename(local_path)}")
        else:
            fail_count += 1
            log(f"✗ {os.path.basename(local_path)}: {error}")

    # 保存增量状态
    save_state(SYNC_STATE_FILE, synced)
    log(f"同步完成: 新增 {success_count}, 失败 {fail_count} (累计已同步 {len(synced)} 个)")
    return success_count, fail_count


# ================= 备份功能 =================
def backup_device_db():
    """增量备份 device_info.json 到 NAS（仅内容变化时备份，只保留最新）"""
    if not os.path.exists(JSON_DB_PATH):
        log("device_info.json 不存在，跳过备份")
        return False

    current_hash = get_file_hash(JSON_DB_PATH)
    prev_hash = load_state(BACKUP_STATE_FILE)
    prev_hash_str = next(iter(prev_hash)) if prev_hash else ""

    if prev_hash_str == current_hash:
        log("device_info.json 无变化，跳过备份 ✓")
        return True

    remote_filename = "device_info_latest.json"

    log(f"检测到变化，更新备份 -> NAS/{remote_filename}")

    success, error = smbclient_upload(JSON_DB_PATH, remote_filename)

    if success:
        log(f"✓ 备份已更新: {remote_filename}")
        save_state(BACKUP_STATE_FILE, {current_hash})
        return True
    else:
        log(f"✗ 备份失败: {error}")
        return False


# ================= 入口 =================
# ================= 增量状态管理 =================

def load_state(state_file):
    """加载同步/备份状态，返回已成功记录的集合"""
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                return set(json.load(f))
        except Exception as e:
            log(f"读取状态文件失败: {e}，重新开始")
    return set()


def save_state(state_file, state_set):
    """保存状态到文件"""
    try:
        with open(state_file, 'w', encoding='utf-8') as f:
            json.dump(sorted(state_set), f, ensure_ascii=False)
    except Exception as e:
        log(f"保存状态文件失败: {e}")


def get_file_hash(filepath):
    """快速获取文件指纹（mtime + size），比 MD5 快得多"""
    try:
        stat = os.stat(filepath)
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except Exception:
        return None


def main():
    """入口"""
    os.makedirs(WORK_DIR, exist_ok=True)

    # 解析参数
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"

    if mode == "sync":
        log("=" * 50)
        log("开始同步到飞牛 NAS")
        sync_files()
        log("同步任务结束")
    elif mode == "backup":
        log("=" * 50)
        log("开始备份 device_info.json 到飞牛 NAS")
        backup_device_db()
        log("备份任务结束")
    else:
        log("=" * 50)
        log("ZR Monitor: 同步 + 备份")

        log("\n--- 同步 CSV 文件 ---")
        sync_files()

        log("\n--- 备份 device_info.json ---")
        backup_device_db()

        log("\n统一任务结束")


if __name__ == "__main__":
    main()
