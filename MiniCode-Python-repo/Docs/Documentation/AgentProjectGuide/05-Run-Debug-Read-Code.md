# 05. 运行、调试与源码阅读路线

## 1. 准备环境

项目要求 Python 3.11+。先确认版本：

```powershell
python --version
```

建议创建虚拟环境，避免污染系统 Python：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

`.[dev]` 会安装 `pytest`、`hypothesis` 和构建工具；核心运行时本身没有声明第三方依赖。

如果 PowerShell 阻止激活脚本，可以只对当前终端临时调整策略，或直接使用 `.venv\Scripts\python.exe` 执行命令。不要为了这个项目修改整台机器的永久安全策略。

## 2. 配置模型

### 方法一：当前 PowerShell 会话使用环境变量

下面仅为占位示例，不要把真实 key 写进文档或 Git：

```powershell
$env:ANTHROPIC_MODEL = "<your-model-id>"
$env:ANTHROPIC_API_KEY = "<your-api-key>"
```

OpenAI-compatible endpoint 可使用：

```powershell
$env:OPENAI_MODEL = "<your-model-id>"
$env:OPENAI_API_KEY = "<your-api-key>"
$env:OPENAI_BASE_URL = "https://api.openai.com/v1"
```

具体字段以 `.env.example` 与 `minicode/config.py` 当前实现为准。

### 方法二：用户设置文件

使用交互安装器：

```powershell
minicode-py --install
```

或编辑用户目录下：

```text
~/.mini-code/settings.json
```

先做只读诊断：

```powershell
minicode-py --validate-config
minicode-readiness --json --fail-on blocked
```

## 3. 三种常用运行方式

### 交互式 TUI

```powershell
minicode-py
```

适合真实开发：可以看到流式输出、工具状态和权限审批。

### Headless 单轮

```powershell
minicode-headless "Explain this repository."
```

默认不会在无交互环境中自动批准编辑。明确需要修改时：

```powershell
minicode-headless --allow-edits "Fix the failing targeted test and verify it."
```

这会自动批准本次 headless 运行的编辑、命令和 cwd 外访问，应只在你理解任务与运行环境时使用。

### 通过 Python 模块

```powershell
python -m minicode.headless "Explain what agent_loop.py does."
```

## 4. 两种 Runtime Profile

默认是 `single`。深入任务可以在当前终端设置：

```powershell
$env:MINI_CODE_RUNTIME_PROFILE = "single-deep"
minicode-py
```

`single-deep` 增加 step budget，启用严格 verification，并允许满足条件后 widening。它会增加调用和上下文成本，不是所有问题都需要。

如果需要额外 utility wrapper tools：

```powershell
$env:MINI_CODE_TOOL_PROFILE = "full"
```

## 5. 启动后先练习这些命令

```text
/help
/tools
/session
/memory
/readiness
/checkpoints
/session-replay
/rewind-preview
```

建议先让 Agent 做一个纯只读任务：

```text
请先不要修改文件。解释 minicode/main.py 到 agent_loop.py 的调用链，并列出代码证据。
```

再做一个很小的可回退修改，例如在临时测试仓库里改一行文档，观察：

1. TUI 是否显示 diff；
2. 允许后是否出现 checkpoint；
3. `/rewind-preview` 显示什么；
4. `/rewind` 是否恢复原内容。

不要拿重要且未备份的工作区做第一次练习。

## 6. 推荐的源码阅读顺序

### 路线 A：先读懂最小 Agent

1. `pyproject.toml`：确认入口和依赖；
2. `minicode/types.py`：认识 `AgentStep`、`ToolCall`、`ModelAdapter`；
3. `minicode/model_registry.py`：模型怎么选；
4. `minicode/anthropic_adapter.py` 或 `openai_adapter.py`：协议怎么统一；
5. `minicode/tooling.py`：ToolDefinition、ToolRegistry、ToolResult；
6. `minicode/tools/read_file.py`：读一个最简单工具；
7. `minicode/agent_loop.py::run_agent_turn`：看模型和工具如何循环；
8. `minicode/turn_kernel.py`：看何时继续、验证和结束。

### 路线 B：再理解产品能力

