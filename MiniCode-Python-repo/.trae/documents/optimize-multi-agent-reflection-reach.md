# MiniCode 优化计划：多Agent编排 + 自我反思 + Agent Reach增强

## 1. 摘要

基于现有代码库，实施三项核心优化：
1. **多Agent编排系统集成** - 将现有的 `multi_agent/` 框架与 `agent_loop.py` 打通，通过 `/multi` 命令触发
2. **Agent自我反思循环** - 任务完成后自动反思执行过程，总结成功/失败原因并记录到长期记忆
3. **Agent Reach工具增强** - 确保 Reach Tools 正确注册到 ToolRegistry，并增强其功能

## 2. 当前状态分析

### 2.1 已存在且可用的模块

| 模块 | 文件路径 | 状态 | 说明 |
|------|---------|------|------|
| Multi-Agent框架 | `py-src/minicode/multi_agent/` | 骨架完整 | Orchestrator、5种Pattern、SharedMemory、MessageQueue、RoleAnalyzer、AdaptiveWorkflow 均已实现 |
| Reach Tools | `py-src/minicode/tools/reach_tools.py` | 已实现 | web_fetch_reach、web_search_reach、github_search、github_read、rss_read |
| 上下文隔离 | `py-src/minicode/context_isolation.py` | 已实现 | AgentContext、ContextSandbox（Token预算管理） |
| Agent智能 | `py-src/minicode/agent_intelligence.py` | 已实现 | ErrorClassifier、NudgeGenerator、ToolScheduler |
| 记忆注入 | `py-src/minicode/memory_injector.py` | 已实现 | MemoryInjector.inject_for_task() |
| 指标收集 | `py-src/minicode/agent_metrics.py` | 已实现 | AgentMetricsCollector、ToolHistoricalStats |

### 2.2 关键缺口

1. **multi_agent/ 未与 agent_loop 集成** - Orchestrator 的 `agent_factory` 是外部注入的，没有实际的 Agent 实现
2. **reach_tools 可能未注册** - 需要确认是否已加入默认 ToolRegistry
3. **缺少自我反思机制** - 没有任务完成后的自动反思和总结模块
4. **缺少 /multi 命令处理** - CLI 层没有多 Agent 模式的入口

## 3. 拟议变更

### 任务 1: 多Agent编排与Agent Loop集成

**目标**: 让现有的 `multi_agent/` 框架真正运行起来，通过 `/multi` 命令触发。

#### 1.1 创建 `multi_agent_agent.py` - 子Agent实现

**文件**: `py-src/minicode/multi_agent_agent.py`

将 `agent_loop.run_agent_turn` 包装为 multi_agent 系统可用的 Agent 实现：

```python
class MultiAgentWrapper:
    """Wraps a single agent_loop run as a multi-agent compatible agent."""
    
    def __init__(self, agent_id: str, role: AgentRole, 
                 model: ModelAdapter, tools: ToolRegistry,
                 shared_memory: SharedMemory, message_queue: MessageQueue,
                 context_sandbox: ContextSandbox):
        self.agent_id = agent_id
        self.role = role
        self.model = model
        self.tools = tools
        self.shared_memory = shared_memory
        self.message_queue = message_queue
        self.context_sandbox = context_sandbox
        self.context = context_sandbox.create_context(
            agent_type=role.name,
            allowed_tools=role.tools,
            max_tokens=40000,
        )
    
    def run(self, task: str) -> str:
        """Execute task using agent_loop.run_agent_turn."""
        # Build system prompt from role
        system_prompt = self._build_system_prompt()
        
        # Read shared memory for context
        shared_context = self._read_shared_context()
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{shared_context}\n\nTask: {task}"},
        ]
        
        # Run agent turn
        result_messages = run_agent_turn(
            model=self.model,
            tools=self.tools,
            messages=messages,
            cwd=self.context.cwd,
            max_steps=self.role.max_steps,
        )
        
        # Extract final output
        final_output = self._extract_final_output(result_messages)
        
        # Write result to shared memory
        self.shared_memory.write(
            f"result_{self.agent_id}",
            {"task": task, "output": final_output, "role": self.role.name},
            self.agent_id,
        )
        
        return final_output
    
    def _build_system_prompt(self) -> str:
        """Build system prompt from AgentRole."""
        parts = [
            f"You are a {self.role.name}.",
            f"Description: {self.role.description}",
            f"Expertise: {', '.join(self.role.expertise)}",
            f"Responsibilities: {', '.join(self.role.responsibilities)}",
            "\nWork independently and return your findings.",
        ]
        if self.role.system_prompt:
            parts.insert(1, self.role.system_prompt)
        return "\n".join(parts)
```

