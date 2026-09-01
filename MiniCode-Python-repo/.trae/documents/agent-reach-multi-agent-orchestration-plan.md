# MiniCode 优化计划：Agent Reach + Multi-Agent Orchestration

## 1. 摘要

本计划为 MiniCode 项目引入两大核心能力：
1. **Agent Reach** — 让 MiniCode Agent 能够访问外部平台（Web、GitHub、Twitter、Reddit、YouTube、RSS 等），大幅扩展信息获取边界
2. **Multi-Agent Orchestration** — 实现多智能体协作编排系统，支持顺序、并行、层级、共识、工具中介五种编排模式，以及自适应工作流

## 2. 当前状态分析

### 2.1 已有架构
- **Agent Loop** (`agent_loop.py`): 核心循环，已集成 metrics、error classification、tool scheduling
- **Memory System** (`memory.py`, `memory_injector.py`): 三层记忆体系（User/Project/Local），BM25 搜索
- **Tool System** (`tooling.py`, `tools/`): 模块化工具注册和执行
- **MCP Integration** (`mcp.py`): 外部 MCP Server 支持
- **Metrics** (`agent_metrics.py`): 性能指标收集
- **Headless Mode** (`headless.py`, `gateway.py`): 非交互式执行和 HTTP 网关

### 2.2 已实现的优化
- 记忆系统优化（BM25、CJK 分词、自动分类）
- Agent Loop 智能化（Error Classification、Nudge Generation、Tool Scheduling）
- 集群压力测试

### 2.3 缺失的能力
- **外部平台访问**: Agent 无法主动获取互联网信息
- **多 Agent 协作**: 单 Agent 处理复杂任务能力有限
- **动态角色分配**: 没有基于任务自动分配角色的机制
- **Agent 间通信**: 缺乏标准化的 Agent 间消息传递机制

## 3. 提议的变更

### 3.1 Phase 1: Agent Reach 集成

#### 3.1.1 内置工具（高频平台）

**文件**: `py-src/minicode/tools/reach_tools.py`

实现以下内置工具：

| 工具名 | 功能 | 依赖 |
|--------|------|------|
| `web_fetch` | 抓取任意网页并转为 Markdown | `requests` + `html2text` |
| `web_search` | 网页搜索（Jina AI Search） | `requests` |
| `github_search` | 搜索 GitHub 仓库/代码/Issues | `requests` + GitHub API |
| `github_read` | 读取 GitHub 仓库文件内容 | `requests` + GitHub API |
| `rss_read` | 读取 RSS 订阅源 | `feedparser` |

**设计原则**:
- 使用标准库 + 最小依赖（`requests`, `feedparser`）
- 遵循现有 ToolDefinition 接口
- 支持异步执行（兼容 ToolScheduler）
- 结果自动截断（遵循 `_smart_truncate_output`）

#### 3.1.2 MCP Server（扩展平台）

**文件**: `py-src/minicode/mcp_servers/reach_mcp_server.py`

实现 MCP Server 封装以下平台：

| 平台 | 功能 | 认证方式 |
|------|------|----------|
| Twitter/X | 搜索推文、读取时间线 | Cookie |
| Reddit | 读取 Subreddit、搜索帖子 | 无需认证（只读） |
| YouTube | 获取视频元数据、字幕 | `yt-dlp` |
| Bilibili | 获取视频信息 | `yt-dlp` |
| 小红书 | 搜索笔记、读取详情 | Cookie |
| LinkedIn | 读取个人资料 | Cookie |
| Boss直聘 | 搜索职位 | Cookie |

**MCP Server 设计**:
- 基于现有 `mcp.py` 的 JsonRpcProtocol
- 每个平台作为一个独立的 MCP Tool
- 配置通过 `.mcp.json` 或环境变量
- 支持代理配置

#### 3.1.3 配置集成

**文件**: `py-src/minicode/config.py` (扩展)

新增配置项：
```python
REACH_CONFIG = {
    "proxy": None,  # 代理地址
    "timeout": 30,  # 请求超时
    "platforms": {
        "github": {"token": None},
        "twitter": {"cookies": None},
        "youtube": {"cookies_from_browser": None},
    }
}
```

### 3.2 Phase 2: Multi-Agent Orchestration

#### 3.2.1 核心架构

**文件**: `py-src/minicode/multi_agent/orchestrator.py`

```
User Request
    ↓
Orchestrator (编排器)
    ├→ Role Analyzer (角色分析器) → 动态角色分配
    ├→ Agent 1 (Specialist) → Task 1
    ├→ Agent 2 (Specialist) → Task 2
    ├→ Agent 3 (Specialist) → Task 3
    ↓
Result Aggregator (结果聚合器)
    ↓
Final Response
```

