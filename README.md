# traffic-logger

Windows 本机连接元数据监控工具（Python 3 标准库，无第三方运行依赖）。

以固定采样间隔读取**部署人授权**的本机数据源提供的连接元数据（真实目标 IP:端口、协议、上下行累计字节），校验后按日落盘为 JSONL 明细、CSV 汇总、文本快照与事件记录。数据只写本机，不读取通信内容、不采集进程信息、不上传任何数据、不修改网络行为。

> 当前版本：v1.2.0-dev。**标记为生产可交付前，需在获得授权的本机对真实数据源完成一次 60 秒人工验收**，验收记录见 [`ACCEPTANCE_v1.2.0.md`](ACCEPTANCE_v1.2.0.md)。

## 特性

- **最小必要字段**：只处理时间 / 本地 IP:端口 / 真实目标 IP:端口 / 协议 / 上下行字节 / 连接数 / 采集状态。
- **真实端点校验**：`remote_ip` 仅接受通过标准库校验的 IPv4/IPv6 字面量；域名、空值、越界值一律按无效记录计数丢弃，不猜测、不兜底。
- **速率不造假**：按累计量差值 ÷ 实际经过时间计算 KB/s；计数器回退自动重建基线，不输出负速率；不把缺失值写成 0。
- **聚合异常不吞明细**：`downloadTotal`/`uploadTotal` 缺失或非法时，有效连接明细照常落盘、事件照常处理，CSV 不写伪造累计值，快照速率记为 `null`；只有连接列表缺失或接口不可访问才判定数据源不可用。
- **本地边界**：仅访问本机回环地址；`localhost` 会在实际连接前解析并确认解析结果属于回环地址，解析到远程地址即拒绝。
- **可运维**：按日轮转、保留期清理（只删本程序生成的文件）、`app.log` 按大小轮转、单实例保护、输入体积上限、事件文件游标/长度边界加固。
- **Windows 自启**：`install.bat`/`uninstall.bat` 注册/移除登录计划任务，脚本仅 ASCII+CRLF，不依赖终端编码。

## 数据边界

| 记录 | 不记录 |
|---|---|
| 时间、本地端点、真实目标 IP:端口、协议、上下行累计字节、连接数、采集状态 | 通信内容/报文、进程名、账号、规则、节点、路径、域名猜测值、令牌 |

## 运行环境

- Windows 10/11，Python 3.10+
- 生产运行仅使用 Python 标准库

## 快速开始

1. 安装 Python 3.10+ 并加入 `PATH`。
2. 编辑 `config.json`：
   - `api.url`：本机回环地址（如 `http://127.0.0.1:9097`），仅允许回环地址。
   - `api.secret`：本地数据接口的**真实认证密钥**（占位符会在生产启动时被拒绝，退出码 2）。
3. 前台运行：

   ```powershell
   python traffic_logger.py
   python traffic_logger.py --config config.json
   python traffic_logger.py --interval 5
   ```

4. 停止：前台按 `Ctrl+C`。

启动退出码：`0` 正常/停止；`1` 初始化或写盘失败；`2` 配置错误。

## 配置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `api.url` | `http://127.0.0.1:9097` | 仅允许本机回环地址；不接受用户信息/路径/查询/片段/远程主机 |
| `api.secret` | `set-your-secret` | 本地接口认证；必须非空；不进入任何日志/输出；占位符在生产启动被拒 |
| `monitor.interval_seconds` | `2` | 采样间隔，有限数值，1–86400 秒 |
| `monitor.conn_snapshot_every` | `30` | 快照周期，正整数，不小于采样间隔且 ≤ 86400 |
| `monitor.log_retention_days` | `180` | 日志保留天数，正整数，≤ 3650；按适用制度评估磁盘容量 |
| `monitor.gateway_log_path` | `%LOCALAPPDATA%\traffic_logger\connection_events.log` | 本地事件文件（仅本地路径） |
| `monitor.log_dir` | `logs` | 日志目录；清理仅限本程序生成的文件 |

配置边界：

- `api.secret` **必须是非空字符串**；`config.json` 默认的 `set-your-secret` 是开发占位符 —— 校验阶段允许，但**生产启动校验会拒绝占位密钥并返回退出码 2**。
- `api.url` 只接受回环地址；允许保留 `localhost`，但程序会在实际连接前解析并确认解析结果属于回环地址（`127.0.0.1`/`::1`），解析到非回环地址或无法解析时拒绝启动。
- `interval_seconds`、`--interval`、`conn_snapshot_every`、`log_retention_days` 拒绝 `NaN`、正负无穷和超出上限的值。
- `log_dir` 与 `gateway_log_path` 只接受本机本地路径（相对项目目录或本机绝对路径）；**拒绝 UNC、网络共享、URL、设备路径和本机保留设备名**。

## 输出文件

| 文件 | 内容 |
|---|---|
| `connections_detail_YYYYMMDD.jsonl` | 每个有效连接每次采样一行；字段见下 |
| `traffic_YYYYMMDD.csv` | 周期汇总（速率/连接数/有效与无效数/累计量） |
| `connections_YYYYMMDD.log` | 人工可读快照，最多 40 条按字节数排序的有效连接 |
| `events_YYYYMMDD.log` | 本地事件文件增量读取，仅 `ts / protocol / remote_ip / remote_port` |
| `app.log` | 程序状态；超过 5 MiB 自动改名 `app_YYYYMMDD_HHMMSS_mmm.log` 重新开始，轮转文件受保留期清理 |

JSONL 明细字段：

```json
{"ts":"2026-08-15T10:20:30+08:00","local_ip":null,"local_port":null,
 "remote_ip":"203.0.113.10","remote_port":443,"protocol":"TCP",
 "up_bytes":1024,"down_bytes":2048,"source":"local_api"}
```