#### 1.2 修改 `orchestrator.py` - 集成到现有框架

**文件**: `py-src/minicode/multi_agent/orchestrator.py`

添加 `create_minicode_orchestrator()` 工厂函数：

```python
def create_minicode_orchestrator(
    model: ModelAdapter,
    tools: ToolRegistry,
    cwd: str = ".",
) -> Orchestrator:
    """Create an orchestrator pre-configured for MiniCode."""
    orchestrator = Orchestrator()
    
    def agent_factory(agent_id: str, role: AgentRole, 
                      shared_memory, message_queue):
        from minicode.context_isolation import ContextSandbox
        sandbox = ContextSandbox(total_token_budget=150000)
        return MultiAgentWrapper(
            agent_id=agent_id,
            role=role,
            model=model,
            tools=tools,
            shared_memory=shared_memory,
            message_queue=message_queue,
            context_sandbox=sandbox,
        )
    
    orchestrator.set_agent_factory(agent_factory)
    return orchestrator
```

#### 1.3 添加 `/multi` 命令处理

**文件**: `py-src/minicode/local_tool_shortcuts.py`

添加 `/multi` 命令解析：

```python
if user_input.startswith("/multi "):
    payload = user_input[len("/multi "):].strip()
    parts = payload.split(" ", 1)
    if len(parts) < 2:
        return None
    pattern, task = parts[0], parts[1]
    return {
        "toolName": "multi_agent_orchestrate",
        "input": {"pattern": pattern, "task": task},
    }
```

**文件**: 新增 `py-src/minicode/tools/multi_agent_tool.py`

创建多Agent编排工具：

```python
from minicode.multi_agent.orchestrator import create_minicode_orchestrator
from minicode.multi_agent.role_analyzer import RoleAnalyzer

def _multi_agent_validate(input_data: dict) -> dict:
    pattern = input_data.get("pattern", "sequential")
    task = input_data.get("task", "")
    if not task:
        raise ValueError("task is required")
    valid_patterns = ["sequential", "parallel", "hierarchical", "consensus", "tool_mediated"]
    if pattern not in valid_patterns:
        raise ValueError(f"pattern must be one of: {valid_patterns}")
    return {"pattern": pattern, "task": task, "max_roles": int(input_data.get("max_roles", 3))}

def _multi_agent_run(input_data: dict, context) -> ToolResult:
    from minicode.model_registry import create_model_adapter
    from minicode.config import load_runtime_config
    from minicode.tools import create_default_tool_registry
    
    runtime = load_runtime_config(context.cwd)
    tools = create_default_tool_registry(context.cwd, runtime=runtime)
    model = create_model_adapter(
        model=runtime.get("model", ""),
        tools=tools,
        runtime=runtime,
    )
    
    orchestrator = create_minicode_orchestrator(model, tools, context.cwd)
    
    trace = orchestrator.execute(
        task=input_data["task"],
        pattern=input_data["pattern"],
        max_roles=input_data["max_roles"],
    )
    
    # Format results
    lines = [
        f"Multi-Agent Execution Complete",
        f"Pattern: {trace.pattern}",
        f"Duration: {trace.duration_ms:.0f}ms",
        f"Agents: {len(trace.agent_results)}",
        "=" * 60,
        "",
    ]
    
    for result in trace.agent_results:
        lines.extend([
            f"Agent: {result.agent_id} ({result.role})",
            f"Status: {result.status.value}",
            f"Output: {result.output[:500]}..." if len(result.output) > 500 else f"Output: {result.output}",
            "",
        ])
    
    return ToolResult(ok=True, output="\n".join(lines))
```

