# -*- coding: utf-8 -*-
"""
traffic-logger 本地连接元数据监控 (v1.1.0-dev)

范围:
  - 以固定采样间隔记录本机连接的累计上下行字节与速率
  - 仅记录数据源明确提供的: 时间 / 本地 IP:端口 / 真实目标 IP:端口 / 协议
  - JSONL 连接明细、CSV 周期汇总、文本快照、事件记录、程序日志
  - 按日轮转、按保留期清理、单实例保护、仅允许本机回环数据源

明确不做:
  - 不读取、解析、保存或展示通信内容与报文载荷
  - 不把域名或任意文本猜测成 remote_ip
  - 不采集进程、规则、节点、账号、令牌等信息
  - 不访问远程服务, 不上传日志, 不修改网络行为

依赖: 仅 Python 3 标准库
运行环境: Windows 10/11, Python 3.10+
用法:
  python traffic_logger.py
  python traffic_logger.py --config config.json
  python traffic_logger.py --interval 5
"""
import argparse
import datetime
import ipaddress
import json
import math
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

VERSION = "1.1.0-dev"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")

# 数据契约常量 (见 TASK_BOOK.md 第 5 节)
CSV_HEADER = ("timestamp,upload_kbs,download_kbs,active_connections,"
              "valid_connections,invalid_connections,total_upload_bytes,"
              "total_download_bytes")
SOURCE_API = "local_api"          # 主明细固定数据源类别
MAX_SNAPSHOT_LINES = 40           # 快照最多展示的有效连接数
EVENT_FAIL_LOG_INTERVAL = 60.0    # 事件解析失败限频 (秒)
RETRY_SLEEP = 5.0                 # 接口失败时的重试间隔 (秒)
PORT_MIN, PORT_MAX = 1, 65535

# 事件文件单次读取上限: 单行最大长度与单次最大行数, 避免无限积压一次读入内存。
MAX_EVENT_LINE_LEN = 4096
MAX_EVENT_LINES_PER_READ = 2000
_OVERLONG = object()              # EventTail 丢弃超长行时的哨兵值

# 本地 HTTP 响应与单字段长度上限 (T-06): 超限按数据源异常处理, 不整体载入内存。
MAX_RESPONSE_BYTES = 1 << 20      # 单次接口响应上限: 1 MiB
MAX_PROTOCOL_LEN = 16             # 协议字段长度上限

# 配置数值边界: 拒绝 NaN/无穷/负值/超出合理上限的值 (T-04)。
MAX_INTERVAL_SECONDS = 86400      # 采样间隔上限: 24 小时
MAX_SNAP_EVERY = 86400            # 快照周期上限: 24 小时
MAX_RETENTION_DAYS = 3650         # 保留天数上限: 10 年

# 明确允许的开发占位密钥; validate_config 允许 (开发), main() 生产启动校验拒绝。
PLACEHOLDER_SECRETS = frozenset({"set-your-secret", "changeme", "your-secret",
                                 "change-me", "change-me-please", "changeme123"})

# 清理白名单: 只有本程序明确生成的日志/临时文件才允许被 retention 删除。
# 使用完整文件名匹配, 不允许目录名、前缀或模糊后缀扩大删除范围。
_PROGRAM_FILE_PATTERNS = (
    re.compile(r"^app\.log$"),
    re.compile(r"^traffic_\d{8}\.csv$"),
    re.compile(r"^connections_\d{8}\.log$"),
    re.compile(r"^connections_detail_\d{8}\.jsonl$"),
    re.compile(r"^events_\d{8}\.log$"),
    re.compile(r"^traffic_logger_[\w.\-]+\.tmp$"),   # 程序预留的临时文件模式
)


# ---------------------------------------------------------------- 基础工具