1. `minicode/main.py`：对象组装；
2. `minicode/tty_app.py`：TUI 主循环；
3. `minicode/tui/input_handler.py`：输入路由和后台 Agent；
4. `minicode/turn_events.py`：线程间事件合同；
5. `minicode/session.py`：持久会话；
6. `minicode/file_review.py`：安全写入；
7. `minicode/permissions.py`：权限决策。

### 路线 C：最后看增强系统

1. `minicode/memory.py`；
2. `minicode/context_manager.py`；
3. `minicode/context_compactor.py`；
4. `minicode/cybernetic_orchestrator.py`；
5. 逐个 controller；
6. `benchmarks/` 与对应 tests。

## 7. 阅读大文件的方法

不要从第一行一直滚到最后一行。使用“符号地图 → 单条路径 → 相关测试”的方式。

### 先列符号

```powershell
rg -n "^(class |def |async def )" minicode\agent_loop.py
```

### 再搜关键调用

```powershell
rg -n "run_agent_turn|_model_next|tools.execute|decide_assistant_turn" minicode tests
```

### 最后找测试

```powershell
rg -n "verification|widening|max_steps|await_user" tests\test_agent_loop.py tests\test_turn_kernel.py
```

每读一个函数，回答四个问题：

1. 输入是什么？
2. 输出是什么？
3. 改了哪些状态或外部资源？
4. 失败后由谁处理？

## 8. 最推荐的断点路径

在 VS Code 或 PyCharm 中使用模块启动配置，先断这几个函数：

```text
minicode.main.main
minicode.tui.input_handler._handle_input
minicode.agent_loop.run_agent_turn
minicode.agent_loop._model_next
minicode.tooling.ToolRegistry.execute
minicode.file_review.apply_reviewed_file_change
minicode.turn_kernel.decide_assistant_turn
minicode.tui.session_flow.refresh_tty_session_snapshot
```

### VS Code 启动思路

程序选择：

```text
module = minicode.main
cwd = 当前仓库根
console = integratedTerminal
```

不要在 debugger 配置中硬编码 API key，使用终端环境变量或受保护的本地配置。

## 9. 如何观察一轮 Agent

在 `run_agent_turn` 中重点 watch：

| 变量 | 看什么 |
| --- | --- |
| `current_messages` | system/user/tool/result 如何增长 |
| `runtime_profile` | single 还是 single-deep |
| `turn_state.step` | 当前第几步 |
| `turn_state.step_policy` | explore/execute/verify |
| `next_step` | 模型返回文本还是 tool calls |
| `calls` | 工具名、id、参数 |
| `_results` | 并发/串行工具的标准结果 |
| `latest_tool_result_summary` | verification 使用的证据摘要 |
| `stop_reason` | 为什么结束 |

如果使用真实模型，断点停太久可能让网络流或工具超时。第一次最好用短任务，或在单元测试中用 fake model 跟踪。

## 10. 测试策略

### 语法检查

```powershell
python -m compileall -q minicode tests benchmarks Main Package
```

### 全量测试

```powershell
python -m pytest -q --import-mode=importlib
```

### 按子系统跑

```powershell
python -m pytest -q tests\test_agent_loop.py tests\test_turn_kernel.py
python -m pytest -q tests\test_permissions.py tests\test_session.py
python -m pytest -q tests\test_memory_integration.py tests\test_memory_search_regression.py tests\test_context_compactor.py
python -m pytest -q tests\test_mcp.py tests\test_tools.py
python -m pytest -q tests\test_cybernetic_orchestrator.py
```

### 结构检查

```powershell
python -m minicode.structure_check --root . --hotspots 5 --max-dependency-upstream 4 --check-material-inventory
```

### Readiness

```powershell
python -m minicode.readiness --json --fail-on blocked
```

测试必须区分三层：

```text
单元测试：一个决策函数/工具
集成测试：adapter、loop、session 等组合
benchmark/readiness：长链路、性能和发布证据
```

## 11. 用 Fake Model 理解 Agent Loop

真实 Provider 会引入网络、凭据和非确定性。测试通常构造一个 fake/mock adapter，让 `next()` 按预定顺序返回：

```text
第 1 次：tool_calls(read_file)
第 2 次：tool_calls(test_runner)
第 3 次：assistant final
```

这样可以稳定验证：

- 工具结果是否被回填；
- step 是否增长；
- await_user 是否立即返回；
- max_steps 是否生效；
- verify/widening 是否按状态转换；
- callback/event 是否顺序正确。