### 任务 2: Agent自我反思循环

**目标**: 任务完成后自动反思执行过程，总结成功/失败原因并记录到长期记忆。

#### 2.1 创建 `agent_reflection.py`

**文件**: `py-src/minicode/agent_reflection.py`

```python
"""Agent self-reflection system.

Provides post-task reflection to improve future performance:
- Success/failure analysis
- Strategy effectiveness review
- Error pattern recognition
- Memory recording for future reference
"""

from dataclasses import dataclass, field
from typing import Any
import time

from minicode.logging_config import get_logger
from minicode.memory import MemoryManager, MemoryScope
from minicode.agent_metrics import AgentMetricsCollector

logger = get_logger("agent_reflection")


@dataclass
class ReflectionResult:
    """Result of a reflection cycle."""
    task_summary: str
    success: bool
    key_decisions: list[str]
    errors_encountered: list[str]
    lessons_learned: list[str]
    suggested_improvements: list[str]
    confidence: float  # 0.0 - 1.0
    timestamp: float = field(default_factory=time.time)
    
    def to_memory_entry(self) -> dict:
        """Convert to a memory entry for persistence."""
        return {
            "content": self._format_content(),
            "category": "reflection",
            "tags": ["self-reflection", "lessons-learned"] + 
                    (["success"] if self.success else ["failure"]),
            "metadata": {
                "confidence": self.confidence,
                "key_decisions": self.key_decisions,
                "errors": self.errors_encountered,
                "improvements": self.suggested_improvements,
            },
        }
    
    def _format_content(self) -> str:
        parts = [
            f"Task Reflection (Success: {self.success})",
            f"Summary: {self.task_summary}",
            "",
            "Key Decisions:",
        ]
        for d in self.key_decisions:
            parts.append(f"  - {d}")
        
        if self.errors_encountered:
            parts.extend(["", "Errors Encountered:"])
            for e in self.errors_encountered:
                parts.append(f"  - {e}")
        
        parts.extend(["", "Lessons Learned:"])
        for l in self.lessons_learned:
            parts.append(f"  - {l}")
        
        if self.suggested_improvements:
            parts.extend(["", "Suggested Improvements:"])
            for i in self.suggested_improvements:
                parts.append(f"  - {i}")
        
        return "\n".join(parts)


class ReflectionEngine:
    """Engine for agent self-reflection."""
    
    def __init__(
        self,
        memory_manager: MemoryManager | None = None,
        min_confidence_threshold: float = 0.5,
    ):
        self.memory = memory_manager
        self.min_confidence = min_confidence_threshold
    
    def reflect(
        self,
        task_description: str,
        execution_trace: list[dict[str, Any]],
        metrics: AgentMetricsCollector | None = None,
    ) -> ReflectionResult:
        """Generate reflection from execution trace.
        
        Args:
            task_description: Original task
            execution_trace: List of step records (tool calls, responses, errors)
            metrics: Optional metrics collector for performance data
            
        Returns:
            Reflection result
        """
        # Analyze execution trace
        tool_calls = [s for s in execution_trace if s.get("type") == "tool_call"]
        errors = [s for s in execution_trace if s.get("type") == "error"]
        assistant_msgs = [s for s in execution_trace if s.get("type") == "assistant"]
        
        # Determine success
        success = len(errors) == 0 and len(assistant_msgs) > 0
        
        # Extract key decisions (assistant messages that contain decisions)
        key_decisions = self._extract_decisions(assistant_msgs)
        
        # Extract errors
        error_list = [e.get("content", "Unknown error") for e in errors]
        
        # Generate lessons
        lessons = self._generate_lessons(tool_calls, errors, success)
        
        # Generate improvements
        improvements = self._generate_improvements(tool_calls, errors, metrics)
        
        # Calculate confidence
        confidence = self._calculate_confidence(success, len(errors), len(tool_calls))
        
        reflection = ReflectionResult(
            task_summary=task_description[:200],
            success=success,
            key_decisions=key_decisions,
            errors_encountered=error_list,
            lessons_learned=lessons,
            suggested_improvements=improvements,
            confidence=confidence,
        )
        
        # Persist to memory
        if self.memory and confidence >= self.min_confidence:
            self._persist_reflection(reflection)
        
        return reflection
    
    def _extract_decisions(self, assistant_msgs: list[dict]) -> list[str]:
        """Extract key decisions from assistant messages."""
        decisions = []
        for msg in assistant_msgs:
            content = msg.get("content", "")
            # Look for decision indicators
            if any(kw in content.lower() for kw in ["decide", "choose", "select", "use ", "will "]):
                # Extract first sentence as decision
                first_sentence = content.split(".")[0].strip()
                if len(first_sentence) > 10:
                    decisions.append(first_sentence[:200])
        return decisions[:5]  # Limit to top 5
    
    def _generate_lessons(
        self,
        tool_calls: list[dict],
        errors: list[dict],
        success: bool,
    ) -> list[str]:
        """Generate lessons learned from execution."""
        lessons = []
        
        if success:
            lessons.append("Task completed successfully with the chosen approach.")
        else:
            lessons.append("Task encountered errors. Review error patterns for future avoidance.")
        
        # Tool usage patterns
        tool_names = [t.get("tool_name", "unknown") for t in tool_calls]
        if tool_names:
            unique_tools = set(tool_names)
            lessons.append(f"Used {len(unique_tools)} unique tool(s): {', '.join(unique_tools)}.")
        
        # Error patterns
        if errors:
            error_tools = set(e.get("tool_name", "unknown") for e in errors)
            lessons.append(f"Errors occurred with tool(s): {', '.join(error_tools)}. Consider alternative approaches.")
        
        return lessons
    
    def _generate_improvements(
        self,
        tool_calls: list[dict],
        errors: list[dict],
        metrics: AgentMetricsCollector | None,
    ) -> list[str]:
        """Generate improvement suggestions."""
        improvements = []
        
        if len(errors) > 2:
            improvements.append("High error rate detected. Consider breaking task into smaller steps.")
        
        if len(tool_calls) > 10:
            improvements.append("Many tool calls used. Consider more efficient approaches or better planning.")
        
        if metrics:
            stats = metrics.get_summary()
            if stats.get("overall_success_rate", 1.0) < 0.7:
                improvements.append("Low success rate. Review tool usage patterns and error recovery strategies.")
        
        return improvements
    
    def _calculate_confidence(
        self,
        success: bool,
        error_count: int,
        tool_count: int,
    ) -> float:
        """Calculate reflection confidence score."""
        base = 0.8 if success else 0.4
        error_penalty = min(error_count * 0.1, 0.3)
        tool_bonus = min(tool_count * 0.02, 0.1)
        return max(0.0, min(1.0, base - error_penalty + tool_bonus))
    
    def _persist_reflection(self, reflection: ReflectionResult) -> None:
        """Save reflection to long-term memory."""
        if self.memory is None:
            return
        
        entry = reflection.to_memory_entry()
        try:
            self.memory.add(
                content=entry["content"],
                scope=MemoryScope.PROJECT,
                category=entry["category"],
                tags=entry["tags"],
                metadata=entry["metadata"],
            )
            logger.info("Reflection persisted to memory (confidence: %.2f)", reflection.confidence)
        except Exception as e:
            logger.warning("Failed to persist reflection: %s", e)
```

