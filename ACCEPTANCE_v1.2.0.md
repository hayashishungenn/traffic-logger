# traffic-logger v1.2.0 人工验收记录

> 完成标准是"问题被修复并被观察到", 不是"代码看起来更完整"。本记录只写实测结果,
> 未执行项一律列出并说明原因, 不伪造完成。

## 1. 自动化验证结果 (已实测)

| 命令 | 结果 | 说明 |
|---|---|---|
| `python -m unittest discover -s tests` | PASS | **113** 个测试通过, **2** 个跳过 (schtasks 无权限, 见 §4) |
| `python -m py_compile traffic_logger.py tests/test_traffic_logger.py tests/test_windows_scripts.py` | PASS | 无语法错误, 无 `SyntaxWarning` |
| `python -m json.tool config.json` | PASS | 配置为合法 JSON |
| `python traffic_logger.py --help` | PASS | 正常输出帮助 |
| `git diff --check` | PASS | 无空白错误 |

静态检索: 源码、配置、脚本、README、任务书、测试与夹具中未发现专属工具名称、
真实密钥、真实用户 IP、机器专属路径或未脱敏样本。Windows 批处理脚本仅含 ASCII
字符与 CRLF 换行, 不依赖终端默认编码 (已有测试断言)。

## 2. 修复与回归测试依据 (自动化)

| 阶段 | 关键回归测试 | 结果 |
|---|---|---|
| 清理白名单 (T-01) | `test_cleanup_preserves_expired_unknown_file` 等 | PASS: 过期程序日志被删, 无关文件/子目录保留; UNC/设备路径被拒 |
| 明细/聚合分离 (T-02) | `test_invalid_aggregate_keeps_valid_detail` | PASS: 聚合非法时明细仍落盘, CSV 无伪造值, 快照速率 `null`, 事件照常处理 |
| 事件游标 (T-03) | `test_same_size_event_replacement_resets_cursor` 等 | PASS: 替换/截断/缺失恢复从头重读, 行数/行长上限有计数 |
| 配置边界 (T-04) | `test_empty_secret_rejected`、`test_non_finite_interval_rejected`、`test_url_path_userinfo_query_rejected` | PASS: 空密钥/NaN/无穷/带路径 URL/占位密钥启动均被拒 |
| 生命周期 (T-05) | `test_main_returns_one_when_log_dir_is_file` | PASS: 日志目录为普通文件时返回 1, 无未处理异常 |
| 输入上限 (T-06) | `test_http_response_size_limit` | PASS: 超限响应按数据源异常处理 |
| 脚本 (T-07) | `test_stop_logic_terminates_project_process` 等 | PASS: 解释器回退、非零退出、非交互 pause、精确停止 |
| 契约夹具 (T-08) | `ContractFixtureTests` | PASS: IPv4/裸 IPv6/带括号 IPv6/缺字段/异常字段独立测试 |
| **P1 聚合缺失 (本轮)** | `test_missing_aggregate_keeps_valid_detail`、`test_both_aggregates_invalid_keeps_detail`、`GatewayApiContractTests` | PASS: 聚合字段缺失/非法不再误判数据源不可用, 有效明细 JSONL 照常落盘, CSV 无错误数据, 事件仍处理 |
| **P1 脚本加固 (本轮)** | `test_scripts_ascii_and_crlf`、`test_stop_logic_ignores_prefix_collision` | PASS: 脚本仅 ASCII+CRLF (不依赖终端编码); 只停止精确匹配项目脚本路径的实例, 不误停 `.py.bak` 前缀进程 |
| **P2 事件长度边界 (本轮)** | `test_event_line_exactly_over_limit_with_newline_discarded`、`test_event_line_over_limit_without_newline_discarded`、`test_overlong_line_leaves_no_parseable_residue` | PASS: 超限行 (带/不带换行) 整行丢弃计数, 残片不产生看似有效的 IP/端口 |
| **P2 localhost 边界 (本轮)** | `test_localhost_resolved_to_non_loopback_rejected`、`test_localhost_unresolvable_rejected`、`test_localhost_url_accepted_and_documented` | PASS: localhost 必须解析到回环地址, 解析到远程地址或无法解析时启动拒绝 |

## 3. 数据源契约口径 (本轮, 与代码一致)

- `GatewayApi.snapshot()` 只在接口不可访问、响应非对象、连接列表缺失时返回 None 元组 (数据源不可用); 聚合总量缺失/非法时原样返回, 由 `Monitor.step()` 区分 `aggregate_valid`。
- `api.url` 中保留的 `localhost` 在实际连接前解析并确认全部结果为回环地址, 禁止自定义 hosts 指向远程。

## 4. 未完成的人工验收项与阻塞原因

### 4.1 真实数据源 60 秒人工验收 — 未执行

任务书要求 "在获得明确授权的本机环境中启动本地数据源, 使用临时日志目录运行不少于
60 秒"。阻塞原因:

1. 需要**明确授权**的真实本地数据源 (本机 `127.0.0.1:9097`); 本次会话未获得授权。
2. 当前已有 2 个运行中的实例 (PID 29172、58560, `pythonw.exe traffic_logger.py`),
   持有单实例锁 (`127.0.0.1:48765`)。未获授权停止它们, 新实例会按设计返回
   `INSTANCE_EXISTS`, 无法完成前台 60 秒运行。

完成后可执行:

```powershell
# 1) 由部署人确认/启动真实数据源; 2) 使用临时日志目录运行 ≥60 秒
python traffic_logger.py --config <授权配置>
# 3) 模拟接口中断/恢复、计数器回退、事件文件追加/替换/截断/缺失恢复、磁盘不可写
# 4) Ctrl+C 后再次启动; 5) 检查日志目录无无关文件被删
```

### 4.2 计划任务安装/卸载脚本完整回读 — 未在本机执行

`WindowsScriptLiveTests` (install → `schtasks /Query` → uninstall → 确认删除) 在本机
跳过, 原因: 当前 shell 会话调用 `schtasks /Create` 返回"拒绝访问" (无创建计划任务的
权限)。脚本的结构、解释器回退、退出码、ASCII/CRLF 与精确停止逻辑已由
`WindowsScriptFileTests` 和 `WindowsScriptStopTests` 实测覆盖; 完整回读需在具有计划
任务权限的会话中运行 `tests/test_windows_scripts.py::WindowsScriptLiveTests`。

## 5. 结论

- 本轮全部 P1 修复 (聚合缺失明细保留、脚本编码与精确停止) 与 P2 加固 (事件长度
  边界、localhost 回环解析) 已完成并有回归测试。
- 当前测试总数 **113** (此前任务书中的 "66 个测试" 已不是当前总数, 以本记录为准)。
- 真实数据源 60 秒人工验收与计划任务完整回读**未完成**, 阻塞原因如上, 未伪造完成。
- 结论: **NOT READY** (最高未解决项: 真实数据源人工验收)。
