# -*- coding: utf-8 -*-
"""
traffic-logger
网络流量日志留存与审计工具 (连接级)

功能:
  - 逐连接记录: 时间 / 目标host / 端口 / 上传字节 / 下载字节 / 规则 / 出口路径 / 协议
  - 汇总记录:   每N秒 上下行速率 / 连接数 / TOP流量目标
  - 网关事件:   增量解析网关内核日志 (域名/规则/路径)
  - 日志轮转:   按日生成, 自动清理过期日志
  - 单实例锁:   防止重复启动

用法:
  python traffic_logger.py
  python traffic_logger.py --config config.json
  python traffic_logger.py --interval 5

依赖: 仅 Python 3 标准库
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")


# ---------------------------------------------------------------- utils

def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def expand_path(p, base):
    p = os.path.expandvars(p)
    if not os.path.isabs(p):
        p = os.path.join(base, p)
    return p


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 模块

class SingleInstance:
    """单实例锁: 绑定固定本地端口, 已占用则视为已有实例"""

    def __init__(self, port=48765):
        import socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.sock.bind(("127.0.0.1", port))
            self.sock.listen(1)
            self.ok = True
        except OSError:
            self.ok = False

    def __bool__(self):
        return self.ok


class Logger:
    """落盘日志器: 日期轮转 + 过期清理"""

    def __init__(self, log_dir, retention_days=30):
        self.log_dir = log_dir
        self.retention_days = retention_days
        os.makedirs(log_dir, exist_ok=True)
        self.today = datetime.date.today().strftime("%Y%m%d")
        self._app_fh = open(os.path.join(log_dir, "app.log"), "a", encoding="utf-8")

    def _check_rotate(self):
        nd = datetime.date.today().strftime("%Y%m%d")
        if nd != self.today:
            self.today = nd
            self._app_fh.close()
            self._app_fh = open(os.path.join(self.log_dir, "app.log"), "a", encoding="utf-8")
            self.cleanup()

    def _fh(self, name):
        self._check_rotate()
        return open(os.path.join(self.log_dir, name), "a", encoding="utf-8")

    def csv(self, name, header, row):
        p = os.path.join(self.log_dir, name)
        with self._fh(name) as f:
            if not os.path.exists(p) or os.path.getsize(p) == 0:
                f.write(header + "\n")
            f.write(row + "\n")

    def text(self, name, lines):
        with self._fh(name) as f:
            for ln in lines:
                f.write(ln + "\n")

    def jsonl(self, name, obj):
        with self._fh(name) as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def app(self, msg):
        self._check_rotate()
        self._app_fh.write(f"[{now()}] {msg}\n")
        self._app_fh.flush()

    def cleanup(self):
        cutoff = time.time() - self.retention_days * 86400
        for fn in os.listdir(self.log_dir):
            fp = os.path.join(self.log_dir, fn)
            try:
                if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
                    self.app(f"清理过期日志: {fn}")
            except Exception:
                pass

    def close(self):
        self._app_fh.close()


class GatewayApi:
    """本机网关服务本地管理接口客户端"""

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
        except Exception:
            return None

    def snapshot(self):
        """返回 (连接列表, 累计下载, 累计上传)"""
        data = self.get("/connections")
        if not data:
            return None, None, None
        return (data.get("connections", []),
                data.get("downloadTotal", 0),
                data.get("uploadTotal", 0))


class GatewayEventTail:
    """网关内核日志增量读取 (文件偏移游标)"""

    def __init__(self, path):
        self.path = expand_path(path, BASE_DIR)
        self.seen = 0

    def read_new(self):
        try:
            size = os.path.getsize(self.path)
            if size < self.seen:
                self.seen = 0  # 日志轮转
            with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self.seen)
                lines = f.readlines()
                self.seen = f.tell()
            return lines
        except Exception:
            return []


# ---------------------------------------------------------------- 主循环

def main():
    ap = argparse.ArgumentParser(description="traffic logger")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--interval", type=float, default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    api_cfg = cfg["api"]
    mon_cfg = cfg["monitor"]
    interval = args.interval or mon_cfg["interval_seconds"]
    snap_every = mon_cfg["conn_snapshot_every"]
    log_dir = expand_path(mon_cfg["log_dir"], BASE_DIR)

    lock = SingleInstance()
    if not lock:
        print(f"[{now()}] 已有实例在运行, 退出 (单实例锁)")
        sys.exit(0)

    logger = Logger(log_dir, mon_cfg["log_retention_days"])
    api = GatewayApi(api_cfg["url"], api_cfg["secret"])
    gateway = GatewayEventTail(mon_cfg["gateway_log_path"])

    logger.app(f"启动 | API={api_cfg['url']} | 间隔={interval}s | 日志={log_dir}")
    print(f"[{now()}] traffic-logger 启动 | API={api_cfg['url']} | 间隔={interval}s")

    prev_total = None
    last_snap = 0.0
    try:
        while True:
            conns, d_total, u_total = api.snapshot()
            if conns is None:
                logger.app("API 不可达, 5s 后重试")
                time.sleep(5)
                continue

            if prev_total:
                down_rate = (d_total - prev_total[0]) / interval / 1024
                up_rate = (u_total - prev_total[1]) / interval / 1024
            else:
                down_rate = up_rate = 0.0
            prev_total = (d_total, u_total)

            # 逐连接结构化 (JSONL, 始终写)
            agg = defaultdict(lambda: {"n": 0, "up": 0, "down": 0})
            for c in conns:
                m = c.get("metadata", {})
                host = (m.get("host") or m.get("remoteDestination")
                        or m.get("destinationIP") or "?")
                row = {
                    "ts": now(),
                    "host": host,
                    "port": m.get("destinationPort", "?"),
                    "up_bytes": c.get("upload", 0),
                    "down_bytes": c.get("download", 0),
                    "rule": c.get("rule", ""),
                    "node": (c.get("chains") or [""])[-1],
                    "type": m.get("type", ""),
                    "process": m.get("process", ""),
                }
                logger.jsonl(f"connections_detail_{logger.today}.jsonl", row)
                agg[host]["n"] += 1
                agg[host]["up"] += row["up_bytes"]
                agg[host]["down"] += row["down_bytes"]

            # 汇总 CSV
            top_up = sorted(agg.items(), key=lambda x: x[1]["up"], reverse=True)[:3]
            top_down = sorted(agg.items(), key=lambda x: x[1]["down"], reverse=True)[:3]
            logger.csv(
                f"traffic_{logger.today}.csv",
                "timestamp,sent_kbs,recv_kbs,conn_count,top_up,top_down",
                f"{now()},{up_rate:.1f},{down_rate:.1f},{len(conns)},"
                f"{'|'.join(f'{h}:{v[chr(117)+chr(112)]//1024}KB' for h, v in top_up)},"
                f"{'|'.join(f'{h}:{v[chr(100)+chr(111)+chr(119)+chr(110)]//1024}KB' for h, v in top_down)}")

            # 连接快照 (每 snap_every 秒)
            if time.time() - last_snap >= snap_every:
                last_snap = time.time()
                lines = [f"[{now()}] snapshot up={up_rate:.1f}KB/s down={down_rate:.1f}KB/s conns={len(conns)}"]
                for c in sorted(conns, key=lambda x: x.get("upload", 0) + x.get("download", 0), reverse=True)[:40]:
                    m = c.get("metadata", {})
                    host = m.get("host") or m.get("remoteDestination") or "?"
                    lines.append(
                        f"  {host}:{m.get('destinationPort', '?')}  "
                        f"↑{c.get('upload', 0)/1024:.1f}KB ↓{c.get('download', 0)/1024:.1f}KB  "
                        f"rule={c.get('rule', '')} node={(c.get('chains') or [''])[-1]} type={m.get('type', '')}")
                logger.text(f"connections_{logger.today}.log", lines)

            # 网关事件
            for line in gateway.read_new():
                m = re.search(
                    r'msg="\[(\w+)\]\s+\S+\s+-->\s+([\w.\-:]+):(\d+)\s+'
                    r'match\s+(\S+)\s+using\s+([^"]+)"', line)
                if m:
                    proto, host, port, rule, node = m.groups()
                    logger.text(f"events_{logger.today}.log",
                                [f"[{now()}] {proto} {host}:{port} rule={rule} node={node}"])

            # 控制台 (仅异常或启动, 避免刷屏; 每 30s 打一行状态)
            if int(time.time()) % 30 < interval:
                print(f"[{now()}] ↑{up_rate:.1f}KB/s ↓{down_rate:.1f}KB/s 连接={len(conns)} "
                      f"| 上传TOP: " + "; ".join(f"{h}({v['up']//1024}KB)" for h, v in top_up))

            time.sleep(interval)
    except KeyboardInterrupt:
        logger.app("手动停止")
        print(f"\n[{now()}] 已停止")
    except Exception as e:
        logger.app(f"异常退出: {e}")
        print(f"[{now()}] ERROR: {e}", file=sys.stderr)
    finally:
        logger.close()


if __name__ == "__main__":
    main()