#### 2.2 修改 `agent_loop.py` - 集成反思

在 `run_agent_turn` 返回前添加反思调用：

```python
# In run_agent_turn, before return:
# ... existing code ...

# After successful completion, trigger reflection
if reflection_engine is not None:
    try:
        reflection = reflection_engine.reflect(
            task_description=_extract_task_description(current_messages),
            execution_trace=_build_execution_trace(current_messages),
            metrics=metrics_collector,
        )
        logger.info("Reflection generated: success=%s, confidence=%.2f", 
                   reflection.success, reflection.confidence)
    except Exception as e:
        logger.warning("Reflection failed: %s", e)

return current_messages
```

### 任务 3: Agent Reach工具增强

**目标**: 确保 Reach Tools 正确注册，并增强功能。

#### 3.1 确认/修复工具注册

**文件**: `py-src/minicode/tools/__init__.py`

确保 reach_tools 已导入：

```python
from minicode.tools.reach_tools import (
    web_fetch_reach_tool,
    web_search_reach_tool,
    github_search_tool,
    github_read_tool,
    rss_read_tool,
    get_reach_tools,
)

# In create_default_tool_registry():
def create_default_tool_registry(cwd: str, runtime: dict | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    
    # ... existing tools ...
    
    # Reach tools
    for tool in get_reach_tools():
        registry.register(tool)
    
    return registry
```

