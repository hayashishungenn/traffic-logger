# -*- coding: utf-8 -*-
"""
traffic_logger Web Dashboard 模块 v2
本地可视化面板: http://127.0.0.1:8090

参考 Neko Master / shadcn-ui 设计语言: 深色 slate 主题, 卡片网格, KPI 大数字,
ECharts 面积图 + 时间范围切换 (30m/1h/24h/today), 连接明细表,
TOP 域名/节点/地区 三列总览 (ip-api 批量反查 + 服务端缓存)。

API:
  /api/live        实时速率 + 活跃连接 + TOP 域名/节点/地区 (查本机接口)
  /api/history     历史汇总 (读 logs/traffic_*.csv) ?range=30m|1h|24h|today
  /api/connections 最近连接明细 (读 logs/connections_detail_*.jsonl)
  /                前端页面

用法:
  python web_dashboard.py [--port 8090] [--config config.json]
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")

def _load_page():
    p = os.path.join(BASE_DIR, "web_apple.html")
    with open(p, "r", encoding="utf-8") as f:
        return f.read()

PAGE = _load_page()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "traffic-logger/2.0"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            if path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/live":
                self._api_live()
            elif path == "/api/history":
                self._api_history(qs.get("range", ["1h"])[0])
            elif path == "/api/connections":
                self._api_connections()
            else:
                self._send(404, b'{"error":"not found"}')
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}).encode("utf-8"))

    # ---- helpers ----

    def _log_dir(self):
        d = self.server.cfg["monitor"]["log_dir"]
        d = os.path.expandvars(d)
        if not os.path.isabs(d):
            d = os.path.join(os.path.dirname(self.server.config_path), d)
        return d

    @staticmethod
    def _agg_top(conns, key_fn, limit=8):
        """按 key_fn 聚合 {name,count,up,down}, 按总流量降序取 TOP limit."""
        agg = {}
        for c in conns:
            key = key_fn(c) or "?"
            e = agg.get(key)
            if e is None:
                e = agg[key] = {"name": key, "count": 0, "up": 0, "down": 0}
            e["count"] += 1
            e["up"] += c.get("upload", 0) or 0
            e["down"] += c.get("download", 0) or 0
        return sorted(agg.values(), key=lambda x: x["up"] + x["down"], reverse=True)[:limit]

    def _geo_lookup(self, ips):
        """目标 IP -> 国家。ip-api.com 免费批量接口 (POST, 每批最多 100 个)。

        服务端缓存: srv._geo_cache {ip: (country, ts)}, 有效期 1 小时;
        仅对传入的前 20 个 IP 查询以控制请求量。
        返回 (mapping, failed): mapping 为 {ip: country};
        failed=True 表示本轮批量请求失败且无缓存可回退。
        """
        srv = self.server
        cache = srv._geo_cache
        now = time.time()
        out = {}
        need = []
        for ip in ips:
            hit = cache.get(ip)
            if hit and now - hit[1] < 3600:
                out[ip] = hit[0]
            else:
                need.append(ip)
        if not need:
            return out, False
        try:
            body = json.dumps(need).encode("utf-8")
            req = urllib.request.Request(
                "http://ip-api.com/batch?fields=query,country,status",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                res = json.loads(r.read().decode("utf-8"))
            for it in res:
                if it.get("status") == "success" and it.get("query"):
                    country = it.get("country") or "未知"
                    cache[it["query"]] = (country, now)
                    out[it["query"]] = country
            return out, False
        except Exception:
            # ip-api 不可用: 静默降级, 仅返回已有缓存
            return out, True

    # ---- API ----

    def _api_live(self):
        cfg = self.server.cfg
        url = cfg["api"]["url"].rstrip("/") + "/connections"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {cfg['api']['secret']}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        conns = data.get("connections", [])
        total_d, total_u = data.get("downloadTotal", 0), data.get("uploadTotal", 0)
        srv = self.server
        now_t = time.time()
        up_kbs = down_kbs = 0.0
        if srv._prev_total is not None:
            elapsed = max(now_t - srv._prev_ts, 0.001)
            dd, ud = total_d - srv._prev_total[0], total_u - srv._prev_total[1]
            if dd >= 0: down_kbs = dd / elapsed / 1024
            if ud >= 0: up_kbs = ud / elapsed / 1024
        srv._prev_total = (total_d, total_u)
        srv._prev_ts = now_t
        rows = []
        for c in conns:
            m = c.get("metadata", {})
            rows.append({
                "host": m.get("host") or m.get("remoteDestination") or m.get("destinationIP") or "?",
                "port": m.get("destinationPort", "?"),
                "up": c.get("upload", 0), "down": c.get("download", 0),
                "rule": c.get("rule", ""), "node": (c.get("chains") or [""])[-1],
                "type": m.get("type", ""),
            })
        rows.sort(key=lambda x: x["up"] + x["down"], reverse=True)

        # ---- ① TOP 域名: 按 host 聚合 (计数+上传+下载), 取 TOP8 ----
        domains = self._agg_top(conns, lambda c: (c.get("metadata", {}) or {}).get("host")
                                or (c.get("metadata", {}) or {}).get("remoteDestination")
                                or (c.get("metadata", {}) or {}).get("destinationIP") or "?")

        # ---- ② TOP 节点: 按出口节点 (chains 最后一项) 聚合 ----
        nodes = self._agg_top(conns, lambda c: (c.get("chains") or [""])[-1] or "?")

        # ---- ③ TOP 地区: 目标 IP -> 国家 (ip-api 批量反查, 服务端 1h 缓存, 每轮最多 20 个 IP) ----
        dest_ips = []
        for c in conns:
            ip = (c.get("metadata", {}) or {}).get("destinationIP")
            if ip and ip not in dest_ips:
                dest_ips.append(ip)
        geo, geo_failed = self._geo_lookup(dest_ips[:20])
        countries = []
        if geo or not geo_failed:
            cagg = {}
            for c in conns:
                ip = (c.get("metadata", {}) or {}).get("destinationIP")
                key = geo.get(ip) or "未知"
                e = cagg.get(key)
                if e is None:
                    e = cagg[key] = {"name": key, "count": 0, "up": 0, "down": 0}
                e["count"] += 1
                e["up"] += c.get("upload", 0) or 0
                e["down"] += c.get("download", 0) or 0
            countries = sorted(cagg.values(), key=lambda x: x["up"] + x["down"], reverse=True)[:8]

        self._send(200, json.dumps({
            "up_kbs": round(up_kbs, 1), "down_kbs": round(down_kbs, 1),
            "active": len(conns), "total_up": total_u, "total_down": total_d,
            "conns": rows[:60],
            "domains": domains, "nodes": nodes, "countries": countries,
        }).encode("utf-8"))

    def _api_history(self, range_key):
        log_dir = self._log_dir()
        files = sorted(f for f in os.listdir(log_dir) if re.match(r"^traffic_\d{8}\.csv$", f))
        rows = []
        if range_key == "today":
            # 当日 00:00 至今: 动态计算本地时区当天起始时间
            now_dt = datetime.datetime.now()
            cutoff = datetime.datetime(now_dt.year, now_dt.month, now_dt.day).timestamp()
        else:
            minutes = {"30m": 30, "1h": 60, "24h": 1440}.get(range_key, 60)
            cutoff = time.time() - minutes * 60
        for fn in files:
            p = os.path.join(log_dir, fn)
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.strip().split(",")
                    if len(parts) < 3 or parts[0] in ("timestamp",):
                        continue
                    try:
                        ts = parts[0]
                        # 时间戳可能是 "2026-08-15T18:30:40+08:00" 或 "2026-08-15 18:30:40"
                        norm = ts.replace("T", " ")
                        dt = datetime.datetime.fromisoformat(norm)
                        if dt.timestamp() < cutoff:
                            continue
                        rows.append({"t": ts, "up": float(parts[1]), "down": float(parts[2]),
                                     "active": int(parts[3]) if len(parts) > 3 else 0})
                    except (ValueError, IndexError):
                        continue
        # 长范围数据抽稀: 超过 1500 点则按步长采样
        if len(rows) > 1500:
            step = len(rows) // 1500
            rows = rows[::step]
        self._send(200, json.dumps(rows).encode("utf-8"))

    def _api_connections(self):
        log_dir = self._log_dir()
        files = sorted(f for f in os.listdir(log_dir) if re.match(r"^connections_detail_\d{8}\.jsonl$", f))
        rows = []
        if files:
            p = os.path.join(log_dir, files[-1])
            size = os.path.getsize(p)
            tail = 512 * 1024
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                if size > tail:
                    f.seek(size - tail)
                    f.readline()
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        self._send(200, json.dumps(rows[-500:]).encode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="traffic-logger web dashboard")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    srv = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    srv.cfg = cfg
    srv.config_path = args.config
    srv._prev_total = None
    srv._prev_ts = 0.0
    srv._geo_cache = {}  # {ip: (country, ts)}, ip-api 1 小时缓存
    print(f"[traffic-logger] dashboard v2 running at http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.server_close()


if __name__ == "__main__":
    main()
