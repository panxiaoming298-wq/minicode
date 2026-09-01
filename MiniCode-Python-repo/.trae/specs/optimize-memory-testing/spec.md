# 记忆体系与集成测试优化规范

## Why

当前 MiniCode 的记忆系统（Memory System）和集成测试存在以下问题需要优化：

1. **记忆系统**：多层记忆（user/project/local）的 TF-IDF 搜索算法需要优化精度和性能；记忆条目管理缺少更好的分类和标签系统；跨会话记忆恢复需要更可靠的机制
2. **集成测试**：测试覆盖率不足，特别是记忆系统与上下文管理器、会话系统的集成场景；缺少端到端的完整工作流测试；测试夹具（fixtures）不够完善

## What Changes

- 优化 TF-IDF 搜索算法，提升记忆检索精度
- 增加记忆条目分类系统和标签管理
- 改进跨会话记忆恢复机制
- 扩展集成测试覆盖记忆相关场景
- 新增端到端工作流测试
- 完善测试夹具和工具函数

## Impact

- Affected specs: Memory System, Integration Testing, Context Management
- Affected code: `py-src/minicode/memory.py`, `py-src/minicode/context_manager.py`, `py-src/tests/`

## ADDED Requirements

### Requirement: 记忆检索优化
系统 SHALL 提供优化的 TF-IDF 搜索算法，支持多语言（英文和中文）分词和更精确的相关性计算。

#### Scenario: 中文代码查询
- **WHEN** 用户输入中文查询包含代码相关术语
- **THEN** 系统能够正确分词并返回相关记忆条目

#### Scenario: 多关键词查询
- **WHEN** 用户使用多个关键词进行查询
- **THEN** 系统返回按相关性排序的记忆条目

### Requirement: 记忆分类与标签系统
系统 SHALL 支持记忆条目的自动分类和手动标签管理。

#### Scenario: 自动分类
- **WHEN** 添加新记忆条目
- **THEN** 系统根据内容自动分配分类标签

#### Scenario: 标签搜索
- **WHEN** 用户通过标签搜索记忆
- **THEN** 系统返回该标签下的所有记忆条目

### Requirement: 集成测试扩展
系统 SHALL 提供全面的集成测试覆盖记忆系统和相关组件的交互。

#### Scenario: 记忆与上下文集成
- **WHEN** 运行记忆相关集成测试
- **THEN** 验证记忆系统与上下文管理器的正确集成

#### Scenario: 端到端工作流
- **WHEN** 运行端到端测试
- **THEN** 验证完整的工作流（用户输入 → 记忆检索 → 上下文构建 → 模型调用 → 结果返回）

## MODIFIED Requirements

### Requirement: TF-IDF 实现
**Reason**: 现有 TF-IDF 实现需要支持更好的中文分词和更平滑的 IDF 计算
**Migration**: 向后兼容，现有记忆数据格式不变

### Requirement: 集成测试结构
**Reason**: 需要重组测试结构，增加专门的记忆集成测试文件
**Migration**: 新增测试文件，不影响现有测试