def now():
    """程序日志时间戳: YYYY-MM-DD HH:MM:SS (本地时间)。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_iso():
    """带时区偏移的 ISO 8601 时间, 同一采样周期内保持一致。"""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def date_str():
    """本地日期 YYYYMMDD, 用于日志文件名 (便于测试注入)。"""
    return datetime.date.today().strftime("%Y%m%d")


def expand_path(p, base):
    """展开环境变量并把相对路径锚定到 base 目录。"""
    p = os.path.expandvars(str(p))
    if not os.path.isabs(p):
        p = os.path.join(base, p)
    return p


def is_program_log_filename(name):
    """仅当文件名精确匹配本程序生成的文件模式时才返回 True。

    完整匹配, 不允许通过目录名、前缀或模糊后缀扩大删除范围。
    """
    if not isinstance(name, str):
        return False
    return any(p.match(name) for p in _PROGRAM_FILE_PATTERNS)


_RESERVED_DEVICE_NAMES = frozenset(
    "CON PRN AUX NUL".split()
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)])


def _is_remote_or_device_path(path):
    """判断解析后的路径是否指向网络位置或设备路径 (UNC 前缀 / URI / 设备前缀)。"""
    p = path.replace("/", "\\")
    if p.startswith("\\\\"):
        return True   # UNC 共享 / 反斜杠设备路径 / 扩展长度路径
    if "://" in p:
        return True   # 残留 URI
    return False


def validate_local_path(value, field):
    """校验本地路径: 非空字符串, 且不是 URL/UNC/设备路径; 失败抛 ConfigError。"""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"字段: {field} | 问题: 不能为空 | 建议: 填写本地路径")
    v = value.strip()
    if "://" in v:
        raise ConfigError(
            f"字段: {field} | 问题: 只允许本地路径 | 建议: 不接受 URL 或网络位置")
    if _is_remote_or_device_path(v):
        raise ConfigError(
            f"字段: {field} | 问题: 拒绝网络路径/设备路径 | 建议: 填写本机本地路径")
    base_name = os.path.basename(v.rstrip("\\/"))
    if base_name.upper() in _RESERVED_DEVICE_NAMES:
        raise ConfigError(
            f"字段: {field} | 问题: 文件名 '{base_name}' 是本机保留设备名 | 建议: 更换文件名")
    return v


def expand_local_path(value, field, base):
    """校验并展开本地路径; 展开后的绝对位置必须仍在本机, 不允许指向远程位置。"""
    v = validate_local_path(value, field)
    p = expand_path(v, base)
    ap = os.path.abspath(p)
    if _is_remote_or_device_path(ap):
        raise ConfigError(
            f"字段: {field} | 问题: 路径解析后指向网络/设备位置 | 建议: 填写本机本地路径")
    return p


# ---------------------------------------------------------------- 输入校验

def is_valid_ip(value):
    """仅接受格式正确的 IPv4/IPv6 字面量。域名、空值、任意文本一律拒绝。"""
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        ipaddress.ip_address(value.strip())
        return True
    except ValueError:
        return False


def parse_port(value):
    """把 1-65535 的整数(或纯数字字符串)安全转换为 int; 其余返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        v = value
    elif isinstance(value, str):
        s = value.strip()
        if not s.isdigit():
            return None
        v = int(s)
    else:
        return None
    if PORT_MIN <= v <= PORT_MAX:
        return v
    return None


