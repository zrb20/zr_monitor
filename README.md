# ZR Monitor · 自动化网络监控系统

基于 **SNMP / HTTP API** 的家庭网络自动化监控系统，实现设备状态采集、异常告警、数据归档与报表推送的完整闭环。

> 语言：Python · 部署：Linux / 树莓派 / 低功耗主机

---

## 功能特性

- **SNMP 设备采集**：1 分钟级轮询网络设备在线状态与端口流量，按年月日时分片归档为 CSV 时间序列矩阵
- **资产自动发现**：通过 ARP 表自动发现网络终端，结合 OUI 库（`oui_mapping.py`，6 万+ 厂商前缀）识别设备类型
- **名称自动映射**：对接爱快（iKuai）路由器 API 自动获取设备备注名，新设备无需手工录入
- **异常告警**：设备离线/状态变化触发飞书消息推送（Webhook + 应用消息双通道）
- **链路巡检**：SSH 巡检 PVE 主机物理网卡（ethtool）状态，监控链路健康
- **数据归档**：CSV 时间序列 → MariaDB（支持 SQL 查询历史在线率、活跃设备等指标）
- **报表推送**：每日 08:00 自动生成设备在线日报、23:00 推送 API 余额巡检
- **NAS 备份**：CSV 增量 + 数据库定时同步至 NAS（SMB），保留多版本

---

## 模块结构

| 模块 | 职责 |
|------|------|
| `silent_collector.py` | 主采集循环：SNMP 轮询 + ARP 发现 + 爱快 API + PVE 链路巡检 + CSV 归档 |
| `fetch_ikuai_names.py` | 爱快设备名称异步抓取（队列驱动，不阻塞主循环） |
| `oui_mapping.py` | OUI 厂商前缀库：MAC → 厂商/设备类型识别 |
| `sync_to_mysql.py` | CSV/JSON → MariaDB 外挂同步（cron 1-5 分钟） |
| `import_to_mysql.py` | 一次性历史数据导入 MariaDB |
| `daily_report.py` | 每日在线日报生成与推送 |
| `check_deepseek_balance.py` | API 余额定时巡检与推送 |
| `zr_sync_backup.py` | NAS SMB 统一同步/备份（sync / backup / both） |
| `feishu_push.py` | 飞书消息推送工具模块 |
| `env_config.py` | 统一配置加载（从 `.env` 读凭据） |

---

## 快速开始

```bash
# 1. 准备运行目录
mkdir -p ~/zr_monitor

# 2. 安装依赖
pip install requests pymysql smbprotocol pytz   # 按实际脚本所需

# 3. 配置凭据（模板）
cp .env.example ~/zr_monitor/.env
vim ~/zr_monitor/.env   # 填入爱快/NAS/PVE 地址与凭据

# 4. 启动主采集
python3 ~/zr_monitor/silent_collector.py
```

### 推荐 cron 编排

```cron
# 主采集（守护/常驻，或每分钟由 cron 拉起）
* * * * * python3 ~/zr_monitor/silent_collector.py

# CSV → MySQL 同步
*/2 * * * * python3 ~/zr_monitor/sync_to_mysql.py

# 每日日报
0 8 * * * python3 ~/zr_monitor/daily_report.py

# 余额巡检
0 23 * * * python3 ~/zr_monitor/check_deepseek_balance.py

# NAS 备份
0 2 * * * python3 ~/zr_monitor/zr_sync_backup.py both
```

---

## 数据流

```
网络设备 ──SNMP/ARP──> silent_collector ──CSV 时间序列──> matrix_data/
    │                        │
    │ 爱快 API               └──sync_to_mysql──> MariaDB（历史可查）
    │
    └── 状态变化 ──> feishu_push ──> 飞书告警
```

## 配置说明

凭据统一从 `.env` 读取（`env_config.py`），**不硬编码在代码中**。模板见 `.env.example`：

```
# 爱快路由器
IKUAI_URL=...
IKUAI_USER=...
IKUAI_PASS=...

# NAS (SMB)
NAS_IP=...
NAS_SHARE=...
NAS_USER=...
NAS_PASSWORD=...

# PVE 链路巡检
PVE_HOST=...
PVE_USER=...
PVE_PASS=...
```

---

## License

个人项目，仅用于学习与家庭自用。
