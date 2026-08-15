# -*- coding: utf-8 -*-
"""traffic-logger 自动化测试: 数据契约与失败路径 (TASK_BOOK.md 第 9 节)。

运行:
  python -m unittest discover -s tests -v
  (或从项目根目录)  python -m unittest tests.test_traffic_logger -v
"""
import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import traffic_logger as tl

# ---------------------------------------------------------------- 共享数据

VALID_IPV4 = {
    "id": "c1",
    "metadata": {
        "network": "tcp",
        "destinationIP": "203.0.113.10",
        "destinationPort": "443",
        "sourceIP": "127.0.0.1",
        "sourcePort": "50001",
    },
    "upload": 1024,
    "download": 2048,
}

VALID_IPV6 = {
    "id": "c2",
    "metadata": {
        "network": "udp",
        "destinationIP": "2001:db8::1",
        "destinationPort": 53,
        "sourceIP": "::1",
        "sourcePort": 50002,
    },
    "upload": 512,
    "download": 0,
}

DOMAIN_RECORD = {
    "id": "c3",
    "metadata": {
        "network": "tcp",
        "host": "example.com",
        "destinationIP": "",
        "remoteDestination": "",
        "destinationPort": "443",
    },
    "upload": 10,
    "download": 20,
}

INVALID_PORT_RECORD = {
    "id": "c4",
    "metadata": {
        "network": "tcp",
        "destinationIP": "203.0.113.20",
        "destinationPort": "99999",
        "sourceIP": "127.0.0.1",
        "sourcePort": "50003",
    },
    "upload": 1,
    "download": 2,
}

SAMPLE_CONNECTIONS = [VALID_IPV4, VALID_IPV6, DOMAIN_RECORD, INVALID_PORT_RECORD]


def make_config(log_dir, api_url="http://127.0.0.1:9097", **overrides):
    """构造一份合法的配置字典; overrides 覆盖 monitor/api 子键。

    默认 secret 使用非占位测试密钥, 避免 main() 的生产占位符校验拒绝它。
    """
    cfg = {
        "api": {"url": api_url, "secret": "test-local-secret-abc"},
        "monitor": {
            "interval_seconds": 1,
            "conn_snapshot_every": 30,
            "log_retention_days": 180,
            "gateway_log_path": os.path.join(log_dir, "events_source.log"),
            "log_dir": log_dir,
        },
    }
    mon = cfg["monitor"]
    mon.update(overrides.get("monitor", {}))
    api = cfg["api"]
    api.update(overrides.get("api", {}))
    return cfg


# ---------------------------------------------------------------- 配置

