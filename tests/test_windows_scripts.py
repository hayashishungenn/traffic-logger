# -*- coding: utf-8 -*-
"""Windows 安装/卸载脚本自动化测试 (T-07)。

两类测试:
  1. WindowsScriptFileTests —— 不依赖计划任务权限, 始终运行:
     检查脚本结构、解释器回退、缺失脚本/坏解释器退出码、非交互 pause 开关、
     卸载路径精确匹配。
  2. WindowsScriptLiveTests —— 需要 schtasks 创建权限, 否则跳过:
     在临时项目副本中执行 install.bat / uninstall.bat (非影响用户的任务名),
     验证安装、回读、卸载和日志保留的可观察结果; 以及仅用 PowerShell 停止
     逻辑对真实项目进程的停止回读 (不依赖 schtasks)。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)


def _extract_stop_line():
    """提取 uninstall.bat 中 [1/3] 的 PowerShell 停止命令, 保持测试与脚本同步。"""
    with open(os.path.join(BASE_DIR, "uninstall.bat"), encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("powershell "):
                return line.strip()
    raise AssertionError("uninstall.bat 缺少 powershell 停止命令")


def _read_script(name):
    with open(os.path.join(BASE_DIR, name), encoding="utf-8") as f:
        return f.read()


def _probe_schtasks():
    """探测能否为本机用户创建并删除计划任务; 不能则跳过脚本实测。"""
    if os.name != "nt":
        return False
    name = "tl_probe_%d" % int(time.time() * 1000)
    try:
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", name,
             "/TR", "cmd /c exit 0", "/SC", "ONLOGON", "/RL", "LIMITED", "/F"],
            capture_output=True, text=True, timeout=60)
    except Exception:
        return False
    finally:
        try:
            subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"],
                           capture_output=True, timeout=60)
        except Exception:
            pass
    return r.returncode == 0


class WindowsScriptFileTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="tl_script_file_")
        self.project = os.path.join(self.base, "tl 测试&(x)安装")
        os.makedirs(self.project)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _run_bat(self, name, extra_env=None):
        env = dict(os.environ)
        env["TL_TASK_NAME"] = "tl_file_%d" % int(time.time() * 1000)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(["cmd", "/c", name], cwd=self.project, env=env,
                              capture_output=True, text=True, timeout=120)

    def test_install_missing_script_exits_nonzero(self):
        # 只复制 install.bat, 不复制 traffic_logger.py → 非零退出
        shutil.copy(os.path.join(BASE_DIR, "install.bat"), self.project)
        r = self._run_bat("install.bat")
        self.assertNotEqual(r.returncode, 0, "缺少脚本时应非零退出")

    def test_install_bad_interpreter_exits_nonzero(self):
        shutil.copy(os.path.join(BASE_DIR, "install.bat"), self.project)
        with open(os.path.join(self.project, "traffic_logger.py"), "w",
                  encoding="utf-8") as f:
            f.write("# stub\n")
        r = self._run_bat("install.bat", {"PYTHONW": "C:\\no_such_python.exe"})
        self.assertNotEqual(r.returncode, 0, "解释器不可用时应非零退出")

    def test_install_probes_pythonw_then_python(self):
        install = _read_script("install.bat")
        self.assertIn("where pythonw", install)
        self.assertIn("where python", install)          # python.exe 回退
        self.assertIn("TL_TASK_NAME", install)          # 任务名可覆盖

    def test_scripts_pause_only_behind_interactive_switch(self):
        for name in ("install.bat", "uninstall.bat"):
            lines = _read_script(name).splitlines()
            for i, line in enumerate(lines):
                if line.strip() == "pause":
                    window = lines[max(0, i - 3):i]
                    self.assertTrue(
                        any("if defined TL_INTERACTIVE" in w for w in window),
                        f"{name}: pause 必须位于 if defined TL_INTERACTIVE 分支内")
            self.assertIn("interactive", _read_script(name))

    def test_uninstall_uses_normalized_path_matching(self):
        un = _read_script("uninstall.bat")
        self.assertIn("TL_STOP_SCRIPT", un)
        self.assertIn("[IO.Path]::GetFullPath", un)     # 规范化路径
        self.assertIn("python(w)?\\.exe", un)           # 只匹配 python/pythonw
        self.assertIn("ERROR_NOT_STOPPED", un)          # 未停止时非零
        self.assertNotIn("Get-Process | Stop-Process", un)  # 拒绝模糊进程匹配

    def test_scripts_reference_project_relative_script(self):
        for name in ("install.bat", "uninstall.bat"):
            self.assertIn("%~dp0", _read_script(name))
            self.assertIn("traffic_logger.py", _read_script(name))


@unittest.skipUnless(_probe_schtasks(), "本机无法创建计划任务 (schtasks 不可用或无权限)")
class WindowsScriptLiveTests(unittest.TestCase):
    def setUp(self):
        base = tempfile.mkdtemp(prefix="tl_script_live_")
        self.project = os.path.join(base, "tl 测试&(x)安装")
        os.makedirs(self.project)
        for name in ("traffic_logger.py", "install.bat", "uninstall.bat"):
            shutil.copy(os.path.join(BASE_DIR, name), os.path.join(self.project, name))
        self.task_name = "tl_live_%d" % int(time.time() * 1000)
        self.env = dict(os.environ)
        self.env["TL_TASK_NAME"] = self.task_name
        self.env.pop("PYTHONW", None)
        self.env.pop("TL_STOP_SCRIPT", None)

    def tearDown(self):
        try:
            subprocess.run(["schtasks", "/Delete", "/TN", self.task_name, "/F"],
                           capture_output=True, timeout=60)
        except Exception:
            pass
        shutil.rmtree(os.path.dirname(self.project), ignore_errors=True)

    def _run_bat(self, name):
        return subprocess.run(["cmd", "/c", name], cwd=self.project, env=self.env,
                              capture_output=True, text=True, timeout=180)

    def test_install_query_uninstall_cycle(self):
        r = self._run_bat("install.bat")
        self.assertEqual(r.returncode, 0, "install.bat 失败:\n" + r.stdout + r.stderr)

        q = subprocess.run(["schtasks", "/Query", "/TN", self.task_name,
                            "/FO", "LIST", "/V"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(q.returncode, 0, "任务回读失败:\n" + q.stdout + q.stderr)
        self.assertIn("traffic_logger.py", q.stdout)

        u = self._run_bat("uninstall.bat")
        self.assertEqual(u.returncode, 0, "uninstall.bat 失败:\n" + u.stdout + u.stderr)

        q2 = subprocess.run(["schtasks", "/Query", "/TN", self.task_name],
                            capture_output=True, text=True, timeout=60)
        self.assertNotEqual(q2.returncode, 0, "任务卸载后仍存在")

    def test_uninstall_keeps_logs_dir(self):
        log_dir = os.path.join(self.project, "logs")
        os.makedirs(log_dir)
        with open(os.path.join(log_dir, "app.log"), "w", encoding="utf-8") as f:
            f.write("keep\n")
        self._run_bat("install.bat")
        u = self._run_bat("uninstall.bat")
        self.assertEqual(u.returncode, 0, u.stdout + u.stderr)
        self.assertTrue(os.path.exists(os.path.join(log_dir, "app.log")),
                        "卸载脚本不应删除 logs/ 内容")


class WindowsScriptStopTests(unittest.TestCase):
    """停止逻辑实测: 用 uninstall.bat 中的 PowerShell 命令停止真实项目进程。

    不依赖 schtasks 权限; 验证特殊字符路径下的精确匹配与停止后回读。
    使用 stub 脚本避免与正在运行的实例竞争单实例锁 (单实例锁本就是全局的)。
    """

    STUB = "import time\nwhile True:\n    time.sleep(1)\n"

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="tl_script_stop_")
        self.project = os.path.join(self.base, "tl 测试&(x)安装")
        os.makedirs(self.project)
        with open(os.path.join(self.project, "traffic_logger.py"), "w",
                  encoding="utf-8") as f:
            f.write(self.STUB)
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(self.project, "traffic_logger.py")],
            cwd=self.project, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def tearDown(self):
        self.proc.kill()
        self.proc.wait()
        shutil.rmtree(self.base, ignore_errors=True)

    def test_stop_logic_terminates_project_process(self):
        time.sleep(1.0)
        self.assertIsNone(self.proc.poll(), "测试进程应仍在运行")
        env = dict(os.environ)
        env["TL_STOP_SCRIPT"] = os.path.join(self.project, "traffic_logger.py")
        # 把 uninstall.bat 中的停止命令原样写入临时 .bat 再执行, 复现真实批处理
        # 解析 (cmd /c 内联字符串会重解析引号, 不能直接传内联命令)。
        # 停止命令只依赖 $env:TL_STOP_SCRIPT, 脚本位置不影响匹配。
        stop_bat = os.path.join(self.base, "stop_step.bat")
        with open(stop_bat, "w", encoding="utf-8") as f:
            f.write("@echo off\n" + _extract_stop_line() + "\n")
        r = subprocess.run(["cmd", "/c", stop_bat], cwd=self.base, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertIn("STOPPED_OK", r.stdout + r.stderr)
        self.proc.wait(timeout=15)
        self.assertIsNotNone(self.proc.poll(), "进程应已被停止")


if __name__ == "__main__":
    unittest.main()