def to_nonneg_int(value):
    """仅接受非负整数(或纯数字字符串); 其他类型返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return int(s)
    return None


# ---------------------------------------------------------------- 配置

class ConfigError(Exception):
    """配置校验失败; 消息包含字段名与修复建议。"""


def _require_dict(obj, field):
    if not isinstance(obj, dict):
        raise ConfigError(f"字段: {field} | 问题: 必须是 JSON 对象 | 建议: 检查配置结构")
    return obj


def _validate_loopback_url(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"字段: {field} | 问题: 不能为空 | 建议: 填写本机回环地址, 如 http://127.0.0.1:9097")
    try:
        u = urllib.parse.urlsplit(value)
    except ValueError:
        raise ConfigError(
            f"字段: {field} | 问题: URL 无法解析 (协议/端口不合法) | 建议: 如 http://127.0.0.1:9097")
    if u.scheme not in ("http", "https"):
        raise ConfigError(
            f"字段: {field} | 问题: 协议仅允许 http/https | 建议: 如 http://127.0.0.1:9097")
    if u.username is not None or u.password is not None:
        raise ConfigError(
            f"字段: {field} | 问题: 不允许包含用户信息 | 建议: 使用纯回环地址")
    if u.query or u.fragment:
        raise ConfigError(
            f"字段: {field} | 问题: 不允许带查询参数或片段 | 建议: 使用纯回环地址")
    if u.path not in ("", "/"):
        raise ConfigError(
            f"字段: {field} | 问题: 不允许带路径 | 建议: 只填根地址, 如 http://127.0.0.1:9097")
    if not u.hostname:
        raise ConfigError(
            f"字段: {field} | 问题: 缺少主机地址 | 建议: 如 http://127.0.0.1:9097")
    try:
        port = u.port
    except ValueError:
        raise ConfigError(
            f"字段: {field} | 问题: 端口必须在 1-65535 | 建议: 如 http://127.0.0.1:9097")
    if port is None or port < 1:
        raise ConfigError(
            f"字段: {field} | 问题: 必须显式指定 1-65535 的端口 | 建议: 如 http://127.0.0.1:9097")
    host = u.hostname
    if host == "localhost":
        # localhost 在本机可能解析为 127.0.0.1 或 ::1, 两者都是本机回环地址;
        # 不用于任何远程解析, 详见 README "配置" 一节。
        return value
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ConfigError(
            f"字段: {field} | 问题: 地址不是本机回环地址 | 建议: 只允许 127.0.0.0/8 或 ::1")
    if not ip.is_loopback:
        raise ConfigError(
            f"字段: {field} | 问题: 地址不是本机回环地址 | 建议: 只允许 127.0.0.0/8 或 ::1")
    return value


def _validate_float(value, field, minimum, default, hint, maximum=None):
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"字段: {field} | 问题: 必须是数值 | 建议: {hint}")
    value = float(value)
    if not math.isfinite(value):
        raise ConfigError(f"字段: {field} | 问题: 必须是有限数值 | 建议: {hint}")
    if value < minimum:
        raise ConfigError(f"字段: {field} | 问题: 不得小于 {minimum} | 建议: {hint}")
    if maximum is not None and value > maximum:
        raise ConfigError(
            f"字段: {field} | 问题: 不得大于 {maximum:g} | 建议: {hint}")
    return value


def _validate_positive_int(value, field, default, hint, maximum=None):
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"字段: {field} | 问题: 必须是大于 0 的整数 | 建议: {hint}")
    if maximum is not None and value > maximum:
        raise ConfigError(
            f"字段: {field} | 问题: 不得大于 {maximum} | 建议: {hint}")
    return value


def is_placeholder_secret(s):
    """开发占位密钥判断; 错误信息不依赖该值本身。"""
    return isinstance(s, str) and s.strip().lower() in PLACEHOLDER_SECRETS


def validate_config(cfg, interval_override=None):
    """加载并校验配置, 返回归一化后的配置对象; 失败抛 ConfigError。"""
    if cfg is None:
        raise ConfigError("字段: 配置根 | 问题: 配置为空 | 建议: 提供 config.json")
    cfg = _require_dict(cfg, "配置根")
    api_cfg = _require_dict(cfg.get("api"), "api")
    mon_cfg = _require_dict(cfg.get("monitor"), "monitor")

    api_url = _validate_loopback_url(api_cfg.get("url"), "api.url")
    secret = api_cfg.get("secret")
    if not isinstance(secret, str) or not secret.strip():
        # 错误信息不回显 secret 值, 只提示字段名与修复方向
        raise ConfigError("字段: api.secret | 问题: 必须是非空字符串 | 建议: 设置为本地接口认证密钥")

    interval = _validate_float(mon_cfg.get("interval_seconds"), "monitor.interval_seconds",
                               1, 2, "建议 2-60, 且不小于 1", maximum=MAX_INTERVAL_SECONDS)
    snap_every = _validate_positive_int(mon_cfg.get("conn_snapshot_every"),
                                        "monitor.conn_snapshot_every", 30,
                                        "正整数, 不小于采样间隔", maximum=MAX_SNAP_EVERY)
    if interval_override is not None:
        interval = _validate_float(interval_override, "--interval", 1, interval,
                                   "建议 2-60, 且不小于 1", maximum=MAX_INTERVAL_SECONDS)
    if snap_every < interval:
        raise ConfigError(
            "字段: monitor.conn_snapshot_every | 问题: 不得小于采样间隔 | "
            f"建议: 设置为 >= interval_seconds ({interval:g})")

    retention_days = _validate_positive_int(mon_cfg.get("log_retention_days"),
                                            "monitor.log_retention_days", 180,
                                            "正整数, 按适用制度配置并评估磁盘容量",
                                            maximum=MAX_RETENTION_DAYS)

    event_path = expand_local_path(mon_cfg.get("gateway_log_path"),
                                   "monitor.gateway_log_path", BASE_DIR)

    log_dir = expand_local_path(mon_cfg.get("log_dir"), "monitor.log_dir", BASE_DIR)

    cfg_obj = {
        "api_url": api_url,
        "api_secret": secret,
        "interval": interval,
        "snap_every": snap_every,
        "retention_days": retention_days,
        "event_path": event_path,
        "log_dir": log_dir,
    }
    return cfg_obj


def load_config(path):
    """读取并解析 JSON 配置; 失败时抛 OSError/ValueError, 不进入半初始化。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 标准化