class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tl_cfg_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_valid_config_passes(self):
        cfg = tl.validate_config(make_config(self.dir))
        self.assertEqual(cfg["interval"], 1.0)
        self.assertEqual(cfg["snap_every"], 30)
        self.assertEqual(cfg["retention_days"], 180)
        self.assertTrue(os.path.isabs(cfg["log_dir"]))
        self.assertTrue(os.path.isabs(cfg["event_path"]))

    def test_missing_api_section_fails(self):
        with self.assertRaises(tl.ConfigError) as cm:
            tl.validate_config({"monitor": {}})
        self.assertIn("api", str(cm.exception))

    def test_missing_monitor_section_fails(self):
        with self.assertRaises(tl.ConfigError):
            tl.validate_config({"api": {"url": "http://127.0.0.1:1", "secret": "x"}})

    def test_remote_url_rejected(self):
        for url in ("http://example.com:9097", "http://8.8.8.8:9097",
                    "http://192.168.1.10:9097", "http://[2001:db8::1]:9097"):
            with self.assertRaises(tl.ConfigError) as cm:
                tl.validate_config(make_config(self.dir, api_url=url))
            self.assertIn("api.url", str(cm.exception))

    def test_loopback_forms_accepted(self):
        for url in ("http://127.0.0.1:9097", "http://localhost:9097", "https://[::1]:9097"):
            cfg = tl.validate_config(make_config(self.dir, api_url=url))
            self.assertEqual(cfg["api_url"], url)

    def test_empty_url_rejected(self):
        with self.assertRaises(tl.ConfigError):
            tl.validate_config(make_config(self.dir, api={"url": ""}))

    def test_non_numeric_secret_rejected_without_value(self):
        with self.assertRaises(tl.ConfigError) as cm:
            tl.validate_config(make_config(self.dir, api={"secret": 123}))
        msg = str(cm.exception)
        self.assertIn("api.secret", msg)
        self.assertNotIn("123", msg)  # 错误信息不泄露值

    def test_interval_below_minimum_rejected(self):
        with self.assertRaises(tl.ConfigError):
            tl.validate_config(make_config(self.dir, monitor={"interval_seconds": 0.5}))

    def test_snap_every_below_interval_rejected(self):
        with self.assertRaises(tl.ConfigError) as cm:
            tl.validate_config(make_config(self.dir, monitor={"interval_seconds": 10,
                                                              "conn_snapshot_every": 5}))
        self.assertIn("conn_snapshot_every", str(cm.exception))

    def test_snap_every_non_integer_rejected(self):
        with self.assertRaises(tl.ConfigError):
            tl.validate_config(make_config(self.dir, monitor={"conn_snapshot_every": 2.5}))

    def test_retention_non_positive_rejected(self):
        with self.assertRaises(tl.ConfigError):
            tl.validate_config(make_config(self.dir, monitor={"log_retention_days": 0}))

    def test_event_path_url_rejected(self):
        with self.assertRaises(tl.ConfigError) as cm:
            tl.validate_config(make_config(self.dir,
                                           monitor={"gateway_log_path": "http://x/y"}))
        self.assertIn("gateway_log_path", str(cm.exception))

    def test_unc_event_path_rejected(self):
        for p in ("\\\\server\\share\\events.log", "\\\\?\\C:\\events.log",
                  "//remote/share/events.log"):
            with self.assertRaises(tl.ConfigError) as cm:
                tl.validate_config(make_config(self.dir,
                                               monitor={"gateway_log_path": p}))
            self.assertIn("gateway_log_path", str(cm.exception))

    def test_unc_log_dir_rejected(self):
        for p in ("\\\\server\\share\\logs", "\\\\?\\C:\\logs", "\\\\.\\C:\\logs",
                  "//remote/share/logs", "https://example.com/logs"):
            with self.assertRaises(tl.ConfigError) as cm:
                tl.validate_config(make_config(self.dir, monitor={"log_dir": p}))
            self.assertIn("log_dir", str(cm.exception))

    def test_reserved_device_name_log_dir_rejected(self):
        with self.assertRaises(tl.ConfigError) as cm:
            tl.validate_config(make_config(self.dir, monitor={"log_dir": "CON"}))
        self.assertIn("log_dir", str(cm.exception))

    def test_interval_override_validated(self):
        with self.assertRaises(tl.ConfigError):
            tl.validate_config(make_config(self.dir), interval_override=0.2)

    def test_empty_secret_rejected(self):
        for s in ("", "   "):
            with self.assertRaises(tl.ConfigError) as cm:
                tl.validate_config(make_config(self.dir, api={"secret": s}))
            self.assertIn("api.secret", str(cm.exception))
            self.assertNotIn(repr(s), str(cm.exception))

    def test_non_finite_interval_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf"), 86401.0):
            with self.assertRaises(tl.ConfigError) as cm:
                tl.validate_config(make_config(self.dir,
                                               monitor={"interval_seconds": bad}))
            self.assertIn("interval_seconds", str(cm.exception))

    def test_url_path_userinfo_query_rejected(self):
        for url in ("http://user:pass@127.0.0.1:9097",
                    "http://127.0.0.1:9097/connections",
                    "http://127.0.0.1:9097?q=1",
                    "http://127.0.0.1:9097#frag",
                    "http://127.0.0.1:0",
                    "http://127.0.0.1:99999"):
            with self.assertRaises(tl.ConfigError) as cm:
                tl.validate_config(make_config(self.dir, api_url=url))
            self.assertIn("api.url", str(cm.exception))

    def test_localhost_url_accepted_and_documented(self):
        # localhost 只在本机解析为回环地址 (127.0.0.1 / ::1), 不用于任何远程解析
        cfg = tl.validate_config(make_config(self.dir, api_url="http://localhost:9097"))
        self.assertEqual(cfg["api_url"], "http://localhost:9097")

    def test_missing_config_file_fails_load(self):
        with self.assertRaises(OSError):
            tl.load_config(os.path.join(self.dir, "nope.json"))

    def test_bad_json_fails_load(self):
        p = os.path.join(self.dir, "bad.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(ValueError):
            tl.load_config(p)


# ---------------------------------------------------------------- IP 校验

class IpTests(unittest.TestCase):
    def test_valid_ipv4(self):
        self.assertTrue(tl.is_valid_ip("203.0.113.10"))
        self.assertTrue(tl.is_valid_ip("127.0.0.1"))

    def test_valid_ipv6(self):
        self.assertTrue(tl.is_valid_ip("2001:db8::1"))
        self.assertTrue(tl.is_valid_ip("::1"))

    def test_domain_rejected(self):
        self.assertFalse(tl.is_valid_ip("example.com"))
        self.assertFalse(tl.is_valid_ip("copilot.tencent.com"))

    def test_empty_and_none_rejected(self):
        self.assertFalse(tl.is_valid_ip(""))
        self.assertFalse(tl.is_valid_ip("  "))
        self.assertFalse(tl.is_valid_ip(None))

    def test_illegal_chars_rejected(self):
        self.assertFalse(tl.is_valid_ip("203.0.113.10:443"))
        self.assertFalse(tl.is_valid_ip("256.1.1.1"))
        self.assertFalse(tl.is_valid_ip("1.2.3"))
        self.assertFalse(tl.is_valid_ip(203001133))


# ---------------------------------------------------------------- 端口校验

class PortTests(unittest.TestCase):
    def test_valid_ports(self):
        self.assertEqual(tl.parse_port(1), 1)
        self.assertEqual(tl.parse_port(443), 443)
        self.assertEqual(tl.parse_port(65535), 65535)
        self.assertEqual(tl.parse_port("443"), 443)

    def test_out_of_range(self):
        self.assertIsNone(tl.parse_port(0))
        self.assertIsNone(tl.parse_port(65536))
        self.assertIsNone(tl.parse_port(-1))

    def test_wrong_types(self):
        self.assertIsNone(tl.parse_port("abc"))
        self.assertIsNone(tl.parse_port("4.5"))
        self.assertIsNone(tl.parse_port(None))
        self.assertIsNone(tl.parse_port(True))
        self.assertIsNone(tl.parse_port(443.0))


# ---------------------------------------------------------------- 字节校验

class BytesTests(unittest.TestCase):
    def test_valid_bytes(self):
        self.assertEqual(tl.to_nonneg_int(0), 0)
        self.assertEqual(tl.to_nonneg_int(1024), 1024)
        self.assertEqual(tl.to_nonneg_int("512"), 512)

    def test_invalid_bytes(self):
        self.assertIsNone(tl.to_nonneg_int(-1))
        self.assertIsNone(tl.to_nonneg_int(1.5))
        self.assertIsNone(tl.to_nonneg_int(None))
        self.assertIsNone(tl.to_nonneg_int("abc"))
        self.assertIsNone(tl.to_nonneg_int(True))

    def test_counter_reset_rebaselines(self):
        logger = mock.Mock(spec=tl.Logger)
        mon = tl.Monitor({"interval": 1.0, "snap_every": 30}, logger, None, None)
        self.assertEqual(mon._update_rates(1000, 2000, 0.0), (0.0, 0.0))   # 基线
        # delta=1000 字节 / 1 秒 / 1024 = 0.9765625 KB/s
        self.assertEqual(mon._update_rates(2000, 3000, 1.0), (1000 / 1024, 1000 / 1024))
        r = mon._update_rates(500, 3000, 2.0)                              # 回退
        self.assertEqual(r, (0.0, 0.0))
        logger.app.assert_called()  # COUNTER_RESET 已记录
        self.assertTrue(any("COUNTER_RESET" in c[0][0] for c in logger.app.call_args_list))


# ---------------------------------------------------------------- 时间

class TimeTests(unittest.TestCase):
    def test_now_iso_has_tz_offset(self):
        ts = tl.now_iso()
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

    def test_date_str_format(self):
        self.assertRegex(tl.date_str(), r"^\d{8}$")


# ---------------------------------------------------------------- 标准化

class StandardizeTests(unittest.TestCase):
    def test_ipv4_record(self):
        row, reason = tl.standardize_connection(VALID_IPV4, "2026-08-15T10:20:30+08:00")
        self.assertIsNone(reason)
        self.assertEqual(row["remote_ip"], "203.0.113.10")
        self.assertEqual(row["remote_port"], 443)
        self.assertEqual(row["local_ip"], "127.0.0.1")
        self.assertEqual(row["local_port"], 50001)
        self.assertEqual(row["protocol"], "TCP")
        self.assertEqual(row["up_bytes"], 1024)
        self.assertEqual(row["down_bytes"], 2048)
        self.assertEqual(row["source"], "local_api")
        self.assertNotIn("host", row)

    def test_ipv6_record(self):
        row, reason = tl.standardize_connection(VALID_IPV6, "2026-08-15T10:20:30+08:00")
        self.assertIsNone(reason)
        self.assertEqual(row["remote_ip"], "2001:db8::1")
        self.assertEqual(row["remote_port"], 53)
        self.assertEqual(row["protocol"], "UDP")

    def test_domain_rejected(self):
        row, reason = tl.standardize_connection(DOMAIN_RECORD, "2026-08-15T10:20:30+08:00")
        self.assertIsNone(row)
        self.assertEqual(reason, "invalid_remote_ip")

    def test_invalid_port_rejected(self):
        row, reason = tl.standardize_connection(INVALID_PORT_RECORD, "2026-08-15T10:20:30+08:00")
        self.assertIsNone(row)
        self.assertEqual(reason, "invalid_remote_port")

    def test_negative_bytes_rejected(self):
        rec = json.loads(json.dumps(VALID_IPV4))
        rec["upload"] = -5
        row, reason = tl.standardize_connection(rec, "t")
        self.assertIsNone(row)
        self.assertEqual(reason, "invalid_up_bytes")

    def test_missing_local_endpoint_uses_null(self):
        rec = json.loads(json.dumps(VALID_IPV4))
        del rec["metadata"]["sourceIP"]
        del rec["metadata"]["sourcePort"]
        row, reason = tl.standardize_connection(rec, "t")
        self.assertIsNone(reason)
        self.assertIsNone(row["local_ip"])
        self.assertIsNone(row["local_port"])

    def test_missing_protocol_uses_null(self):
        rec = json.loads(json.dumps(VALID_IPV4))
        rec["metadata"]["network"] = ""
        row, reason = tl.standardize_connection(rec, "t")
        self.assertIsNone(reason)
        self.assertIsNone(row["protocol"])

    def test_non_object_rejected(self):
        row, reason = tl.standardize_connection(["not", "dict"], "t")
        self.assertIsNone(row)
        self.assertEqual(reason, "not_object")


# ---------------------------------------------------------------- 事件解析

class EventParseTests(unittest.TestCase):
    def test_arrow_target(self):
        self.assertEqual(tl.parse_event_line('msg="[TCP] 203.0.113.10:443 ..."'),
                         ("TCP", "203.0.113.10", 443))

    def test_ipv6_target(self):
        self.assertEqual(tl.parse_event_line("[UDP] 2001:db8::1:53"),
                         ("UDP", "2001:db8::1", 53))

    def test_bracketed_ipv6(self):
        self.assertEqual(tl.parse_event_line("--> [2001:db8::1]:443"),
                         (None, "2001:db8::1", 443))

    def test_domain_not_recorded(self):
        self.assertIsNone(tl.parse_event_line("[TCP] example.com:443"))

    def test_bad_port_not_recorded(self):
        self.assertIsNone(tl.parse_event_line("[TCP] 203.0.113.10:99999"))

    def test_garbage_not_recorded(self):
        self.assertIsNone(tl.parse_event_line("no useful tokens here"))


class EventTailTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tl_evt_")
        self.path = os.path.join(self.dir, "source.log")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, text):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(text)

    def test_incremental_read_no_repeat(self):
        self._write("line1\n")
        tail = tl.EventTail(self.path)
        self.assertEqual(tail.read_new(), ["line1\n"])
        self.assertEqual(tail.read_new(), [])          # 不重复
        self._write("line2\n")
        self.assertEqual(tail.read_new(), ["line2\n"])

    def test_truncation_resets_cursor(self):
        self._write("AAAAA\nBBBBB\n")
        tail = tl.EventTail(self.path)
        self.assertEqual(tail.read_new(), ["AAAAA\n", "BBBBB\n"])
        with open(self.path, "w", encoding="utf-8") as f:   # 截断
            f.write("CCCCC\n")
        self.assertEqual(tail.read_new(), ["CCCCC\n"])      # 游标重置

    def test_missing_file_returns_empty(self):
        tail = tl.EventTail(os.path.join(self.dir, "missing.log"))
        self.assertEqual(tail.read_new(), [])

    def test_same_size_event_replacement_resets_cursor(self):
        # 同大小文件替换: 内容不同但字节数相同, 必须识别并从头重读 (T-03)
        self._write("AAAAA\nBBBBB\n")
        tail = tl.EventTail(self.path)
        self.assertEqual(tail.read_new(), ["AAAAA\n", "BBBBB\n"])
        tmp = self.path + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("CCCCC\nDDDDD\n")
        os.replace(tmp, self.path)          # 替换为同大小新文件
        self.assertEqual(tail.read_new(), ["CCCCC\n", "DDDDD\n"])
        self.assertEqual(tail.reset_reason, "replaced")
        self.assertEqual(tail.read_new(), [])   # 不重复

    def test_missing_then_recovered_reads_from_zero(self):
        tail = tl.EventTail(self.path)
        self._write("AAAAA\n")
        self.assertEqual(tail.read_new(), ["AAAAA\n"])
        os.remove(self.path)
        self.assertEqual(tail.read_new(), [])
        self.assertTrue(tail.missing)
        self._write("BBBBB\n")
        self.assertEqual(tail.read_new(), ["BBBBB\n"])   # 恢复后从头读取
        self.assertFalse(tail.missing)

    def test_event_read_limit_counts(self):
        # 积压超过单次行数上限: 分次读取, 有明确计数且不丢行、不吞首字符
        self._write("l1\nl2\nl3\n")
        tail = tl.EventTail(self.path, max_lines=2, max_line_len=100)
        self.assertEqual(tail.read_new(), ["l1\n", "l2\n"])
        self.assertTrue(tail.truncated)
        self.assertEqual(tail.truncated_total, 1)
        self.assertEqual(tail.read_new(), ["l3\n"])
        self.assertFalse(tail.truncated)
        self.assertEqual(tail.truncated_total, 1)

    def test_event_overlong_line_discarded_and_counted(self):
        # 超长行整行丢弃并计数, 不产生截断后看似有效的字段, 后续行正常读取
        tail = tl.EventTail(self.path, max_lines=10, max_line_len=64)
        self._write("X" * 100 + "\n[TCP] 203.0.113.7:53\n")
        lines = tail.read_new()
        self.assertEqual(lines, ["[TCP] 203.0.113.7:53\n"])
        self.assertEqual(tail.overlong, 1)

    def test_event_source_missing_recovered_logged(self):
        tail = tl.EventTail(self.path)
        mon_logger = mock.Mock(spec=tl.Logger)
        mon = tl.Monitor({"interval": 1.0, "snap_every": 30}, mon_logger, None, tail)
        self._write("[TCP] 203.0.113.11:443\n")
        mon._process_events("t")
        self.assertFalse(any("EVENT_SOURCE_MISSING" in c[0][0]
                             for c in mon_logger.app.call_args_list))
        os.remove(self.path)
        mon._process_events("t")
        self.assertTrue(any("EVENT_SOURCE_MISSING" in c[0][0]
                            for c in mon_logger.app.call_args_list))
        self._write("[TCP] 203.0.113.12:443\n")
        mon._process_events("t")
        self.assertTrue(any("EVENT_SOURCE_RECOVERED" in c[0][0]
                            for c in mon_logger.app.call_args_list))
        saved_lines = [c[0][1][0] for c in mon_logger.text.call_args_list]
        self.assertTrue(any("203.0.113.12" in ln for ln in saved_lines))

    def test_bad_lines_not_saved(self):
        tail = tl.EventTail(self.path)
        mon_logger = mock.Mock(spec=tl.Logger)
        mon = tl.Monitor({"interval": 1.0, "snap_every": 30}, mon_logger, None, tail)
        self._write("[TCP] bad:443\ngarbage\n[TCP] 203.0.113.10:53\n")
        mon._process_events("2026-08-15T10:20:30+08:00")
        saved_lines = [c[0][1][0] for c in mon_logger.text.call_args_list]
        self.assertEqual(len(saved_lines), 1)          # 只写入一条有效记录
        self.assertIn("203.0.113.10", saved_lines[0])
        self.assertNotIn("bad", saved_lines[0])
        # 解析失败已计数, 且限频记录在程序日志
        self.assertEqual(mon.event_failures, 2)
        self.assertTrue(any("event_parse_failures" in c[0][0]
                            for c in mon_logger.app.call_args_list))