这是测试 Agent 的基本技巧：控制模型输出，把不确定系统变成确定状态机测试。

## 12. 常见故障排查

### `No model configured`

检查：

```powershell
minicode-py --validate-config
minicode-readiness --json --fail-on blocked
```

确认 model id、key 和 base URL 属于同一个 provider 通道。

### `Command requires approval`

你可能在 headless 模式，或命令被识别为危险操作。使用交互 TUI 审批；只在可信 CI 环境显式加 `--allow-edits`。

### `Edit requires approval`

同上。文件编辑不是默认静默执行，TUI 会展示 diff。

### `Path ... is outside cwd`

Agent 正在访问工作区外路径。确认目标是否正确，再在 TUI 中按最小目录 scope 授权。

### `Reached the maximum tool step limit`

可能原因：

- 模型重复搜索；
- 工具错误没有被修复；
- 最终协议没有正确输出；
- 任务范围太大。

先看 `/session-replay` 和日志，不要第一反应就是无限增加 max steps。

### `Context near limit`

先检查是否有超大工具结果和重复读文件；可缩小任务、手工 compact，或拆分会话。blocking guard 是为了避免 provider 直接报 413 类错误。

### MCP 工具不存在或 server 失败

确认 MCP 是否被信任和加载、命令是否合法、server 是否能通过 stdio 启动。MCP 是 lazy init，启动时显示未连接不一定等于永久不可用，第一次调用的错误更有诊断价值。

### TUI 显示卡住

检查：

- worker thread 是否还活着；
- `agent_event_queue` 是否持续 drain；
- provider timeout；
- 工具 timeout；
- `~/.mini-code/minicode.log`。

## 13. 日志在哪里

默认日志文件：

```text
~/.mini-code/minicode.log
```

它使用 10 MB 的按大小轮转，最多保留 5 个备份。调试时可用：

```powershell
minicode-py --log-level DEBUG
minicode-py --log-level DEBUG --structured-logs
```

结构化日志适合机器过滤，包含 module、level、file，以及可用时的 tool/model/duration/token/cost 等字段。

不要把包含敏感 provider 错误上下文的日志直接上传公共 issue；先检查并脱敏。

## 14. 三个动手练习

### 练习一：追踪只读工具

目标：理解最小 tool call。

1. 在 `ToolRegistry.execute` 断点；
2. 让 Agent 读取 `pyproject.toml`；
3. 观察 validator、ToolContext 和 ToolResult；
4. 观察下一次 `model.next` 的 messages。

### 练习二：追踪安全编辑

目标：理解 permission + checkpoint。

1. 在临时文件中准备一行文本；
2. 让 Agent 修改；
3. 在 `apply_reviewed_file_change` 断点；
4. 看 diff、审批和 checkpoint 顺序；
5. 执行 rewind 并确认 reverse checkpoint。

### 练习三：写一个只读工具

目标：理解扩展点。

可以新增一个“统计指定文本文件行数”的只读工具：

1. 定义 validator；
2. 定义 `_run` 并返回 ToolResult；
3. 定义 input schema 与 metadata；
4. 注册到 core 或 full profile；
5. 写参数错误、成功、超大输入和并发安全测试；
6. 确认模型 adapter 能看到新 schema。

仓库已有 `line_count` utility tool，可先把它当参考，再在实验分支实现不同的教学版本，避免重复污染主功能。

## 15. 一次改动的标准验证清单

```text
[ ] 明确改动目标和边界
[ ] 找到直接实现与调用方
[ ] 找到或补充对应测试
[ ] 做最小修改
[ ] 跑最相关测试
[ ] 跑受影响集成测试
[ ] 运行 compileall
[ ] 查看 git diff
[ ] 确认没有 key、临时报告或会话数据进入提交
[ ] 在最终说明中报告真实验证结果和未验证项
```

## 16. 当前这份手册的验证说明

生成手册时已对仓库和核心调用链做静态核对，并统计了代码与测试文件。当前系统 Python 未安装 `pytest`，所以没有在本次文档工作中复跑 README 的历史全量测试数字。你本地执行 `python -m pip install -e ".[dev]"` 后，应重新运行本章测试命令，再把结果写进自己的简历材料。