def standardize_connection(rec, ts):
    """把原始连接记录标准化为契约字段。

    返回 (row, None) 或 (None, reject_reason)。
    只有真实 IP 字面量进入 remote_ip; 域名、缺失或越界值一律拒绝。
    """
    if not isinstance(rec, dict):
        return None, "not_object"
    meta = rec.get("metadata")
    if not isinstance(meta, dict):
        meta = {}

    remote_ip = None
    for key in ("destinationIP", "remoteDestination"):
        v = meta.get(key)
        if isinstance(v, str) and v.strip() and is_valid_ip(v):
            remote_ip = v.strip()
            break
    if remote_ip is None:
        return None, "invalid_remote_ip"

    remote_port = parse_port(meta.get("destinationPort"))
    if remote_port is None:
        return None, "invalid_remote_port"

    up = to_nonneg_int(rec.get("upload"))
    if up is None:
        return None, "invalid_up_bytes"
    down = to_nonneg_int(rec.get("download"))
    if down is None:
        return None, "invalid_down_bytes"

    local_ip = meta.get("sourceIP")
    if not (isinstance(local_ip, str) and is_valid_ip(local_ip)):
        local_ip = None
    local_port = parse_port(meta.get("sourcePort"))

    protocol = meta.get("network")
    if isinstance(protocol, str) and protocol.strip():
        protocol = protocol.strip().upper()[:MAX_PROTOCOL_LEN]
    else:
        protocol = None

    return {
        "ts": ts,
        "local_ip": local_ip,
        "local_port": local_port,
        "remote_ip": remote_ip,
        "remote_port": remote_port,
        "protocol": protocol,
        "up_bytes": up,
        "down_bytes": down,
        "source": SOURCE_API,
    }, None


def parse_event_line(line):
    """从本地事件行提取 (protocol, remote_ip, remote_port); 无法确认返回 None。

    通用解析: 协议取 [PROTO] 标签, 目标取 '-->' 后的 IP:端口; 无箭头时取首个
    IP:端口 字面量。IP 部分必须通过标准库校验, 域名行不记录。
    """
    proto = None
    m = re.search(r"\[([A-Za-z0-9_]+)\]", line)
    if m:
        proto = m.group(1).upper()[:MAX_PROTOCOL_LEN]

    target = None
    arrow = re.search(r"-->\s*([^\s\"',]+)", line)
    if arrow:
        target = arrow.group(1)
    else:
        for tok in line.split():
            tok = tok.strip("\"',;")
            if ":" in tok and target is None:
                target = tok

    if target:
        ip, sep, port = target.rpartition(":")
        if sep:
            ip = ip.strip("[]")
            if is_valid_ip(ip):
                p = parse_port(port)
                if p is not None:
                    return proto, ip, p
    return None


# ---------------------------------------------------------------- 日志模块

class Logger:
    """落盘日志器: 按日切换文件、追加表头、保留期清理。

    每次写入独立打开/关闭句柄, 避免跨进程占用; 写盘失败直接抛 OSError,
    由上层以 WRITE_ERROR 暴露并不再继续伪造数据。
    """

    def __init__(self, log_dir, retention_days=180):
        self.log_dir = log_dir
        self.retention_days = retention_days
        self._last_cleanup_day = None
        os.makedirs(log_dir, exist_ok=True)

    @property
    def today(self):
        return date_str()

    def _check_rotate(self):
        """跨日时执行一次旧文件清理 (仅限本目录内过期文件)。"""
        day = self.today
        if day != self._last_cleanup_day:
            self._last_cleanup_day = day
            self.cleanup()

    def write_lines(self, name, lines):
        path = os.path.join(self.log_dir, name)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            for ln in lines:
                f.write(ln if ln.endswith("\n") else ln + "\n")
        self._check_rotate()

    def csv(self, name, header, row):
        path = os.path.join(self.log_dir, name)
        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        lines = [header] if new_file else []
        lines.append(row)
        self.write_lines(name, lines)

    def jsonl_many(self, name, rows):
        if rows:
            self.write_lines(name, [json.dumps(r, ensure_ascii=False) for r in rows])

    def text(self, name, lines):
        if lines:
            self.write_lines(name, lines)

    def app(self, msg):
        self.write_lines("app.log", [f"[{now()}] {msg}"])

    def cleanup(self):
        if not os.path.isdir(self.log_dir):
            return
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        skipped = 0
        for fn in os.listdir(self.log_dir):
            # 只处理本程序明确生成的日志/临时文件, 不越权删除其他文件。
            if not is_program_log_filename(fn):
                skipped += 1
                continue
            fp = os.path.join(self.log_dir, fn)
            try:
                if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
                    removed += 1
                else:
                    skipped += 1
            except OSError:
                skipped += 1  # 只读或占用文件跳过, 不阻断采集
        try:
            self.app(f"CLEANUP removed={removed} skipped={skipped} days={self.retention_days}")
        except OSError:
            pass

    def close(self):
        """无常驻句柄, 无需额外释放; 保留以兼容显式调用。"""


