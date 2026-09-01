# Project Memory

*Last updated: 2026-09-01 19:34*

## Architecture

- 工程问题应视为由输入、输出和反馈构成的动态系统，而非孤立元件的简单组合
- 系统总运动状态由子系统相互作用决定，整体大于部分之和
- 可控性与稳定性是系统能否被有效控制的关键
- **智能体映射**: agent loop=控制系统，tools=执行器，LLM=控制器
- 输出信号反向调节输入，实现系统自我修正
- 负反馈：纠正偏差、维持稳定（如恒温器原理）
- 正反馈：放大变化、驱动进化
- 反馈深度决定控制精度，反馈延迟影响系统稳定性
- **智能体映射**: tool_result→error_analysis→nudge→next_turn=完整反馈回路
- 黑箱：无法打开观察内部状态的复杂系统
- 通过输入输出关系认识系统功能，不依赖内部结构知识
- **智能体映射**: 无需理解LLM内部，通过prompt-input→response-output建立控制映射
- 记忆体注入=输入调制，system prompt=黑箱配置
- 复杂工程分解为子系统，分层优化后整合
- 每层有独立的输入/输出/反馈回路
- 高层控制低层目标，低层向上层反馈状态
- **智能体映射**: USER(战略)→PROJECT(战术)→LOCAL(执行)
- 数学建模→仿真验证→实验反馈→理论修正的完整闭环
- 定性分析确定方向，定量计算精确控制
- **智能体映射**: 意图解析(定性)→任务规划(定量)→Pipeline执行(实践)→DecisionAudit(反馈)
- 系统稳定性：在干扰下恢复平衡状态的能力
- 鲁棒控制：不确定环境下的可靠运行
- **智能体映射**: 容错重试、降级策略、边界保护
- 在约束条件下寻找性能最优解
- 代价函数设计决定优化方向
- **智能体映射**: token预算、成本约束、工具并发优化
- 多个输入输出相互耦合，需协同调节
- 解耦设计简化控制复杂度
- **智能体映射**: 多工具并发调度、结果融合、冲突检测

## 控制论与Ddd架构深度融合

- **Layer 1 战略层 (USER)**: 工程控制论核心思想 — 定义系统的控制哲学和全局原则
- **Layer 2 抽象层 (USER/PROJECT)**: 每次对话的底层逻辑抽象 — 模式识别和规律提取
- **Layer 3 场景层 (PROJECT/LOCAL)**: 落地场景的核心逻辑 — 项目特定的实现模式
- **Layer 4 执行层 (LOCAL/SCRATCHPAD)**: 当前任务的操作上下文 — 短期工作记忆
- 技能不需要控制论去梳理 — 技能是执行器，关注"怎么做"
- 完成一天工作量或阶段里程碑时，用控制论对 skill 梳理总结
- 此时与记忆体一起形成闭环：执行结果 → 模式提取 → 记忆更新 → 技能优化
- Skill 层 = 正反馈通道（强化有效模式），Memory 层 = 负反馈通道（纠正偏差）

## 控制论三大控制器实现

- — 观测并生成控制信号 `observe(state: SystemState) -> ControlSignal`
- — 记录模式有效性 `record_pattern_effectiveness(pattern_id, success)`
- — 获取模式推荐（按有效性排序） `get_pattern_recommendations()`
- — 预判配置 `preconfigure(intent: ParsedIntent) -> PreemptiveConfig`
- — 风险预判 `assess_risks(intent, config) -> RiskAssessment`
- — 预判程度 `get_optimal_preemption_level(intent) -> PreemptionLevel`
- — 记录指标快照 `record_snapshot(snapshot: MetricSnapshot)`
- — 生成稳定性报告 `get_stability_report() -> StabilityReport`
- — 快速健康检查 `check_health() -> (HealthLevel, float)`
- — 稳定性阈值检查 `is_stable(threshold) -> bool`

## Reflection

- Task Context: hello from browser

Key Decisions:

Lessons Learned:
  - Task completed successfully with the chosen approach.
  - Used 1 unique tool(s): unknown. `self-reflection success`
- Task Context: hello from browser

Key Decisions:

Lessons Learned:
  - Task completed successfully with the chosen approach.
  - Used 1 unique tool(s): unknown. `self-reflection success`
- Task Context: hello from browser

Key Decisions:

Lessons Learned:
  - Task completed successfully with the chosen approach.
  - Used 1 unique tool(s): unknown. `self-reflection success`
- Task Context: 你可以做什么

Key Decisions:

Lessons Learned:
  - Task completed successfully with the chosen approach.
  - Used 1 unique tool(s): unknown. `self-reflection success`
- Task Context: 你可以做什么

Key Decisions:

Lessons Learned:
  - Task completed successfully with the chosen approach.
  - Used 1 unique tool(s): unknown. `self-reflection success`
