#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日 23:00 推送 DeepSeek API 余额到 Telegram
"""

import os
import json
import datetime

WORK_DIR = os.path.expanduser("~/zr_monitor")
BALANCE_STATE_FILE = os.path.join(WORK_DIR, "ds_balance_state.json")

# ================= 读取 API Key =================
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# 如果环境变量未设置，尝试从 .env 文件加载
if not DEEPSEEK_API_KEY:
    env_file = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    if key == "DEEPSEEK_API_KEY" and not DEEPSEEK_API_KEY:
                        DEEPSEEK_API_KEY = value
        except Exception:
            pass

# ================= 飞书推送 =================
from feishu_push import send_feishu_message

def send_telegram_message(message: str) -> bool:
    """发送飞书消息（替代原 Telegram 推送）"""
    return send_feishu_message(message)


def load_balance_state() -> dict:
    """加载上一次的余额记录"""
    if os.path.exists(BALANCE_STATE_FILE):
        try:
            with open(BALANCE_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_balance_state(state: dict):
    """保存本次余额记录"""
    try:
        with open(BALANCE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def query_balance() -> tuple[float | None, str]:
    """
    查询 DeepSeek 余额
    返回 (余额, 状态消息)
    """
    if not DEEPSEEK_API_KEY:
        return None, "❌ 未配置 DEEPSEEK_API_KEY"

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }

    import requests
    try:
        r = requests.get("https://api.deepseek.com/user/balance", headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("is_available"):
                for info in data.get("balance_infos", []):
                    total = float(info.get("total_balance", 0))
                    currency = info.get("currency", "CNY")
                    return total, f"✅ 查询成功"
            return None, f"❌ 余额数据异常: {data}"
        else:
            return None, f"❌ API 返回 {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return None, f"❌ 请求失败: {e}"


def main():
    today = datetime.date.today().strftime("%Y-%m-%d")

    balance, status = query_balance()

    if balance is None:
        message = (
            f"💳 *DeepSeek 余额日报* — {today}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{status}"
        )
        send_telegram_message(message)
        return

    # 对比上次余额
    prev_state = load_balance_state()
    prev_balance = prev_state.get("balance")
    change = ""
    if prev_balance is not None:
        diff = balance - prev_balance
        if diff < 0:
            change = f"📉 较上次减少 {abs(diff):.2f} 元"
        elif diff > 0:
            change = f"📈 较上次增加 {diff:.2f} 元"
        else:
            change = "➖ 较上次无变化"

    # 保存本次余额
    save_balance_state({
        "balance": balance,
        "date": today,
        "updated_at": datetime.datetime.now().isoformat()
    })

    # 构造消息
    message = (
        f"💳 *DeepSeek API 余额日报*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 日期：{today}\n"
        f"💰 余额：**{balance:.2f} 元**\n"
        f"🔁 {change}"
    )

    ok = send_telegram_message(message)
    if not ok:
        # 如果推送失败，打印到 stdout 给 journal
        print(f"[{today}] 余额: {balance:.2f}元, 推送: 失败")
    else:
        print(f"[{today}] 余额: {balance:.2f}元, 推送: 成功")


if __name__ == "__main__":
    main()