#### 3.2 增强 Reach Tools

**文件**: `py-src/minicode/tools/reach_tools.py`

添加缓存和重试机制：

```python
import functools
import time

# Simple in-memory cache for reach tools
_reach_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 300  # 5 minutes

def _get_cached(key: str) -> str | None:
    if key in _reach_cache:
        value, timestamp = _reach_cache[key]
        if time.time() - timestamp < _CACHE_TTL:
            return value
        del _reach_cache[key]
    return None

def _set_cached(key: str, value: str) -> None:
    _reach_cache[key] = (value, time.time())

# Add retry decorator
def _with_retry(max_retries=2, delay=1.0):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except urllib.error.URLError as e:
                    if attempt < max_retries:
                        time.sleep(delay * (attempt + 1))
                        continue
                    raise
            return None
        return wrapper
    return decorator
```

### 任务 4: 测试套件

#### 4.1 多Agent集成测试

**文件**: `py-src/tests/test_multi_agent_integration.py`

```python
"""Integration tests for multi-agent orchestration."""

import sys
sys.path.insert(0, r"d:\Desktop\minicode\py-src")

from minicode.multi_agent.orchestrator import Orchestrator
from minicode.multi_agent.patterns import SequentialPattern, ParallelPattern
from minicode.multi_agent.types import AgentRole
from minicode.multi_agent.shared_memory import SharedMemory
from minicode.multi_agent.message_queue import MessageQueue


class MockAgent:
    """Mock agent for testing."""
    def __init__(self, agent_id, role, shared_memory, message_queue):
        self.agent_id = agent_id
        self.role = role
        self.shared_memory = shared_memory
        self.message_queue = message_queue
    
    def run(self, task: str) -> str:
        # Write to shared memory
        self.shared_memory.write(
            f"result_{self.agent_id}",
            {"task": task, "agent": self.agent_id},
            self.agent_id,
        )
        return f"[{self.agent_id}] Completed: {task}"


def test_sequential_pattern():
    """Test sequential execution."""
    pattern = SequentialPattern()
    roles = [
        AgentRole(name="researcher", description="Research task"),
        AgentRole(name="writer", description="Write report"),
    ]
    
    trace = pattern.execute("Test task", roles, 
                           lambda id, role, sm, mq: MockAgent(id, role, sm, mq))
    
    assert len(trace.agent_results) == 2
    assert trace.agent_results[0].status.value == "completed"
    assert trace.agent_results[1].status.value == "completed"
    print("✓ Sequential pattern test passed")


def test_parallel_pattern():
    """Test parallel execution."""
    pattern = ParallelPattern()
    roles = [
        AgentRole(name="analyzer1", description="Analyze part 1"),
        AgentRole(name="analyzer2", description="Analyze part 2"),
    ]
    
    trace = pattern.execute("Test task", roles,
                           lambda id, role, sm, mq: MockAgent(id, role, sm, mq))
    
    assert len(trace.agent_results) == 2
    assert all(r.status.value == "completed" for r in trace.agent_results)
    print("✓ Parallel pattern test passed")


def test_orchestrator_integration():
    """Test full orchestrator with mock factory."""
    orchestrator = Orchestrator()
    orchestrator.set_agent_factory(
        lambda id, role, sm, mq: MockAgent(id, role, sm, mq)
    )
    
    trace = orchestrator.execute(
        task="Analyze this codebase",
        pattern="sequential",
        max_roles=2,
    )
    
    assert trace.pattern == "sequential"
    assert len(trace.agent_results) > 0
    print("✓ Orchestrator integration test passed")


if __name__ == "__main__":
    test_sequential_pattern()
    test_parallel_pattern()
    test_orchestrator_integration()
    print("\nAll multi-agent integration tests passed!")
```

