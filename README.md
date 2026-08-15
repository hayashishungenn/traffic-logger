# traffic-logger

Windows 本地连接元数据监控工具 (v1.1.0-dev)。

以固定采样间隔记录本机连接的**累计上下行字节、速率、真实目标 IP 与端口**，全部数据只在本机落盘，不读取通信内容、不采集进程信息、不上传任何数据。

- 主要语言: Python 3 (生产运行仅使用标准库)
- 运行环境: Windows 10/11, Python 3.10+
- 数据边界: 仅处理 `时间 / 本地IP:端口 / 真实目标IP:端口 / 协议 / 上传字节 / 下载字节 / 连接数 / 采集状态`

## 目录结构

```text
traffic_logger/
├── traffic_logger.py   # 主程序
├── config.json         # 配置 (不含真实密钥)
├── install.bat         # 安装: 注册登录自启计划任务
├── uninstall.bat       # 卸载: 停止实例 + 删除计划任务, 保留日志
├── README.md           # 本文档
├── tests/              # 自动化测试
└── logs/               # 运行时输出 (不提交到版本库)
```

## 安装

1. 安装 Python 3.10+ 并加入 `PATH`。
2. 编辑 `config.json`:
   - `api.url`: 只允许本机回环地址 (如 `http://127.0.0.1:9097`)。
   - `api.secret`: 设置为本地数据接口的认证密钥 (必须是真实密钥, 占位符会在启动时被拒绝)。
3. 双击 `install.bat`:
   - 检查脚本与 Python 是否可用;
   - 注册计划任务 `traffic_logger_autostart` (登录时后台运行);
   - 失败时返回非零退出码。
   - 如需自定义 Python, 先执行 `set PYTHONW=<pythonw 完整路径>`。

> 安装脚本使用项目相对路径 (`%~dp0`), 不依赖开发机绝对路径; 移动整个目录后重新执行安装即可。

## 配置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `api.url` | `http://127.0.0.1:9097` | 仅允许本机回环地址; 不接受用户信息/路径/查询/片段/远程主机 |
| `api.secret` | `set-your-secret` | 本地接口认证; 必须非空; 不进入任何日志/输出 |
| `monitor.interval_seconds` | `2` | 采样间隔, 有限数值, 1–86400 秒 |
| `monitor.conn_snapshot_every` | `30` | 快照周期, 正整数, 不小于采样间隔且 ≤ 86400 |
| `monitor.log_retention_days` | `180` | 日志保留天数, 正整数, ≤ 3650; 按适用制度评估磁盘容量 |
| `monitor.gateway_log_path` | `%LOCALAPPDATA%\traffic_logger\connection_events.log` | 本地事件文件 (仅本地路径) |
| `monitor.log_dir` | `logs` | 日志目录; 清理仅限本程序生成的文件 |

配置边界:

- `api.secret` **必须是非空字符串**; `config.json` 默认的 `set-your-secret` 是开发占位符 —— 校验阶段 (开发配置) 允许, 但**生产启动校验会拒绝占位密钥并返回退出码 2**, 请替换为实际本地接口认证密钥后再运行。
- `api.url` 只接受回环地址; `localhost` 仅在本机解析为 `127.0.0.1` 或 `::1` (两者都是本机回环地址), 不用于任何远程解析。
- `interval_seconds`、`--interval`、`conn_snapshot_every`、`log_retention_days` 拒绝 `NaN`、正负无穷和超出上限的值。
- `log_dir` 与 `gateway_log_path` 只接受本机本地路径 (相对项目目录或本机绝对路径); **拒绝 UNC、网络共享、URL、设备路径和本机保留设备名**。

## 启动 / 停止

前台启动 (同一终端观察状态):

```powershell
python traffic_logger.py
python traffic_logger.py --config config.json
python traffic_logger.py --interval 5
```

停止: 前台按 `Ctrl+C`; 计划任务模式运行 `uninstall.bat`。

单实例保护: 重复启动会提示 `INSTANCE_EXISTS` 并正常退出, 不影响已运行实例与其日志。

## 输出文件 (logs/)