# ---------------------------------------------------------------- 日志模块

class LoggerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tl_log_")
        self.logger = tl.Logger(self.dir, retention_days=2)

    def tearDown(self):
        self.logger.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_csv_header_written_once(self):
        self.logger.csv("t.csv", "h1,h2", "1,2")
        self.logger.csv("t.csv", "h1,h2", "3,4")
        with open(os.path.join(self.dir, "t.csv"), encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content.count("h1,h2"), 1)
        self.assertEqual(len(content.splitlines()), 3)   # 表头 + 2 行

    def test_cross_day_switches_filename(self):
        # 与 Monitor 一致: 用 logger.today 拼出带日期的文件名
        with mock.patch("traffic_logger.date_str", return_value="20260101"):
            self.logger.jsonl_many(
                f"connections_detail_{self.logger.today}.jsonl", [{"a": 1}])
        with mock.patch("traffic_logger.date_str", return_value="20260102"):
            self.logger.jsonl_many(
                f"connections_detail_{self.logger.today}.jsonl", [{"a": 2}])
        self.assertTrue(os.path.exists(
            os.path.join(self.dir, "connections_detail_20260101.jsonl")))
        self.assertTrue(os.path.exists(
            os.path.join(self.dir, "connections_detail_20260102.jsonl")))

    def test_cleanup_removes_only_expired_files(self):
        old = os.path.join(self.dir, "traffic_20200101.csv")
        with open(old, "w", encoding="utf-8") as f:
            f.write("x\n")
        os.utime(old, (time.time() - 10 * 86400, time.time() - 10 * 86400))
        fresh = os.path.join(self.dir, "app.log")
        with open(fresh, "w", encoding="utf-8") as f:
            f.write("x\n")
        self.logger.cleanup()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_cleanup_does_not_touch_subdirectories(self):
        sub = os.path.join(self.dir, "user_data")
        os.makedirs(sub)
        nested = os.path.join(sub, "old.txt")
        with open(nested, "w", encoding="utf-8") as f:
            f.write("keep\n")
        os.utime(nested, (time.time() - 10 * 86400, time.time() - 10 * 86400))
        self.logger.cleanup()
        self.assertTrue(os.path.exists(nested))   # 不越权删除子目录内容

    def test_cleanup_skips_readonly_file_without_crash(self):
        ro = os.path.join(self.dir, "traffic_20200102.csv")
        with open(ro, "w", encoding="utf-8") as f:
            f.write("x\n")
        os.utime(ro, (time.time() - 10 * 86400, time.time() - 10 * 86400))
        if os.name == "nt":
            os.chmod(ro, 0o444)   # 只读: Windows 上删除会失败
        self.logger.cleanup()     # 不得抛异常
        if os.name == "nt":
            self.assertTrue(os.path.exists(ro))   # 只读文件被跳过, 不清除
        os.chmod(ro, 0o666)

    def test_cleanup_preserves_expired_unknown_file(self):
        # 过期的程序日志被删除; 同目录下过期的未知文件必须保留 (T-01 P0)
        old = time.time() - 10 * 86400
        program = os.path.join(self.dir, "traffic_20200101.csv")
        with open(program, "w", encoding="utf-8") as f:
            f.write("x\n")
        os.utime(program, (old, old))
        for name in ("important.txt", "config.json", "README.md"):
            p = os.path.join(self.dir, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write("keep\n")
            os.utime(p, (old, old))
        self.logger.cleanup()
        self.assertFalse(os.path.exists(program), "过期程序日志应被清理")
        for name in ("important.txt", "config.json", "README.md"):
            self.assertTrue(os.path.exists(os.path.join(self.dir, name)),
                            f"未知文件 {name} 不应被清理删除")

    def test_program_filename_whitelist(self):
        for good in ("app.log", "traffic_20260101.csv", "connections_20260101.log",
                     "connections_detail_20260101.jsonl", "events_20260101.log",
                     "traffic_logger_tmp_1.tmp"):
            self.assertTrue(tl.is_program_log_filename(good), good)
        for bad in ("important.txt", "config.json", "README.md", "app.txt",
                    "traffic_202601.csv", "traffic_20260101", "traffic_abc.csv",
                    "user_data", "connections_detail.log", ".hidden",
                    "app.log.bak", "traffic_20260101.csv.evil", "logs"):
            self.assertFalse(tl.is_program_log_filename(bad), bad)

    def test_write_failure_raises(self):
        bad = os.path.join(self.dir, "blocked")
        os.makedirs(bad)
        with open(os.path.join(bad, "file"), "w", encoding="utf-8") as f:
            f.write("x")
        # 把已有文件当作日志目录 → makedirs/写入失败须抛 OSError, 不得静默
        with self.assertRaises(OSError):
            tl.Logger(os.path.join(bad, "file"), retention_days=2)


# ---------------------------------------------------------------- 单实例

class SingleInstanceTests(unittest.TestCase):
    def test_first_then_second_then_release(self):
        lock1 = tl.SingleInstance(0)
        self.assertTrue(lock1)
        port = lock1.port
        lock2 = tl.SingleInstance(port)
        self.assertFalse(lock2)      # 第二实例被拒
        lock2.close()
        lock1.close()
        lock3 = tl.SingleInstance(port)
        self.assertTrue(lock3)       # 释放后重启成功
        lock3.close()

    def test_bool_and_close(self):
        lock = tl.SingleInstance(0)
        self.assertTrue(bool(lock))
        lock.close()
        self.assertFalse(bool(lock))


# ---------------------------------------------------------------- 安全: 密钥不外泄

class SecretHygieneTests(unittest.TestCase):
    SECRET = "SUPERSECRET_TEST_TOKEN_12345"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tl_sec_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self):
        cfg = tl.validate_config(make_config(self.dir, api={"secret": self.SECRET}))
        logger = tl.Logger(cfg["log_dir"], cfg["retention_days"])
        api = _StatefulSource()
        api.response = {"downloadTotal": 1000, "uploadTotal": 2000,
                        "connections": [VALID_IPV4]}
        mon = tl.Monitor(cfg, logger, api, tl.EventTail(os.path.join(self.dir, "no.log")))
        for _ in range(2):
            mon.step()

    def test_secret_never_in_output_files(self):
        self._run()
        for fn in os.listdir(self.dir):
            with open(os.path.join(self.dir, fn), "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            self.assertNotIn(self.SECRET, content, f"secret leaked into {fn}")

    def test_config_error_does_not_echo_secret(self):
        cfg = make_config(self.dir, api={"secret": self.SECRET})
        cfg["api"]["url"] = "http://8.8.8.8:9097"
        with self.assertRaises(tl.ConfigError) as cm:
            tl.validate_config(cfg)
        self.assertNotIn(self.SECRET, str(cm.exception))

    def test_startup_log_has_no_secret_and_no_machine_path(self):
        cfg = tl.validate_config(make_config(self.dir, api={"secret": self.SECRET}))
        logger = tl.Logger(cfg["log_dir"], cfg["retention_days"])
        logger.app("STARTED source=local_api")
        logger.close()
        with open(os.path.join(self.dir, "app.log"), encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn(self.SECRET, content)
        self.assertNotIn(self.dir, content)   # 不写入解析后的机器专属路径


# ---------------------------------------------------------------- 集成: 本地回环数据源

class _Handler(BaseHTTPRequestHandler):
    source = None

    def do_GET(self):
        if self.path == "/connections":
            if self.source.down:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps(self.source.response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


class _StatefulSource:
    """可切换状态的数据源: 正常 / 降级 / 断开。"""

    def __init__(self):
        self.response = {"downloadTotal": 0, "uploadTotal": 0, "connections": []}
        self.down = False

    def snapshot(self):
        if self.down:
            return None, None, None
        data = self.response
        return (data["connections"], data["downloadTotal"], data["uploadTotal"])


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tl_int_")
        # 仅监听本机回环地址的测试数据源
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        _Handler.source = _StatefulSource()
        self.t = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.t.start()
        self.url = f"http://127.0.0.1:{self.port}"
        cfg = make_config(self.dir, api={"url": self.url, "secret": "int-test-secret"})
        cfg["monitor"]["conn_snapshot_every"] = 1
        self.cfg = tl.validate_config(cfg)
        self.logger = tl.Logger(self.cfg["log_dir"], self.cfg["retention_days"])
        self.mon = tl.Monitor(self.cfg, self.logger,
                              tl.GatewayApi(self.cfg["api_url"], self.cfg["api_secret"]),
                              tl.EventTail(os.path.join(self.dir, "no_events.log")))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.logger.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _load(self, name):
        p = os.path.join(self.dir, name)
        if not os.path.exists(p):
            return []
        with open(p, "r", encoding="utf-8") as f:
            return f.read().splitlines()

    def test_three_cycles_valid_and_invalid_counts(self):
        _Handler.source.response = {"downloadTotal": 100000, "uploadTotal": 50000,
                                    "connections": list(SAMPLE_CONNECTIONS)}
        for _ in range(3):
            s = self.mon.step()
            self.assertTrue(s["ok"])
            self.assertEqual(s["valid"], 2)    # IPv4 + IPv6
            self.assertEqual(s["invalid"], 2)  # 域名 + 越界端口

        detail = self._load(f"connections_detail_{tl.date_str()}.jsonl")
        self.assertEqual(len(detail), 6)       # 每个周期 2 条合格记录
        for line in detail:
            obj = json.loads(line)
            self.assertIn(obj["remote_ip"], ("203.0.113.10", "2001:db8::1"))
            self.assertTrue(1 <= obj["remote_port"] <= 65535)
            self.assertNotIn("host", obj)
            self.assertNotIn("rule", obj)
            self.assertNotIn("process", obj)

        csv = self._load(f"traffic_{tl.date_str()}.csv")
        self.assertEqual(csv[0], tl.CSV_HEADER)               # 表头一次
        self.assertEqual(len(csv), 4)                         # 表头 + 3 周期
        for row in csv[1:]:
            parts = row.split(",")
            self.assertEqual(parts[3], "4")                   # active
            self.assertEqual(parts[4], "2")                   # valid
            self.assertEqual(parts[5], "2")                   # invalid

    def test_counter_reset_no_negative_rate(self):
        src = _Handler.source
        src.response = {"downloadTotal": 10000, "uploadTotal": 9000,
                        "connections": [VALID_IPV4]}
        self.mon.step()
        time.sleep(0.05)
        src.response = {"downloadTotal": 5000, "uploadTotal": 9000,   # 下载回退
                        "connections": [VALID_IPV4]}
        s = self.mon.step()
        self.assertEqual(s["up_rate"], 0.0)
        self.assertEqual(s["down_rate"], 0.0)
        self.assertTrue(s["aggregate_valid"])
        csv = self._load(f"traffic_{tl.date_str()}.csv")
        for row in csv[1:]:
            up, down = row.split(",")[1], row.split(",")[2]
            self.assertGreaterEqual(float(up), 0.0)
            self.assertGreaterEqual(float(down), 0.0)
        app = self._load("app.log")
        self.assertTrue(any("COUNTER_RESET" in line for line in app))
        # 计数器回退只重设基线, 不删除本周期有效连接明细 (T-02)
        detail = self._load(f"connections_detail_{tl.date_str()}.jsonl")
        self.assertGreaterEqual(len(detail), 2)

    def test_invalid_aggregate_keeps_valid_detail(self):
        # 聚合总量非法时: 明细仍落盘, CSV 不出现伪造累计值, 快照速率为 null,
        # 本周期事件照常处理, 返回值区分部分成功。
        with open(os.path.join(self.dir, "no_events.log"), "a",
                  encoding="utf-8") as f:
            f.write("[TCP] 203.0.113.99:443\n")
        src = _Handler.source
        src.response = {"downloadTotal": "bad", "uploadTotal": 200,
                        "connections": [VALID_IPV4]}
        s = self.mon.step()
        self.assertTrue(s["ok"])
        self.assertTrue(s["source_ok"])
        self.assertTrue(s["detail_written"])
        self.assertFalse(s["aggregate_valid"])
        self.assertIsNone(s["up_rate"])
        self.assertIsNone(s["down_rate"])

        detail = self._load(f"connections_detail_{tl.date_str()}.jsonl")
        self.assertEqual(len(detail), 1)
        obj = json.loads(detail[0])
        self.assertEqual(obj["remote_ip"], "203.0.113.10")

        csv = self._load(f"traffic_{tl.date_str()}.csv")
        self.assertEqual(csv, [])   # 不写入伪造 CSV 累计值/速率

        snap = self._load(f"connections_{tl.date_str()}.log")
        self.assertTrue(any("up_kbs=null down_kbs=null" in line for line in snap))

        events = self._load(f"events_{tl.date_str()}.log")
        self.assertTrue(any("203.0.113.99" in line for line in events),
                        "聚合异常周期仍应处理本地事件")

        app = self._load("app.log")
        self.assertTrue(any("aggregate downloadTotal invalid" in line for line in app))

    def test_source_unavailable_then_recovered(self):
        src = _Handler.source
        src.response = {"downloadTotal": 100, "uploadTotal": 200, "connections": [VALID_IPV4]}
        self.assertTrue(self.mon.step()["ok"])
        src.down = True
        s = self.mon.step()
        self.assertFalse(s["ok"])
        src.down = False
        s = self.mon.step()
        self.assertTrue(s["ok"])
        app = self._load("app.log")
        self.assertTrue(any("SOURCE_UNAVAILABLE" in line for line in app))
        self.assertTrue(any("SOURCE_RECOVERED" in line for line in app))

    def test_no_secret_and_no_undefined_fields_in_outputs(self):
        _Handler.source.response = {"downloadTotal": 100, "uploadTotal": 200,
                                    "connections": [VALID_IPV4]}
        for _ in range(2):
            self.mon.step()
        for fn in os.listdir(self.dir):
            with open(os.path.join(self.dir, fn), "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            self.assertNotIn("int-test-secret", content)


# ---------------------------------------------------------------- 入口行为 (main)

class MainTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tl_main_")
        # 让 main 的单实例锁使用临时端口, 不依赖固定端口 48765
        orig_init = tl.SingleInstance.__init__
        self.lock_patcher = mock.patch.object(
            tl.SingleInstance, "__init__",
            lambda self, port=48765: orig_init(self, 0))
        self.lock_patcher.start()
        self.addCleanup(self.lock_patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write_config(self, **cfg):
        cfg = make_config(self.dir, **cfg)
        cfg["monitor"]["log_dir"] = self.dir
        p = os.path.join(self.dir, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return p

    def test_config_error_exit_code_2(self):
        p = self._write_config(api={"url": "http://8.8.8.8:9097"})
        code = tl.main(["--config", p])
        self.assertEqual(code, 2)

    def test_placeholder_secret_rejected(self):
        # validate_config 允许开发占位密钥; 生产启动 (main) 必须拒绝 (T-04)
        p = self._write_config(api={"secret": "set-your-secret"})
        code = tl.main(["--config", p])
        self.assertEqual(code, 2)

    def test_main_returns_one_when_log_dir_is_file(self):
        # 日志目录指向普通文件: main() 返回 1, 不抛出未处理 FileExistsError (T-05)
        blocker = os.path.join(self.dir, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        cfg = make_config(self.dir)
        cfg["monitor"]["log_dir"] = blocker
        cfg["monitor"]["gateway_log_path"] = os.path.join(self.dir, "evt.log")
        p = os.path.join(self.dir, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        code = tl.main(["--config", p])
        self.assertEqual(code, 1)
        # 初始化失败不创建任何业务日志/不写伪造数据
        self.assertEqual(os.listdir(self.dir), ["blocker", "config.json"])

    def test_missing_config_exit_code_2(self):
        code = tl.main(["--config", os.path.join(self.dir, "missing.json")])
        self.assertEqual(code, 2)

    def test_stop_via_keyboard_interrupt_logs_stopped(self):
        p = self._write_config()
        with mock.patch.object(tl.Monitor, "run_forever",
                               side_effect=KeyboardInterrupt):
            code = tl.main(["--config", p])
        self.assertEqual(code, 0)
        with open(os.path.join(self.dir, "app.log"), encoding="utf-8") as f:
            app = f.read()
        self.assertIn("STOPPED", app)
        self.assertIn("STARTED", app)

    def test_write_failure_exit_code_1(self):
        p = self._write_config()
        with mock.patch.object(tl.Logger, "write_lines",
                               side_effect=OSError("disk full")):
            code = tl.main(["--config", p])
        self.assertEqual(code, 1)

    def test_duplicate_instance_exits_cleanly(self):
        p = self._write_config()
        out = io.StringIO()
        lock = tl.SingleInstance(0)
        try:
            with mock.patch.object(tl.SingleInstance, "__bool__",
                                   return_value=False):
                with contextlib.redirect_stdout(out):
                    code = tl.main(["--config", p])
        finally:
            lock.close()
        self.assertEqual(code, 0)
        self.assertIn("INSTANCE_EXISTS", out.getvalue())
        with open(os.path.join(self.dir, "app.log"), encoding="utf-8") as f:
            app = f.read()
        self.assertIn("INSTANCE_EXISTS", app)


if __name__ == "__main__":
    unittest.main()
