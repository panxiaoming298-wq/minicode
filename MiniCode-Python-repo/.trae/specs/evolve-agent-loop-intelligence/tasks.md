# Agent Loop 智能演化任务清单

- [x] Task 1: 实现性能指标收集系统
  - [x] SubTask 1.1: 创建 AgentMetrics 数据类，定义指标结构（执行时间、token消耗、成功率、错误类型）
  - [x] SubTask 1.2: 在 agent_loop.py 中集成指标收集，记录每轮执行数据
  - [x] SubTask 1.3: 创建指标持久化模块，将历史指标保存到本地文件
  - [x] SubTask 1.4: 编写指标收集的单元测试

- [x] Task 2: 实现智能工具调度器
  - [x] SubTask 2.1: 创建 ToolScheduler 类，基于历史成功率排序工具调用
  - [x] SubTask 2.2: 实现动态并发控制，根据工具冲突历史调整并发度
  - [x] SubTask 2.3: 集成调度器到 agent_loop.py 的并发执行逻辑
  - [x] SubTask 2.4: 编写调度器单元测试

- [x] Task 3: 实现自适应错误恢复
  - [x] SubTask 3.1: 创建 ErrorClassifier 类，自动分类错误类型（网络/权限/资源/逻辑）
  - [x] SubTask 3.2: 实现 RecoveryStrategy 枚举和策略选择逻辑
  - [x] SubTask 3.3: 实现指数退避重试、降级执行、用户请求等恢复策略
  - [x] SubTask 3.4: 集成错误恢复到 agent_loop.py 的工具执行流程
  - [x] SubTask 3.5: 编写错误恢复单元测试

- [x] Task 4: 实现记忆驱动的上下文注入
  - [x] SubTask 4.1: 创建 MemoryInjector 类，在 Agent Loop 开始时检索相关记忆
  - [x] SubTask 4.2: 实现基于任务内容的记忆搜索和筛选逻辑
  - [x] SubTask 4.3: 实现记忆注入到系统提示词的格式化逻辑
  - [x] SubTask 4.4: 实现失败时的相似解决方案检索
  - [x] SubTask 4.5: 编写记忆注入单元测试

- [x] Task 5: 实现智能 Nudge 生成
  - [x] SubTask 5.1: 创建 NudgeGenerator 类，根据失败原因生成针对性提示
  - [x] SubTask 5.2: 定义常见失败场景的 Nudge 模板
  - [x] SubTask 5.3: 集成 Nudge 生成到 agent_loop.py 的重试逻辑
  - [x] SubTask 5.4: 编写 Nudge 生成单元测试

- [x] Task 6: 集成测试与验证
  - [x] SubTask 6.1: 编写 Agent Loop 端到端集成测试（含智能调度）
  - [x] SubTask 6.2: 编写错误恢复集成测试
  - [x] SubTask 6.3: 编写记忆注入集成测试
  - [x] SubTask 6.4: 运行所有现有测试，确保无回归

# Task Dependencies

- [Task 2] depends on [Task 1] - 智能调度需要历史指标数据
- [Task 3] depends on [Task 1] - 错误恢复需要错误类型统计
- [Task 4] depends on [Task 2] - 记忆注入在调度之后执行
- [Task 5] depends on [Task 3] - 智能 Nudge 基于错误分类
- [Task 6] depends on [Task 1, Task 2, Task 3, Task 4, Task 5] - 集成测试需要所有组件