| 文件 | 内容 |
|---|---|
| `connections_detail_YYYYMMDD.jsonl` | 每个有效连接每次采样一行; 字段见下 |
| `traffic_YYYYMMDD.csv` | 周期汇总 (速率/连接数/有效与无效数/累计量) |
| `connections_YYYYMMDD.log` | 人工可读快照, 最多 40 条按字节数排序的有效连接 |
| `events_YYYYMMDD.log` | 本地事件文件增量读取, 仅 `ts / protocol / remote_ip / remote_port` |
| `app.log` | 程序状态: 启动/停止/重复实例/源不可达与恢复/无效记录/计数器回退/写盘失败/轮转/清理/未捕获异常 |

JSONL 明细字段:

```json
{"ts":"2026-08-15T10:20:30+08:00","local_ip":null,"local_port":null,
 "remote_ip":"203.0.113.10","remote_port":443,"protocol":"TCP",
 "up_bytes":1024,"down_bytes":2048,"source":"local_api"}
```

约束:
- `remote_ip` 只接受通过标准库校验的 IPv4/IPv6 字面量; **域名或无法确认的文本一律不进入明细**, 只计入无效计数。
- `up_bytes` / `down_bytes` 为非负整数; 计数器回退时重新建立基线, 不输出负速率。
- CSV 固定表头 `timestamp,upload_kbs,download_kbs,active_connections,valid_connections,invalid_connections,total_upload_bytes,total_download_bytes`, 已有文件不重复写表头。
- **部分数据源异常** (T-02): 聚合总量 `downloadTotal`/`uploadTotal` 非法时, 该周期**仍会写入有效连接 JSONL 明细并处理本地事件**, 但不会写伪造的 CSV 累计值/速率, 快照中的速率显示为 `up_kbs=null down_kbs=null`, 并在 `app.log` 记录 `INVALID_RECORD aggregate ... invalid`; 计数器回退只重建速率基线, 不删除当周期明细。
- 文件名含本地日期, 跨日自动切换; 超过保留期的文件由程序自动清理, **只删除本程序生成的文件名模式** (`app.log`、`traffic_YYYYMMDD.csv`、`connections_YYYYMMDD.log`、`connections_detail_YYYYMMDD.jsonl`、`events_YYYYMMDD.log` 及程序临时文件), 同目录下的其他文件、子目录内容一律保留, 清理不跟随子目录。

## 卸载

运行 `uninstall.bat`:

1. 仅停止命令行包含本项目脚本路径的进程;
2. 删除本项目计划任务 `traffic_logger_autostart`;
3. **默认保留** `logs/` 目录, 如需删除请手动清理。

## 测试

```powershell
python -m unittest discover -s tests -v
```

覆盖: 配置校验、IP/端口/字节/时间、标准化拒绝路径、JSONL/CSV/快照契约、跨日轮转与清理、事件增量读取与坏行、单实例、密钥不外泄、以及本地回环数据源集成 (含计数器回退与源中断/恢复)。

## 排障

| 现象 | 处理 |
|---|---|
| `INSTANCE_EXISTS` | 已有实例运行, 无需处理; 如需重启先停止旧实例 |
| `SOURCE_UNAVAILABLE` | 本地数据接口不可达; 检查接口与 `api.url`, 程序会自动重试并在恢复后继续 |
| `INVALID_RECORD` | 数据源返回了域名/越界端口/非法字节; 属预期丢弃, 计入无效计数 |
| `WRITE_ERROR` | 磁盘不可写; 检查 `monitor.log_dir` 权限, 程序以非零状态退出 |
| 配置错误提示 | 按提示修正对应字段后再启动 |

## 权限与合规说明

- 本工具只监控**部署人拥有管理权或已取得明确授权**的本机数据源, 数据仅本地留存。
- 日志目录建议使用操作系统权限限制访问; 是否适用网络日志留存义务、留存期限与个人信息保护要求, 由部署单位结合主体身份、数据类型、内部制度与适用法律法规确定。
- 本工具不构成合规认证, 也不替代部署单位的法律、隐私与网络安全评估。
