# Agent Loop 智能演化规范

## Why

Agent Loop 是 MiniCode 的核心中枢，当前实现虽然稳定但缺乏"智能"——它机械地执行工具调用，不会在失败时学习，不会根据历史表现调整策略，也不会利用新优化的记忆系统来改进决策。上一轮优化使记忆系统具备了更好的搜索和分类能力，但 Agent Loop 尚未充分利用这些能力。

本轮优化的目标是让 Agent Loop 从"执行器"进化为"智能调度器"：
1. 利用记忆系统学习历史执行模式
2. 根据上下文自适应调整执行策略
3. 在失败时智能恢复而非简单重试
4. 收集性能指标用于持续优化

## What Changes

- **智能工具调度**: 基于历史成功率动态调整工具执行顺序和并发策略
- **自适应错误恢复**: 根据错误类型自动选择重试/替代/降级策略
- **记忆驱动的上下文注入**: 在 Agent Loop 中自动注入相关记忆到上下文
- **执行模式自适应**: 根据任务类型自动切换串行/并行执行模式
- **性能指标收集**: 收集每轮执行时间、token 消耗、成功率等指标
- **智能 Nudge 生成**: 根据失败原因生成针对性的继续提示

## Impact

- Affected specs: Agent Loop, Memory System, Context Management, Tool System
- Affected code: `py-src/minicode/agent_loop.py`, `py-src/minicode/memory.py`, `py-src/minicode/context_manager.py`, `py-src/minicode/state.py`

## ADDED Requirements

### Requirement: 智能工具调度
系统 SHALL 根据工具历史表现动态调整执行策略。

#### Scenario: 高成功率工具优先
- **WHEN** 多个工具可以并行执行
- **THEN** 历史成功率高的工具优先执行，失败率高的工具延后或改为串行

#### Scenario: 动态并发控制
- **WHEN** 工具执行历史显示某类工具经常相互干扰
- **THEN** 系统自动降低这些工具的并发度

### Requirement: 自适应错误恢复
系统 SHALL 根据错误类型和上下文自动选择恢复策略。

#### Scenario: 网络错误自动重试
- **WHEN** 工具执行因网络问题失败
- **THEN** 系统自动重试，使用指数退避策略

#### Scenario: 权限错误降级
- **WHEN** 工具因权限不足失败
- **THEN** 系统尝试使用低权限替代方案或请求用户授权

#### Scenario: 资源不足等待
- **WHEN** 工具因资源不足（内存/磁盘）失败
- **THEN** 系统等待后重试，或建议清理资源

### Requirement: 记忆驱动的上下文注入
系统 SHALL 在 Agent Loop 执行前自动检索和注入相关记忆。

#### Scenario: 任务开始时注入记忆
- **WHEN** Agent Loop 开始新一轮执行
- **THEN** 系统自动搜索与当前任务相关的记忆并注入上下文

#### Scenario: 失败时检索相似解决方案
- **WHEN** 工具执行失败
- **THEN** 系统搜索记忆中是否有类似问题的解决方案

### Requirement: 性能指标收集
系统 SHALL 收集和分析 Agent Loop 的执行指标。

#### Scenario: 执行时间追踪
- **WHEN** 每轮 Agent Loop 执行完成
- **THEN** 系统记录执行时间、工具调用次数、token 消耗

#### Scenario: 成功率统计
- **WHEN** 工具执行完成
- **THEN** 系统更新该工具的成功率统计

## MODIFIED Requirements

### Requirement: 工具并发执行
**Reason**: 需要在现有并发基础上增加智能调度
**Migration**: 向后兼容，现有并发逻辑保留，增加智能层

### Requirement: 错误处理
**Reason**: 现有错误处理是固定的，需要自适应能力
**Migration**: 保留现有错误处理作为 fallback，新增智能恢复层
