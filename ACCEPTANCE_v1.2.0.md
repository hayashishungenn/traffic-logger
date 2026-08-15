# traffic-logger v1.2.0 人工验收记录

> 完成标准是"问题被修复并被观察到", 不是"代码看起来更完整"。本记录只写实测结果,
> 未执行项一律列出并说明原因, 不伪造完成。

## 1. 自动化验证结果 (已实测)

| 命令 | 结果 | 说明 |
|---|---|---|
| `python -m unittest discover -s tests` | PASS | 100 个测试通过, 2 个跳过 (schtasks 无权限, 见 §3) |
| `python -m py_compile traffic_logger.py tests/test_traffic_logger.py tests/test_windows_scripts.py` | PASS | 无语法错误, 无 `SyntaxWarning` |
| `python -m json.tool config.json` | PASS | 配置为合法 JSON |
| `python traffic_logger.py --help` | PASS | 正常输出帮助 |
| `git diff --check` | PASS | 无空白错误 |

静态检索: 源码、配置、脚本、README、任务书、测试与夹具中未发现专属工具名称、
真实密钥、真实用户 IP、机器专属路径或未脱敏样本 (测试中的 `256.1.1.1` 为非法值拒
绝用例)。

## 2. T-01 ~ T-08 实测依据 (自动化回归)

| 任务 | 关键回归测试 | 结果 |
|---|---|---|
| T-01 清理白名单 | `test_cleanup_preserves_expired_unknown_file` 等 | PASS: 过期程序日志被删, `important.txt`/`config.json`/`README.md`/子目录保留; UNC/设备路径被拒 |
| T-02 明细/聚合分离 | `test_invalid_aggregate_keeps_valid_detail` | PASS: 聚合非法时 JSONL 明细仍落盘, CSV 无伪造累计值, 快照速率 `null`, 事件照常处理 |
| T-03 事件游标 | `test_same_size_event_replacement_resets_cursor` 等 | PASS: 同大小替换/截断/缺失恢复均从头重读, 行数/行长上限有计数 |
| T-04 配置边界 | `test_empty_secret_rejected`、`test_non_finite_interval_rejected`、`test_url_path_userinfo_query_rejected` | PASS: 空密钥/NaN/无穷/带路径 URL/占位密钥启动均被拒 |
| T-05 生命周期 | `test_main_returns_one_when_log_dir_is_file` | PASS: 日志目录为普通文件时 `main()` 返回 1, 无未处理 `FileExistsError` |
| T-06 输入上限 | `test_http_response_size_limit` | PASS: 超限响应按数据源异常处理, 只记录类型 |
| T-07 脚本 | `test_stop_logic_terminates_project_process` 等 7 项 | PASS: 解释器回退、缺失脚本/坏解释器非零退出、非交互 pause、规范化路径精确停止 (含空格/括号/中文/`&` 路径) |
| T-08 契约夹具 | `ContractFixtureTests` (5 项) | PASS: IPv4/裸 IPv6/带括号 IPv6/缺字段/异常字段均有独立测试 |

## 3. 未完成的人工验收项与阻塞原因

### 3.1 真实数据源 60 秒人工验收 — 未执行

任务书 T-09 要求: "在获得明确授权的本机环境中启动本地数据源, 使用临时日志目录运行
不少于 60 秒"。

阻塞原因:

1. 需要**明确授权**的真实本地数据源 (本机 `127.0.0.1:9097`)。任务书 2.3 要求只监控部署
   人拥有管理权或已取得明确授权的数据源; 本次会话未获得针对该数据源的授权。
2. 当前已有 2 个运行中的实例 (PID 29172、58560, `pythonw.exe traffic_logger.py`),
   持有单实例锁 (`127.0.0.1:48765`)。未获授权停止它们, 因此新实例启动会按设计返回
   `INSTANCE_EXISTS`, 无法在锁外完成前台 60 秒运行。

完成后可执行:

```powershell
# 1) 由部署人确认/启动真实数据源; 2) 使用临时日志目录运行 ≥60 秒
python traffic_logger.py --config <授权配置>   # 观察 JSONL/CSV/快照/事件/app.log 追加
# 3) 模拟接口中断/恢复、计数器回退、事件文件替换、磁盘不可写
# 4) Ctrl+C 后再次启动; 5) 检查日志目录无无关文件被删
```

### 3.2 计划任务安装/卸载脚本回读 — 未在本机执行

自动化测试中的 `WindowsScriptLiveTests` (install → `schtasks /Query` → uninstall) 在本机
跳过, 原因: 当前 shell 会话调用 `schtasks /Create` 返回"拒绝访问" (无创建计划任务的
权限)。脚本本身的结构、解释器回退、退出码与停止逻辑已由 `WindowsScriptFileTests` 和
`WindowsScriptStopTests` 实测覆盖; 完整回读需在具有计划任务权限的会话中运行
`ACCEPTANCE_v1.2.0.md` 步骤 5 / `tests/test_windows_scripts.py::WindowsScriptLiveTests`。

## 4. 结论

- 全部 P0/P1 问题 (T-01~T-05) 已修复并有回归测试。
- 原始 66 个测试与新增测试全部通过 (共 100)。
- 真实数据源 60 秒人工验收与计划任务完整回读**未完成**, 阻塞原因如上, 未伪造完成。
- 结论: **NOT READY** (最高未解决项: 真实数据源人工验收)。