约束：

- `remote_ip` 只接受通过标准库校验的 IPv4/IPv6 字面量；**域名或无法确认的文本一律不进入明细**，只计入无效计数。数据源的真实目标 IP 取自 `metadata.destinationIP` 或 `metadata.remoteDestination`，支持裸 IPv4（`203.0.113.10`）、裸 IPv6（`2001:db8::1`）和带括号 IPv6（`[2001:db8::1]`），都会规范为裸地址写入 `remote_ip`（契约夹具见 `tests/fixtures/`）。
- `up_bytes` / `down_bytes` 为非负整数；计数器回退时重新建立基线，不输出负速率。
- CSV 固定表头 `timestamp,upload_kbs,download_kbs,active_connections,valid_connections,invalid_connections,total_upload_bytes,total_download_bytes`，已有文件不重复写表头。
- **部分数据源异常**：聚合总量 `downloadTotal`/`uploadTotal` **缺失或非法**时，该周期仍会标准化并写入有效连接 JSONL 明细并处理本地事件（不会误判为数据源不可用），但不写伪造的 CSV 累计值/速率，快照速率显示为 `up_kbs=null down_kbs=null`，并在 `app.log` 记录 `INVALID_RECORD aggregate ... invalid`；计数器回退只重建速率基线，不删除当周期明细。
- 文件名含本地日期，跨日自动切换；超过保留期的文件由程序自动清理，**只删除本程序生成的文件名模式**（`app.log`、`app_YYYYMMDD_HHMMSS_mmm.log`、`traffic_YYYYMMDD.csv`、`connections_YYYYMMDD.log`、`connections_detail_YYYYMMDD.jsonl`、`events_YYYYMMDD.log` 及程序临时文件），同目录下的其他文件、子目录内容一律保留，清理不跟随子目录。

## 开机自启（安装 / 卸载）

`install.bat` 注册登录时后台运行的计划任务 `traffic_logger_autostart`：

- 检查脚本与 Python（优先 `pythonw.exe`，回退 `python.exe`，均不可用则非零退出）；
- 注册后使用 `schtasks /Query` 回读任务名、命令与触发条件；
- 默认不暂停（适合自动化），交互式查看运行 `install.bat interactive`；
- 任务名可用环境变量 `TL_TASK_NAME` 覆盖（多实例/测试用，不影响其他计划任务）；
- 脚本正文只使用 ASCII 字符和 CRLF 换行，不依赖终端默认编码即可正常解析。

`uninstall.bat`：

- 仅停止命令行中**精确包含规范化后的本项目脚本路径**（作为独立词边界，不匹配 `traffic_logger.py.bak` 之类前缀）的 `python`/`pythonw` 进程，停止后回读进程是否仍存在，未停止时返回非零码并保留日志；
- 删除本项目计划任务；**默认保留** `logs/` 目录；
- 脚本路径包含空格、括号、中文或 `&` 时仍可正常执行。

## 测试

```powershell
python -m unittest discover -s tests -v
```

覆盖：配置校验（含 localhost 回环解析边界）、IP/端口/字节/时间、标准化拒绝路径、聚合字段缺失/非法时的明细保留、JSONL/CSV/快照契约、跨日轮转与清理、`app.log` 轮转、事件增量读取与长度边界、单实例、密钥不外泄、本地回环数据源集成（含计数器回退与源中断/恢复）、输入体积上限、Windows 安装/卸载脚本（ASCII/CRLF、精确停止、前缀碰撞拒绝）与数据源契约夹具。当前共 116 个自动化测试。

## 人工验收与可交付状态

自动化测试之外，还需在**获得明确授权**的本机环境中对真实本地数据源完成一次人工验收，未完成前不标记为生产可交付。步骤：

1. 使用临时日志目录启动，连续运行不少于 60 秒。
2. 检查 `connections_detail_*.jsonl`、`traffic_*.csv`、`connections_*.log`、`events_*.log` 和 `app.log` 的生成、字段与追加行为；确认 `remote_ip` 可通过标准库 IP 校验。
3. 模拟接口不可达与恢复、计数器回退、事件文件追加/替换/截断/缺失恢复、磁盘不可写，检查对应状态记录。
4. 停止后再次启动，确认单实例锁释放、日志无缺失、目录内无无关文件被清理删除。
5. 使用临时计划任务完成安装/卸载脚本回读，确认不删除 `logs/`、不影响其他任务与 Python 进程。

验收记录见 [`ACCEPTANCE_v1.2.0.md`](ACCEPTANCE_v1.2.0.md)（含未执行项与阻塞原因，不伪造完成）。

## 排障

| 现象 | 处理 |
|---|---|
| `INSTANCE_EXISTS` | 已有实例运行，无需处理；如需重启先停止旧实例 |
| `SOURCE_UNAVAILABLE` | 本地数据接口不可达；检查接口与 `api.url`，程序会自动重试并在恢复后继续 |
| `INVALID_RECORD` | 数据源返回了域名/越界端口/非法字节或非法聚合量；属预期丢弃，计入无效计数，明细不受影响 |
| `WRITE_ERROR` | 磁盘不可写；检查 `monitor.log_dir` 权限，程序以非零状态退出 |
| 配置错误提示 | 按提示修正对应字段后再启动 |

## 合规说明

- 本工具只监控**部署人拥有管理权或已取得明确授权**的本机数据源，数据仅本地留存。
- 日志目录建议使用操作系统权限限制访问；是否适用网络日志留存义务、留存期限与个人信息保护要求，由部署单位结合主体身份、数据类型、内部制度与适用法律法规确定。
- 本工具不构成合规认证，也不替代部署单位的法律、隐私与网络安全评估。
