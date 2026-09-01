# 07. 代码证据索引与事实边界

这份索引用于快速查证。路径链接以本手册目录为起点。

## 1. 核心事实与代码证据

| 说法 | 主要代码证据 | 建议查看的测试 |
| --- | --- | --- |
| 当前 Python 主包从 `minicode/` 安装 | [pyproject.toml](../../../pyproject.toml)、[README.zh-CN.md](../../../README.zh-CN.md) | `tests/test_packaging.py` |
| CLI 有 5 个入口 | [pyproject.toml](../../../pyproject.toml) 的 `[project.scripts]` | `tests/test_main.py`、readiness/provider tests |
| 主程序组装 model/tools/permissions/memory/context | [minicode/main.py](../../../minicode/main.py) | `tests/test_main.py`、`tests/test_integration.py` |
| Headless 是无交互单轮入口 | [minicode/headless.py](../../../minicode/headless.py) | `tests/test_headless.py` |
| 核心循环是多步模型—工具闭环 | [minicode/agent_loop.py](../../../minicode/agent_loop.py) 的 `run_agent_turn` | `tests/test_agent_loop.py`、`tests/test_agent_flow.py` |
| 内部统一模型输出为 `AgentStep` | [minicode/types.py](../../../minicode/types.py) | adapter 与 agent loop tests |
| 支持 Anthropic 与 OpenAI-compatible adapter | [minicode/anthropic_adapter.py](../../../minicode/anthropic_adapter.py)、[minicode/openai_adapter.py](../../../minicode/openai_adapter.py) | `tests/test_anthropic_adapter.py`、`tests/test_openai_adapter.py` |
| Model registry 负责 provider 检测和 adapter 创建 | [minicode/model_registry.py](../../../minicode/model_registry.py) | `tests/test_model_selection_controller.py`、provider tests |
| 默认有 23 个核心工具 | [minicode/tools/__init__.py](../../../minicode/tools/__init__.py) 的 `_CORE_TOOLS` | `tests/test_tools.py`、`tests/test_functional_completeness.py` |
| ToolRegistry 统一校验、执行、异常和截断 | [minicode/tooling.py](../../../minicode/tooling.py) 的 `ToolRegistry.execute` | `tests/test_tools.py` |
| 多 tool call 会区分并发安全与串行 | [minicode/agent_loop.py](../../../minicode/agent_loop.py) 的 concurrent tool execution、`ToolScheduler` | `tests/test_agent_loop.py`、concurrency tests |
| TUI 用后台线程和 TurnEventQueue | [minicode/tui/input_handler.py](../../../minicode/tui/input_handler.py)、[minicode/turn_events.py](../../../minicode/turn_events.py) | `tests/test_turn_events.py`、`tests/test_tui.py` |
| 路径、命令、编辑有独立权限门禁 | [minicode/permissions.py](../../../minicode/permissions.py) | `tests/test_permissions.py` |
| 编辑先生成 diff、审批、checkpoint 再写 | [minicode/file_review.py](../../../minicode/file_review.py) | session/permissions/tools tests |
| rewind 会创建 reverse checkpoint | [minicode/session.py](../../../minicode/session.py) 的 `rewind_session_data` | `tests/test_session.py` |
| Session 支持增量 delta 与 consolidation | [minicode/session.py](../../../minicode/session.py) 的 `_save_delta`、`_consolidate_deltas` | `tests/test_session.py`、`tests/test_session_contract.py` |
| Session 支持 inspect/replay/resume | [minicode/session.py](../../../minicode/session.py)、[minicode/cli_commands.py](../../../minicode/cli_commands.py) | session/CLI tests |
| Memory 有 user/project/local scope | [minicode/memory.py](../../../minicode/memory.py) 的 `MemoryScope`、`MemoryPaths` | `tests/test_memory_integration.py`、`tests/test_memory_search_regression.py` |
| Memory 有四层 tier | [minicode/memory.py](../../../minicode/memory.py) 的 `MemoryTier` | memory tests |
| Memory 检索以 BM25 为主并叠加其他信号 | [minicode/memory.py](../../../minicode/memory.py) 的 `_bm25_score`、`search`、`_score_entry` | memory search/reranker tests |
| 相关 memory 按 token 预算注入 prompt | [minicode/memory.py](../../../minicode/memory.py) 的 `get_relevant_context`、[minicode/prompt.py](../../../minicode/prompt.py) | prompt/memory integration tests |
| Context 有 token 估算与压缩 | [minicode/context_manager.py](../../../minicode/context_manager.py)、[minicode/context_compactor.py](../../../minicode/context_compactor.py) | context tests |
| 压缩有 circuit breaker | [minicode/circuit_breaker.py](../../../minicode/circuit_breaker.py)、[minicode/agent_loop.py](../../../minicode/agent_loop.py) | `tests/test_circuit_breaker.py` |
| Turn Kernel 有 explore/execute/verify | [minicode/turn_kernel.py](../../../minicode/turn_kernel.py) 的 `derive_turn_step_policy` | `tests/test_turn_kernel.py` |
| 有 verification evidence guard | [minicode/turn_kernel.py](../../../minicode/turn_kernel.py) 的 `decide_assistant_turn` | `tests/test_turn_kernel.py` |
| single-deep 支持有条件 widening | [minicode/runtime_profiles.py](../../../minicode/runtime_profiles.py)、[minicode/turn_kernel.py](../../../minicode/turn_kernel.py) | turn kernel/runtime profile tests |
| MCP server 使用 stdio 且 lazy init | [minicode/mcp.py](../../../minicode/mcp.py) 的 `StdioMcpClient`、`create_mcp_backed_tools` | `tests/test_mcp.py` |
| Skill 从项目/用户/Claude 兼容目录发现 | [minicode/skills.py](../../../minicode/skills.py) | `tests/test_skills.py` |
| task 工具可启动隔离子 Agent | [minicode/tools/task.py](../../../minicode/tools/task.py) | tools/agent tests |
| CyberneticOrchestrator 管理多个 controller | [minicode/cybernetic_orchestrator.py](../../../minicode/cybernetic_orchestrator.py) | `tests/test_cybernetic_orchestrator.py` |
| Readiness 区分本地与 provider 状态 | [minicode/readiness.py](../../../minicode/readiness.py)、[minicode/product_surfaces.py](../../../minicode/product_surfaces.py) | readiness/release tests |
| 运行日志按大小轮转并可结构化 | [minicode/logging_config.py](../../../minicode/logging_config.py) | logging tests |