# ---------------------------------------------------------------- 数据源客户端

class DataLimitError(Exception):
    """本地输入超过约定上限 (响应过大等); 按数据源异常处理。"""

    def __init__(self, kind, size=None):
        super().__init__(kind)
        self.kind = kind
        self.size = size


class GatewayApi:
    """本机回环接口客户端; 只访问配置中校验通过的回环地址。"""

    def __init__(self, url, secret):
        self.url = url.rstrip("/")
        self.secret = secret

    def get(self, path):
        req = urllib.request.Request(
            self.url + path,
            headers={"Authorization": f"Bearer {self.secret}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                # 限制单次响应大小: 超限抛 DataLimitError, 不把完整响应载入内存。
                chunk = r.read(MAX_RESPONSE_BYTES + 1)
                if len(chunk) > MAX_RESPONSE_BYTES:
                    raise DataLimitError("http_response_too_large", len(chunk))
                return json.loads(chunk.decode("utf-8"))
        except DataLimitError:
            raise
        except urllib.error.HTTPError as e:
            e.close()  # 释放 HTTPError 响应句柄
            return None
        except Exception:
            return None

    def snapshot(self):
        """返回 (连接列表, 下载累计, 上传累计); 失败或缺少累计量返回 None 元组。"""
        data = self.get("/connections")
        if not isinstance(data, dict):
            return None, None, None
        conns = data.get("connections")
        if not isinstance(conns, list):
            return None, None, None
        d_total = data.get("downloadTotal")
        u_total = data.get("uploadTotal")
        if d_total is None or u_total is None:
            return None, None, None
        return conns, d_total, u_total


class EventTail:
    """本地事件文件偏移游标增量读取。

    通过 (st_dev, st_ino, st_mtime_ns) 记录文件身份: 文件被替换、轮转、截断、
    同大小重写时都能识别并从头读取; 单次读取设行数/行长上限, 游标只在成功
    读取后推进。
    """

    def __init__(self, path, max_lines=MAX_EVENT_LINES_PER_READ,
                 max_line_len=MAX_EVENT_LINE_LEN):
        self.path = path
        self.max_lines = max_lines
        self.max_line_len = max_line_len
        self.seen = 0
        self._identity = None       # 最近一次成功读取时的文件身份
        self.missing = False        # 文件当前是否缺失
        self.reset_reason = None    # 本次 read_new 是否因轮转/替换/截断重置
        self.truncated = False      # 本次读取是否达到行数上限
        self.truncated_total = 0    # 累计达到行数上限次数
        self.overlong = 0           # 累计超长行数

    def _readline_bounded(self, f):
        """读取一行; 超过行长上限时丢弃整行并计数, 不产生截断后看似有效的字段。"""
        line = f.readline(self.max_line_len + 1)
        if not line:
            return None
        if len(line) > self.max_line_len and not line.endswith("\n"):
            self.overlong += 1
            while line and not line.endswith("\n"):
                line = f.readline(self.max_line_len + 1)
            return _OVERLONG
        return line

    def read_new(self):
        self.reset_reason = None
        self.truncated = False
        try:
            st = os.stat(self.path)
        except OSError:
            self.seen = 0
            self._identity = None
            self.missing = True     # 文件缺失视为无新事件; 恢复后由调用方记录
            return []

        if self.missing:
            self.missing = False    # 文件重新出现, 由调用方记录恢复状态

        identity = (st.st_dev, st.st_ino, st.st_mtime_ns)
        size = st.st_size
        if self._identity is None:
            self.seen = 0                       # 首次读取
        elif identity[:2] != self._identity[:2]:
            self.reset_reason = "replaced"      # 文件对象变化 (替换/轮转)
            self.seen = 0
        elif size < self.seen:
            self.reset_reason = "truncated"     # 文件被截断
            self.seen = 0
        elif size == self.seen and identity != self._identity:
            self.reset_reason = "rewritten"     # 同大小内容重写
            self.seen = 0

        try:
            with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self.seen)
                lines = []
                while len(lines) < self.max_lines:
                    line = self._readline_bounded(f)
                    if line is None:
                        break
                    if line is _OVERLONG:
                        continue
                    lines.append(line)
                cookie = f.tell()       # 文本模式下合法的可回溯位置
                self.seen = cookie
                if len(lines) >= self.max_lines:
                    # 因行数上限退出: 探测是否仍有内容, 有则记录截断,
                    # 并让游标回到探测前的位置, 不吞掉下一行的首字符。
                    probe = f.read(1)
                    if probe:
                        self.truncated = True   # 积压超过单次上限, 剩余下次再读
                        self.truncated_total += 1
                        self.seen = cookie
                self._identity = identity
        except OSError:
            return []                           # 读取失败, 游标不推进
        return lines


# ---------------------------------------------------------------- 单实例保护

class SingleInstance:
    """绑定本机回环端口实现单实例; 进程退出后由系统自动释放。"""

    def __init__(self, port=48765):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.port = port
        self.ok = False
        try:
            # 注意: Windows 上 SO_REUSEADDR 允许第二进程抢占同一端口,
            # 因此单实例锁绝不能设置它, 否则保护失效。
            self.sock.bind(("127.0.0.1", port))
            self.sock.listen(1)
            self.port = self.sock.getsockname()[1]
            self.ok = True
        except OSError:
            self.close()

    def __bool__(self):
        return self.ok

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self.ok = False


# ---------------------------------------------------------------- 采集主控

class Monitor:
    """一个采样周期的采集、标准化、统计与落盘; 可独立驱动便于测试。"""

    def __init__(self, cfg, logger, api, events):
        self.cfg = cfg
        self.logger = logger
        self.api = api
        self.events = events
        self.interval = cfg["interval"]
        self.snap_every = cfg["snap_every"]

        self.prev_total = None
        self.prev_sample_time = None
        self.last_snap = 0.0
        self.source_down = False
        self.event_failures = 0
        self.last_event_fail_log = 0.0
        self._prev_events_missing = False
        self._last_event_missing_log = 0.0
        self._last_event_recovered_log = 0.0
        self._last_event_reset_log = 0.0
        self._last_event_limit_log = 0.0
        self._last_truncated_total = 0
        self._last_overlong_total = 0

    def _update_rates(self, d_total, u_total, now_t):
        """累计差值/实际经过时间 → KB/s; 回退时重新建立基线, 不输出负速率。"""
        d = to_nonneg_int(d_total)
        u = to_nonneg_int(u_total)
        if d is None or u is None:
            return None
        if self.prev_total is None:
            self.prev_total = (d, u)
            self.prev_sample_time = now_t
            return 0.0, 0.0
        elapsed = max(now_t - self.prev_sample_time, 0.001)
        d_delta = d - self.prev_total[0]
        u_delta = u - self.prev_total[1]
        if d_delta < 0 or u_delta < 0:
            self.logger.app("COUNTER_RESET counter rolled back, baseline re-established")
            self.prev_total = (d, u)
            self.prev_sample_time = now_t
            return 0.0, 0.0
        down_rate = d_delta / elapsed / 1024.0
        up_rate = u_delta / elapsed / 1024.0
        self.prev_total = (d, u)
        self.prev_sample_time = now_t
        return up_rate, down_rate

    def _log_event_state(self):
        """限频记录事件源缺失/恢复/游标重置/读取上限等状态变化。

        每种状态使用独立时间戳限频, 避免缺失与恢复在相邻周期被互相吞掉。
        """
        now_t = time.time()
        if self.events.missing and not self._prev_events_missing:
            if now_t - self._last_event_missing_log >= EVENT_FAIL_LOG_INTERVAL:
                self._last_event_missing_log = now_t
                self.logger.app("EVENT_SOURCE_MISSING source=local_event_file")
        elif not self.events.missing and self._prev_events_missing:
            if now_t - self._last_event_recovered_log >= EVENT_FAIL_LOG_INTERVAL:
                self._last_event_recovered_log = now_t
                self.logger.app("EVENT_SOURCE_RECOVERED source=local_event_file")
        self._prev_events_missing = self.events.missing

        if self.events.reset_reason and now_t - self._last_event_reset_log >= EVENT_FAIL_LOG_INTERVAL:
            self._last_event_reset_log = now_t
            self.logger.app(f"EVENT_CURSOR_RESET reason={self.events.reset_reason}")
        if (self.events.truncated_total > self._last_truncated_total
                and now_t - self._last_event_limit_log >= EVENT_FAIL_LOG_INTERVAL):
            self._last_event_limit_log = now_t
            self._last_truncated_total = self.events.truncated_total
            self.logger.app(f"EVENT_READ_LIMIT kind=lines_per_read total={self.events.truncated_total}")
        if (self.events.overlong > self._last_overlong_total
                and now_t - self._last_event_limit_log >= EVENT_FAIL_LOG_INTERVAL):
            self._last_event_limit_log = now_t
            self._last_overlong_total = self.events.overlong
            self.logger.app(f"EVENT_READ_LIMIT kind=overlong_line total={self.events.overlong}")

    def _process_events(self, ts):
        lines = self.events.read_new()
        self._log_event_state()
        rows = []
        for line in lines:
            parsed = parse_event_line(line)
            if parsed is None:
                self.event_failures += 1
                continue
            proto, ip, port = parsed
            rows.append(f"timestamp={ts} protocol={proto} remote_ip={ip} remote_port={port}")
        if rows:
            self.logger.text(f"events_{self.logger.today}.log", rows)
        if self.event_failures and (time.time() - self.last_event_fail_log >= EVENT_FAIL_LOG_INTERVAL):
            self.last_event_fail_log = time.time()
            self.logger.app(f"INVALID_RECORD event_parse_failures_total={self.event_failures}")

    def _write_snapshot(self, ts, active, valid_rows, up_rate, down_rate, now_t):
        """写文本快照; 聚合总量无效时速率写明确的 null, 不得写零冒充真实速率。"""
        if now_t - self.last_snap < self.snap_every:
            return
        self.last_snap = now_t
        top = sorted(valid_rows, key=lambda r: r["up_bytes"] + r["down_bytes"],
                     reverse=True)[:MAX_SNAPSHOT_LINES]
        if up_rate is None or down_rate is None:
            rate_part = "up_kbs=null down_kbs=null"
        else:
            rate_part = f"up_kbs={up_rate:.1f} down_kbs={down_rate:.1f}"
        lines = [f"timestamp={ts} active={active} {rate_part}"]
        for r in top:
            proto = r["protocol"] if r["protocol"] else "null"
            lines.append(f"remote={r['remote_ip']}:{r['remote_port']} "
                         f"up_bytes={r['up_bytes']} down_bytes={r['down_bytes']} "
                         f"protocol={proto}")
        self.logger.text(f"connections_{self.logger.today}.log", lines)

    def step(self):
        """执行一个采样周期。

        返回字典至少区分: source_ok / detail_written / aggregate_valid, 避免调用方
        把"部分成功"误判为完整成功。源不可用时返回 ok=False。
        """
        ts = now_iso()
        try:
            conns, d_total, u_total = self.api.snapshot()
        except DataLimitError as e:
            # 输入超过上限: 只记录类型与计数, 不写入原始内容 (T-06)。
            self.logger.app(f"DATA_LIMIT kind={e.kind}")
            return {"ok": False, "source_ok": False, "limit": e.kind}
        if conns is None:
            if not self.source_down:
                self.logger.app("SOURCE_UNAVAILABLE source=local_api")
                self.source_down = True
            return {"ok": False, "source_ok": False}

        if self.source_down:
            self.logger.app("SOURCE_RECOVERED source=local_api")
            self.source_down = False

        active = len(conns)
        valid_rows = []
        rejects = defaultdict(int)
        for rec in conns:
            row, reason = standardize_connection(rec, ts)
            if row is None:
                rejects[reason] += 1
            else:
                valid_rows.append(row)
        invalid_total = sum(rejects.values())
        if invalid_total:
            reasons = ",".join(f"{k}={v}" for k, v in sorted(rejects.items()))
            self.logger.app(f"INVALID_RECORD count={invalid_total} reasons={reasons}")

        # 有效连接明细不依赖聚合总量是否有效 (T-02): 先落盘明细。
        self.logger.jsonl_many(f"connections_detail_{self.logger.today}.jsonl", valid_rows)
        detail_written = bool(valid_rows)

        now_t = time.time()
        # 聚合总量非法时, 跳过伪造 CSV, 但明细、事件与带 null 速率的快照照常处理。
        d = to_nonneg_int(d_total)
        u = to_nonneg_int(u_total)
        if d is None or u is None:
            bad = "downloadTotal" if d is None else "uploadTotal"
            self.logger.app(f"INVALID_RECORD aggregate {bad} invalid, CSV skipped")
            self._write_snapshot(ts, active, valid_rows, None, None, now_t)
            self._process_events(ts)
            return {"ok": True, "source_ok": True, "detail_written": detail_written,
                    "aggregate_valid": False, "active": active, "valid": len(valid_rows),
                    "invalid": invalid_total, "up_rate": None, "down_rate": None}

        rates = self._update_rates(d_total, u_total, now_t)
        up_rate, down_rate = rates

        # CSV 周期汇总: 固定表头, 已有文件不重复写表头
        row_text = (f"{ts},{up_rate:.1f},{down_rate:.1f},{active},"
                    f"{len(valid_rows)},{invalid_total},{d_total},{u_total}")
        self.logger.csv(f"traffic_{self.logger.today}.csv", CSV_HEADER, row_text)

        # 文本快照: 按配置周期, 最多 40 条按总字节数排序的有效连接
        self._write_snapshot(ts, active, valid_rows, up_rate, down_rate, now_t)

        # 本地事件增量读取
        self._process_events(ts)

        return {"ok": True, "source_ok": True, "detail_written": detail_written,
                "aggregate_valid": True, "active": active, "valid": len(valid_rows),
                "invalid": invalid_total, "up_rate": up_rate, "down_rate": down_rate}

    def run_forever(self):
        """前台主循环: 成功按采样间隔, 失败按固定间隔重试。"""
        while True:
            stats = self.step()
            if stats.get("ok"):
                time.sleep(self.interval)
            else:
                time.sleep(RETRY_SLEEP)


# ---------------------------------------------------------------- 入口

def _safe_app_log(path, msg):
    """在不创建、不修改日志目录的前提下尝试追加程序日志; 失败静默。

    用于重复实例提示等场景, 避免为了写一条提示日志而创建/破坏日志目录。
    """
    try:
        d = os.path.dirname(path)
        if d and os.path.isdir(d):
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(f"[{now()}] {msg}\n")
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="traffic-logger 本地连接元数据监控")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--interval", type=float, default=None)
    args = ap.parse_args(argv)

    try:
        raw = load_config(args.config)
        cfg = validate_config(raw, interval_override=args.interval)
    except (OSError, ValueError) as e:
        print(f"[traffic-logger] 配置错误: 无法加载 {args.config} ({type(e).__name__})", file=sys.stderr)
        print("[traffic-logger] 建议: 检查文件是否存在且为合法 JSON", file=sys.stderr)
        return 2
    except ConfigError as e:
        print(f"[traffic-logger] 配置错误: {e}", file=sys.stderr)
        return 2

    # 生产启动校验: 占位密钥在开发配置中允许, 生产启动必须使用真实密钥。
    if is_placeholder_secret(cfg["api_secret"]):
        print("[traffic-logger] 配置错误: api.secret 仍为占位符, 生产启动必须设置真实密钥",
              file=sys.stderr)
        print("[traffic-logger] 建议: 修改 config.json 的 api.secret 为实际本地接口认证密钥",
              file=sys.stderr)
        return 2

    lock = SingleInstance()
    if not lock:
        # 重复实例: 只写提示日志, 不创建/锁定/破坏日志目录。
        print("[traffic-logger] INSTANCE_EXISTS 已有实例在运行, 本实例退出")
        _safe_app_log(os.path.join(cfg["log_dir"], "app.log"),
                      "INSTANCE_EXISTS second instance refused")
        return 0

    # 统一生命周期: 日志器/数据源/事件读取器/监控器初始化失败都返回非零码。
    try:
        logger = Logger(cfg["log_dir"], cfg["retention_days"])
        api = GatewayApi(cfg["api_url"], cfg["api_secret"])
        events = EventTail(cfg["event_path"])
        mon = Monitor(cfg, logger, api, events)
    except OSError as e:
        print(f"[traffic-logger] INIT_ERROR 初始化失败: {type(e).__name__}", file=sys.stderr)
        print("[traffic-logger] 建议: 检查 monitor.log_dir 是否可访问且为目录", file=sys.stderr)
        lock.close()
        return 1

    try:
        logger.app(f"STARTED interval={cfg['interval']:g}s snap_every={cfg['snap_every']:g}s "
                   f"retention_days={cfg['retention_days']} source=local_api")
        print(f"[traffic-logger] v{VERSION} started (interval={cfg['interval']:g}s)")
        mon.run_forever()
        logger.app("STOPPED normal exit")
        return 0
    except KeyboardInterrupt:
        logger.app("STOPPED user interrupt")
        print("\n[traffic-logger] stopped")
        return 0
    except OSError as e:
        try:
            logger.app(f"WRITE_ERROR kind={type(e).__name__}")
        except OSError:
            pass
        print(f"[traffic-logger] WRITE_ERROR 写盘失败: {type(e).__name__}", file=sys.stderr)
        return 1
    except Exception as e:
        try:
            logger.app(f"UNEXPECTED_ERROR kind={type(e).__name__}")
        except OSError:
            pass
        print(f"[traffic-logger] UNEXPECTED_ERROR {type(e).__name__}", file=sys.stderr)
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