#### 4.2 反思系统测试

**文件**: `py-src/tests/test_reflection.py`

```python
"""Tests for agent reflection system."""

import sys
sys.path.insert(0, r"d:\Desktop\minicode\py-src")

from minicode.agent_reflection import ReflectionEngine, ReflectionResult


def test_reflection_success():
    """Test reflection on successful execution."""
    engine = ReflectionEngine()
    
    trace = [
        {"type": "assistant", "content": "I will analyze the codebase structure."},
        {"type": "tool_call", "tool_name": "list_files"},
        {"type": "tool_call", "tool_name": "read_file"},
        {"type": "assistant", "content": "Analysis complete. The project uses a modular architecture."},
    ]
    
    reflection = engine.reflect("Analyze project structure", trace)
    
    assert reflection.success is True
    assert reflection.confidence > 0.5
    assert len(reflection.key_decisions) > 0
    assert len(reflection.lessons_learned) > 0
    print("✓ Success reflection test passed")


def test_reflection_with_errors():
    """Test reflection on failed execution."""
    engine = ReflectionEngine()
    
    trace = [
        {"type": "assistant", "content": "I will run the tests."},
        {"type": "tool_call", "tool_name": "run_command"},
        {"type": "error", "content": "Command failed: pytest not found", "tool_name": "run_command"},
        {"type": "error", "content": "Alternative approach also failed", "tool_name": "run_command"},
    ]
    
    reflection = engine.reflect("Run test suite", trace)
    
    assert reflection.success is False
    assert len(reflection.errors_encountered) == 2
    assert len(reflection.suggested_improvements) > 0
    print("✓ Error reflection test passed")


def test_reflection_to_memory():
    """Test reflection memory entry format."""
    reflection = ReflectionResult(
        task_summary="Test task",
        success=True,
        key_decisions=["Used grep to find patterns"],
        errors_encountered=[],
        lessons_learned=["Grep is efficient for pattern matching"],
        suggested_improvements=[],
        confidence=0.9,
    )
    
    entry = reflection.to_memory_entry()
    
    assert entry["category"] == "reflection"
    assert "self-reflection" in entry["tags"]
    assert "success" in entry["tags"]
    assert entry["metadata"]["confidence"] == 0.9
    print("✓ Memory entry test passed")


if __name__ == "__main__":
    test_reflection_success()
    test_reflection_with_errors()
    test_reflection_to_memory()
    print("\nAll reflection tests passed!")
```

#### 4.3 Reach Tools 集成测试

**文件**: `py-src/tests/test_reach_integration.py`

