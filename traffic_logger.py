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
        if u.scheme not in ("http", "https"):
            raise ValueError("scheme")
        if not u.hostname or not u.port:
            raise ValueError("port")
        host = u.hostname
        if host == "localhost":
            return value
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            raise ConfigError(
                f"字段: {field} | 问题: 地址 '{host}' 不是本机回环地址 | 建议: 只允许 127.0.0.0/8 或 ::1")
        if not ip.is_loopback:
            raise ConfigError(
                f"字段: {field} | 问题: 地址 '{host}' 不是本机回环地址 | 建议: 只允许 127.0.0.0/8 或 ::1")
        return value
    except ConfigError:
        raise
    except Exception:
        raise ConfigError(
            f"字段: {field} | 问题: 协议仅允许 http/https, 且必须带端口 | 建议: 如 http://127.0.0.1:9097")


def _validate_float(value, field, minimum, default, hint):
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"字段: {field} | 问题: 必须是数值 | 建议: {hint}")
    value = float(value)
    if value < minimum:
        raise ConfigError(f"字段: {field} | 问题: 不得小于 {minimum} | 建议: {hint}")
    return value


def _validate_positive_int(value, field, default, hint):
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"字段: {field} | 问题: 必须是大于 0 的整数 | 建议: {hint}")
    return value