#### 3.2.2 动态角色生成

**文件**: `py-src/minicode/multi_agent/role_analyzer.py`

基于任务描述自动生成角色：

```python
class RoleAnalyzer:
    """Analyzes task description and generates optimal agent roles."""
    
    def analyze(self, task: str) -> list[AgentRole]:
        # 1. 提取任务关键词
        # 2. 匹配角色模板库
        # 3. 生成自定义角色（如需要）
        # 4. 返回角色列表和任务分配
```

**内置角色模板**:
- `ResearchAgent`: 信息收集、搜索、分析
- `CoderAgent`: 代码编写、重构、调试
- `TesterAgent`: 测试用例生成、执行、报告
- `ReviewerAgent`: 代码审查、文档检查
- `ArchitectAgent`: 架构设计、技术选型
- `DevOpsAgent`: 部署、CI/CD、基础设施

#### 3.2.3 编排模式实现

**文件**: `py-src/minicode/multi_agent/patterns.py`

实现五种编排模式：

1. **SequentialPattern** (`sequential`)
   - Agent 按顺序执行
   - 每个 Agent 的输出作为下一个 Agent 的输入
   - 适用：有依赖关系的任务链

2. **ParallelPattern** (`parallel`)
   - 多个 Agent 同时执行
   - 结果聚合后返回
   - 适用：独立子任务

3. **HierarchicalPattern** (`hierarchical`)
   - Manager Agent 协调多个 Worker Agent
   - Manager 负责任务分配和结果审核
   - 适用：复杂项目需要 oversight

4. **ConsensusPattern** (`consensus`)
   - 多个 Agent 独立分析同一问题
   - 通过投票或讨论达成共识
   - 适用：关键决策、风险评估

5. **ToolMediatedPattern** (`tool_mediated`)
   - Agent 通过共享工具/数据库间接协作
   - 最小化直接通信
   - 适用：大规模系统

#### 3.2.4 共享内存 + 消息队列

**文件**: `py-src/minicode/multi_agent/shared_memory.py`

```python
class SharedMemory:
    """Shared memory for inter-agent communication."""
    
    def write(self, key: str, value: Any, agent_id: str) -> None
    def read(self, key: str) -> Any
    def subscribe(self, key: str, callback: Callable) -> None
    def get_history(self, agent_id: str | None = None) -> list[MemoryEvent]
```

**文件**: `py-src/minicode/multi_agent/message_queue.py`

```python
class MessageQueue:
    """Asynchronous message queue for agent communication."""
    
    def send(self, to: str, message: AgentMessage) -> None
    def receive(self, agent_id: str, timeout: float | None = None) -> AgentMessage | None
    def broadcast(self, message: AgentMessage) -> None
```

#### 3.2.5 自适应工作流

**文件**: `py-src/minicode/multi_agent/adaptive_workflow.py`

```python
class AdaptiveWorkflow:
    """Dynamically adjusts workflow based on execution progress."""
    
    def monitor(self, execution_trace: ExecutionTrace) -> WorkflowAdjustment:
        # 1. 监控执行进度
        # 2. 检测瓶颈（慢 Agent、错误率高）
        # 3. 动态调整：
        #    - 增加资源（添加 Specialist Agent）
        #    - 重新分配任务
        #    - 插入验证步骤
```

### 3.3 Phase 3: 集成与测试

#### 3.3.1 Agent Loop 集成

**文件**: `py-src/minicode/agent_loop.py` (修改)

在 `run_agent_turn` 中集成 Multi-Agent 支持：

```python
def run_agent_turn(
    ...,
    multi_agent_mode: bool = False,
    orchestrator: Orchestrator | None = None,
) -> list[ChatMessage]:
    if multi_agent_mode and orchestrator:
        return orchestrator.execute(task, model, tools)
    # ... existing logic
```

#### 3.3.2 CLI 集成

**文件**: `py-src/minicode/cli_commands.py` (扩展)

新增命令：
- `/reach <platform> <query>` — 使用 Agent Reach 查询外部平台
- `/multi-agent <task>` — 启动多 Agent 协作模式
- `/orchestrate <pattern> <task>` — 使用指定编排模式

#### 3.3.3 测试计划

**文件**: `py-src/tests/test_reach_tools.py`
- 测试每个 Reach Tool 的基本功能
- 测试错误处理（网络错误、超时）
- 测试结果截断

**文件**: `py-src/tests/test_multi_agent.py`
- 测试每种编排模式
- 测试动态角色生成
- 测试共享内存和消息队列
- 测试自适应工作流

**文件**: `py-src/tests/test_multi_agent_integration.py`
- 端到端测试：完整多 Agent 任务
- 性能测试：并发执行效率
- 错误恢复测试