## 2. 主调用链索引

```text
pyproject.toml
  -> minicode.main:main
     -> load_runtime_config
     -> create_default_tool_registry
     -> PermissionManager
     -> create_model_adapter
     -> ContextManager / MemoryManager
     -> build_system_prompt_bundle
     -> run_tty_app
        -> tui.input_handler._handle_input
           -> background thread
              -> run_agent_turn
                 -> _model_next
                    -> AnthropicModelAdapter.next / OpenAIModelAdapter.next
                 -> ToolScheduler
                 -> _execute_single_tool
                    -> ToolRegistry.execute
                       -> concrete tool._run
                 -> decide_assistant_turn / decide_tool_turn
              -> TurnEventQueue
        -> refresh_tty_session_snapshot
        -> finalize_tty_session
```

## 3. 安全编辑调用链

```text
write_file / edit_file / patch_file
  -> resolve_tool_path
  -> apply_reviewed_file_change
     -> load_existing_file
     -> build_unified_diff
     -> PermissionManager.ensure_edit
     -> create_file_checkpoint
     -> target.write_text
```

回退链：

```text
/rewind-preview
  -> format_rewind_preview

/rewind
  -> rewind_session_data
     -> select checkpoints
     -> capture reverse checkpoints
     -> restore previous content or delete new file
     -> save_session(force_full=True)
```

## 4. Memory 调用链

```text
main / TUI input
  -> MemoryManager(project_root=cwd)
  -> load USER / PROJECT / LOCAL scopes

每次普通用户输入
  -> memory_manager.get_relevant_context(query=input_text)
     -> search
        -> BM25 + substring/tag/domain/usage/recency
        -> threshold + dedup + limit
     -> token budget formatting
  -> build_system_prompt_bundle
  -> dynamic memory section
```