def validate_config(cfg, interval_override=None):
    """加载并校验配置, 返回归一化后的配置对象; 失败抛 ConfigError。"""
    if cfg is None:
        raise ConfigError("字段: 配置根 | 问题: 配置为空 | 建议: 提供 config.json")
    cfg = _require_dict(cfg, "配置根")
    api_cfg = _require_dict(cfg.get("api"), "api")
    mon_cfg = _require_dict(cfg.get("monitor"), "monitor")

    api_url = _validate_loopback_url(api_cfg.get("url"), "api.url")
    secret = api_cfg.get("secret")
    if not isinstance(secret, str):
        raise ConfigError("字段: api.secret | 问题: 必须是字符串 | 建议: 设置为本地接口认证密钥")

    interval = _validate_float(mon_cfg.get("interval_seconds"), "monitor.interval_seconds",
                               1, 2, "建议 2-60, 且不小于 1")
    snap_every = _validate_positive_int(mon_cfg.get("conn_snapshot_every"),
                                        "monitor.conn_snapshot_every", 30,
                                        "正整数, 不小于采样间隔")
    if interval_override is not None:
        interval = _validate_float(interval_override, "--interval", 1, interval,
                                   "建议 2-60, 且不小于 1")
    if snap_every < interval:
        raise ConfigError(
            "字段: monitor.conn_snapshot_every | 问题: 不得小于采样间隔 | "
            f"建议: 设置为 >= interval_seconds ({interval:g})")

    retention_days = _validate_positive_int(mon_cfg.get("log_retention_days"),
                                            "monitor.log_retention_days", 180,
                                            "正整数, 按适用制度配置并评估磁盘容量")

    event_path = mon_cfg.get("gateway_log_path")
    if not isinstance(event_path, str) or not event_path.strip():
        raise ConfigError("字段: monitor.gateway_log_path | 问题: 不能为空 | 建议: 填写本地事件文件路径")
    if "://" in event_path:
        raise ConfigError("字段: monitor.gateway_log_path | 问题: 只允许本地路径 | 建议: 填写本地文件路径")

    log_dir = mon_cfg.get("log_dir")
    if not isinstance(log_dir, str) or not log_dir.strip():
        raise ConfigError("字段: monitor.log_dir | 问题: 不能为空 | 建议: 填写日志目录, 默认 logs")

    cfg_obj = {
        "api_url": api_url,
        "api_secret": secret,
        "interval": interval,
        "snap_every": snap_every,
        "retention_days": retention_days,
        "event_path": expand_path(event_path, BASE_DIR),
        "log_dir": expand_path(log_dir, BASE_DIR),
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
        protocol = protocol.strip().upper()
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
        proto = m.group(1).upper()

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
        for fn in os.listdir(self.log_dir):
            fp = os.path.join(self.log_dir, fn)
            try:
                if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
                    removed += 1
            except OSError:
                continue  # 只读或占用文件跳过, 不阻断
        if removed:
            try:
                self.app(f"CLEANUP removed={removed} days={self.retention_days}")
            except OSError:
                pass

    def close(self):
        """无常驻句柄, 无需额外释放; 保留以兼容显式调用。"""


# ---------------------------------------------------------------- 数据源客户端

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
                return json.loads(r.read().decode("utf-8"))
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
    """本地事件文件偏移游标增量读取; 截断/轮转时重置游标。"""

    def __init__(self, path):
        self.path = path
        self.seen = 0

    def read_new(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []  # 文件不存在视为无新事件
        if size < self.seen:
            self.seen = 0  # 轮转或截断
        try:
            with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self.seen)
                lines = f.readlines()
                self.seen = f.tell()
        except OSError:
            return []
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

    def _process_events(self, ts):
        lines = self.events.read_new()
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

    def step(self):
        """执行一个采样周期, 返回统计字典; 源不可用时返回 ok=False。"""
        ts = now_iso()
        conns, d_total, u_total = self.api.snapshot()
        if conns is None:
            if not self.source_down:
                self.logger.app("SOURCE_UNAVAILABLE source=local_api")
                self.source_down = True
            return {"ok": False}

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

        now_t = time.time()
        rates = self._update_rates(d_total, u_total, now_t)
        if rates is None:
            self.logger.app("INVALID_RECORD aggregate totals invalid, CSV skipped")
            return {"ok": True, "active": active, "valid": len(valid_rows),
                    "invalid": invalid_total}
        up_rate, down_rate = rates

        # JSONL 连接明细: 每个有效连接每次采样一行
        self.logger.jsonl_many(f"connections_detail_{self.logger.today}.jsonl", valid_rows)

        # CSV 周期汇总: 固定表头, 已有文件不重复写表头
        row_text = (f"{ts},{up_rate:.1f},{down_rate:.1f},{active},"
                    f"{len(valid_rows)},{invalid_total},{d_total},{u_total}")
        self.logger.csv(f"traffic_{self.logger.today}.csv", CSV_HEADER, row_text)

        # 文本快照: 按配置周期, 最多 40 条按总字节数排序的有效连接
        if now_t - self.last_snap >= self.snap_every:
            self.last_snap = now_t
            top = sorted(valid_rows, key=lambda r: r["up_bytes"] + r["down_bytes"],
                         reverse=True)[:MAX_SNAPSHOT_LINES]
            lines = [f"timestamp={ts} active={active} up_kbs={up_rate:.1f} down_kbs={down_rate:.1f}"]
            for r in top:
                proto = r["protocol"] if r["protocol"] else "null"
                lines.append(f"remote={r['remote_ip']}:{r['remote_port']} "
                             f"up_bytes={r['up_bytes']} down_bytes={r['down_bytes']} "
                             f"protocol={proto}")
            self.logger.text(f"connections_{self.logger.today}.log", lines)

        # 本地事件增量读取
        self._process_events(ts)

        return {"ok": True, "active": active, "valid": len(valid_rows),
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

    lock = SingleInstance()
    if not lock:
        print("[traffic-logger] INSTANCE_EXISTS 已有实例在运行, 本实例退出")
        try:
            tmp = Logger(cfg["log_dir"], cfg["retention_days"])
            tmp.app("INSTANCE_EXISTS second instance refused")
            tmp.close()
        except OSError:
            pass
        lock.close()
        return 0

    logger = Logger(cfg["log_dir"], cfg["retention_days"])
    api = GatewayApi(cfg["api_url"], cfg["api_secret"])
    events = EventTail(cfg["event_path"])
    mon = Monitor(cfg, logger, api, events)

    try:
        logger.app(f"STARTED interval={cfg['interval']:g}s snap_every={cfg['snap_every']:g}s "
                   f"retention_days={cfg['retention_days']} source=local_api")
        print(f"[traffic-logger] v{VERSION} started (interval={cfg['interval']:g}s)")
        mon.run_forever()
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