## 4. 假设与决策

### 4.1 技术决策

| 决策 | 选择 | 理由 |
|------|------|------|
| Agent Reach 实现方式 | 混合模式 | 高频平台内置（低延迟），扩展平台 MCP（灵活） |
| Multi-Agent 通信 | 共享内存 + 消息队列 | 与现有 memory.py 架构一致，支持持久化 |
| 角色分配 | 动态生成 | 更灵活，无需预定义所有角色 |
| 编排模式 | 五种 + 自适应 | 覆盖绝大多数场景 |
| 外部依赖 | requests, feedparser, yt-dlp | 最小化依赖，标准库优先 |

### 4.2 假设

1. 用户有稳定的网络连接（Agent Reach 需要）
2. 目标平台 API 保持稳定
3. 多 Agent 执行时上下文窗口足够（或启用自动压缩）
4. 用户愿意配置平台认证信息（Cookie/Token）

## 5. 验证步骤

### 5.1 Agent Reach 验证

- [ ] `web_fetch` 能成功抓取网页并转为 Markdown
- [ ] `web_search` 能返回搜索结果
- [ ] `github_search` 能搜索 GitHub 仓库
- [ ] `rss_read` 能解析 RSS 订阅源
- [ ] MCP Server 能正确注册到 MiniCode
- [ ] 至少 3 个扩展平台（Twitter/Reddit/YouTube）能正常工作

### 5.2 Multi-Agent 验证

- [ ] Sequential 模式：Agent A 输出作为 Agent B 输入
- [ ] Parallel 模式：3 个 Agent 同时执行，结果聚合
- [ ] Hierarchical 模式：Manager 协调 2 个 Worker
- [ ] Consensus 模式：3 个 Agent 对同一问题达成共识
- [ ] ToolMediated 模式：Agent 通过共享内存协作
- [ ] 自适应工作流：检测到慢 Agent 后自动调整
- [ ] 动态角色生成：根据任务描述生成合适角色

### 5.3 集成验证

- [ ] CLI 命令 `/reach` 和 `/multi-agent` 可用
- [ ] Agent Loop 在 multi_agent_mode=True 时正确调用 Orchestrator
- [ ] 所有测试通过（单元测试 + 集成测试）
- [ ] 文档完整（架构说明 + 使用指南）

## 6. 文件变更清单

### 新增文件

```
py-src/minicode/tools/reach_tools.py          # 内置 Reach 工具
py-src/minicode/mcp_servers/reach_mcp_server.py  # Reach MCP Server
py-src/minicode/multi_agent/
  ├── __init__.py
  ├── orchestrator.py       # 核心编排器
  ├── role_analyzer.py      # 动态角色分析
  ├── patterns.py           # 五种编排模式
  ├── shared_memory.py      # 共享内存
  ├── message_queue.py      # 消息队列
  ├── adaptive_workflow.py  # 自适应工作流
  └── types.py              # 类型定义
py-src/tests/test_reach_tools.py
py-src/tests/test_multi_agent.py
py-src/tests/test_multi_agent_integration.py
py-src/docs/AGENT_REACH_GUIDE.md
py-src/docs/MULTI_AGENT_GUIDE.md
```

### 修改文件

```
py-src/minicode/agent_loop.py      # 集成 multi_agent_mode
py-src/minicode/cli_commands.py    # 新增 /reach, /multi-agent 命令
py-src/minicode/config.py          # 新增 Reach 配置
py-src/minicode/tooling.py         # 注册 Reach Tools
py-src/minicode/mcp.py             # 支持 Reach MCP Server
```

## 7. 风险评估

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 外部平台 API 变更 | 高 | 封装抽象层，隔离平台差异 |
| 网络不稳定 | 中 | 实现重试机制和超时控制 |
| 多 Agent 上下文溢出 | 高 | 启用自动压缩，限制 Agent 数量 |
| 平台认证失效 | 中 | 提供清晰的错误提示和配置指南 |
| 性能下降 | 中 | 并行执行，缓存结果，压力测试 |

## 8. 时间线

| 阶段 | 任务 | 预计时间 |
|------|------|----------|
| Phase 1 | Agent Reach 内置工具 | 2-3 小时 |
| Phase 1 | Agent Reach MCP Server | 2-3 小时 |
| Phase 2 | Multi-Agent 核心架构 | 3-4 小时 |
| Phase 2 | 编排模式实现 | 3-4 小时 |
| Phase 2 | 共享内存 + 消息队列 | 2-3 小时 |
| Phase 3 | 集成与测试 | 2-3 小时 |
| Phase 3 | 文档 | 1-2 小时 |
| **总计** | | **15-22 小时** |