任务完成后还可能经过 reflection 与 usage feedback，更新记忆价值。

## 5. Turn Kernel 状态索引

### Profile

| 字段 | single | single-deep |
| --- | ---: | ---: |
| max steps | 50 | 80 |
| empty response retries | 2 | 3 |
| thinking recovery retries | 3 | 5 |
| working memory TTL | 1800 秒 | 7200 秒 |
| strict verification | false | true |
| widen after step | 无 | 6 |
| widening step bonus | 0 | 6 |

### Stop Reason

定义见 `TurnStopReason`：

```text
done
max_steps
await_user
blocked
verification_failed
widen_needed
```

### Phase

定义见 `TurnStepPhase`：

```text
explore
execute
verify
```

## 6. 规模数据复现命令

在仓库根目录运行 PowerShell：

```powershell
$pkgFiles = Get-ChildItem -LiteralPath "minicode" -Recurse -File -Filter "*.py"
$testFiles = Get-ChildItem -LiteralPath "tests" -Recurse -File -Filter "*.py"

$pkgLines = ($pkgFiles | Get-Content | Measure-Object -Line).Lines
$testLines = ($testFiles | Get-Content | Measure-Object -Line).Lines

"minicode files: $($pkgFiles.Count), lines: $pkgLines"
"test files: $($testFiles.Count), lines: $testLines"
```

核心工具数：

```powershell
python -c "from minicode.tools import _CORE_TOOLS; print(len(_CORE_TOOLS)); print([t.name for t in _CORE_TOOLS])"
```

测试收集与执行：

```powershell
python -m pytest --collect-only -q
python -m pytest -q --import-mode=importlib
```

每次写简历前重新运行，不要把会变化的快照当永久事实。

## 7. 可以说，但要加边界的内容

### “项目有控制论闭环”

可以说：代码确实初始化 Orchestrator，并将 context/error/cost/progress 信号用于调节压缩、预算、并发和恢复。

必须补充：部分信号是启发式或估算，收益需要消融 benchmark 证明。

证据：`agent_loop.py` 中多处 `step * 2.0`、异常降级和 controller action；相关 `cybernetic_*` tests/benchmarks。

### “项目支持安全编辑”

可以说：有路径权限、危险命令审批、diff review、checkpoint、rewind。

必须补充：不是系统级 sandbox；用户授权命令后仍可能产生副作用；多文件写入当前也不是统一事务。

### “项目有长期记忆”

可以说：有本地持久条目、scope/tier、检索、注入和维护。

必须补充：这是 retrieval-augmented external memory，不是模型参数记忆，也不能保证记忆内容永远正确。

### “项目经过大量测试”

可以说：当前 `tests/` 有 82 个 Python 测试文件，README 记录一次 1311 passed/2 skipped。

必须补充：如果你没有在当前 commit 与当前环境复跑，就说明是仓库 CI/README 记录。

## 8. 不应宣称的内容

| 不应宣称 | 原因 |
| --- | --- |
| “Agent 绝不会误删文件” | 权限降低风险，但不是形式化安全保证 |
| “Verification 保证答案正确” | evidence guard 是启发式，最终依赖真实测试 |
| “控制论提升成功率 30%” | 除非有明确 benchmark、基线和统计结果 |
| “记忆相当于模型训练” | 实际是检索后注入 prompt |
| “所有 5 万行代码由我独立实现” | 必须与真实 Git/贡献事实一致 |
| “支持任意模型” | 只支持满足当前 adapter/protocol/tool-calling 合同的 provider |
| “MCP 是安全沙箱” | MCP 是协议和扩展面，不等同沙箱 |
| “checkpoint 替代 Git” | 它只解决 session 内细粒度恢复 |

## 9. 提交或面试前的证据包

建议为你自己的实际改动准备：

```text
1 个问题说明
1 张调用链图
1 个关键 diff
1 条失败测试
1 条修复后通过测试
1 个错误/恢复路径
1 段你对技术债的反思
```

这比记住几十个类名更能证明你真正理解并参与了项目。