```python
"""Integration tests for Agent Reach tools."""

import sys
sys.path.insert(0, r"d:\Desktop\minicode\py-src")

from minicode.tools.reach_tools import (
    web_fetch_reach_tool,
    web_search_reach_tool,
    github_search_tool,
    get_reach_tools,
)
from minicode.tooling import ToolContext


def test_reach_tools_registered():
    """Test that all reach tools are available."""
    tools = get_reach_tools()
    tool_names = [t.name for t in tools]
    
    assert "web_fetch_reach" in tool_names
    assert "web_search_reach" in tool_names
    assert "github_search" in tool_names
    assert "github_read" in tool_names
    assert "rss_read" in tool_names
    print(f"✓ All {len(tools)} reach tools registered")


def test_github_search_validation():
    """Test GitHub search input validation."""
    # Valid input
    result = github_search_tool.validator({"query": "python web framework"})
    assert result["query"] == "python web framework"
    
    # Invalid - missing query
    try:
        github_search_tool.validator({})
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
    
    print("✓ GitHub search validation test passed")


def test_web_fetch_validation():
    """Test web fetch input validation."""
    # Valid input
    result = web_fetch_reach_tool.validator({"url": "https://example.com"})
    assert result["url"] == "https://example.com"
    
    # Invalid URL
    try:
        web_fetch_reach_tool.validator({"url": "not-a-url"})
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
    
    print("✓ Web fetch validation test passed")


if __name__ == "__main__":
    test_reach_tools_registered()
    test_github_search_validation()
    test_web_fetch_validation()
    print("\nAll reach integration tests passed!")
```

## 4. 假设与决策

### 4.1 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 多Agent触发方式 | `/multi <pattern> <task>` 命令 | 用户显式控制，避免误触发 |
| 反思触发时机 | 任务完成后 | 有完整上下文，不影响执行性能 |
| 子Agent实现 | 复用 `agent_loop.run_agent_turn` | 保持一致性，减少代码重复 |
| 上下文隔离 | 使用现有 `ContextSandbox` | Token预算管理已就绪 |
| 记忆持久化 | 复用现有 `MemoryManager` | 自动分类和标签已支持 |

### 4.2 依赖假设

1. `agent_loop.run_agent_turn` 的签名保持不变
2. `ToolRegistry` 支持动态注册工具
3. `MemoryManager` 支持 PROJECT scope 写入
4. 网络访问可用于 Reach Tools（Jina AI、GitHub API）

## 5. 验证步骤

### 5.1 功能验证

```bash
# 1. 运行多Agent测试
cd d:\Desktop\minicode\py-src
python tests/test_multi_agent_integration.py

# 2. 运行反思测试
python tests/test_reflection.py

# 3. 运行Reach工具测试
python tests/test_reach_integration.py

# 4. 运行现有测试确保无回归
python tests/test_reach_tools.py
python tests/test_multi_agent.py
```

### 5.2 集成验证

```bash
# 测试 /multi 命令解析
python -c "
from minicode.local_tool_shortcuts import parse_local_tool_shortcut
result = parse_local_tool_shortcut('/multi sequential analyze project')
print(result)
"

# 测试反思引擎
python -c "
from minicode.agent_reflection import ReflectionEngine
engine = ReflectionEngine()
trace = [
    {'type': 'assistant', 'content': 'I will analyze the code.'},
    {'type': 'tool_call', 'tool_name': 'list_files'},
]
reflection = engine.reflect('Test task', trace)
print(f'Success: {reflection.success}, Confidence: {reflection.confidence}')
"
```

## 6. 文件变更清单

### 新增文件
- `py-src/minicode/multi_agent_agent.py` - 子Agent包装器
- `py-src/minicode/agent_reflection.py` - 自我反思引擎
- `py-src/minicode/tools/multi_agent_tool.py` - /multi 命令工具
- `py-src/tests/test_multi_agent_integration.py` - 多Agent集成测试
- `py-src/tests/test_reflection.py` - 反思系统测试
- `py-src/tests/test_reach_integration.py` - Reach工具集成测试

### 修改文件
- `py-src/minicode/multi_agent/orchestrator.py` - 添加 `create_minicode_orchestrator()`
- `py-src/minicode/local_tool_shortcuts.py` - 添加 `/multi` 命令解析
- `py-src/minicode/agent_loop.py` - 集成反思引擎
- `py-src/minicode/tools/__init__.py` - 确保 reach_tools 注册
- `py-src/minicode/tools/reach_tools.py` - 添加缓存和重试

## 7. 回滚计划

如果出现问题：
1. 新增文件可直接删除
2. 修改文件使用 git 回滚
3. `/multi` 命令未注册时不会影响现有功能
4. 反思引擎异常被捕获，不会中断主流程
