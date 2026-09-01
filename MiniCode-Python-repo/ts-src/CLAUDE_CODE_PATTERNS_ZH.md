# 通过 MiniCode 你可以学习到 Claude Code 的哪些设计

## 1. Agent Loop

### Claude Code 的设计方案

Claude Code 的主体是一个持续推进的 agent loop。系统围绕同一条主控制流运转：

- 接收用户输入
- 组织当前上下文
- 请求模型
- 根据模型输出决定是否调用工具
- 执行工具
- 把工具结果继续回传给模型
- 在满足结束条件时结束当前回合

### 通过 MiniCode 可以看到的对应实现

MiniCode 的核心也是一个多步推进的回合循环。终端交互、工具系统、权限系统、MCP、skills 都围绕这条 loop 组织。

## 2. 结构化消息模型

### Claude Code 的设计方案

Claude Code 把会话中的不同状态拆成不同类型的消息或事件，用于区分：

- 用户输入
- assistant 最终回答
- 中间进度
- 工具调用
- 工具结果
- 上下文压缩后的边界或摘要信息

### 通过 MiniCode 可以看到的对应实现

MiniCode 没有把 transcript 只当作字符串列表处理，而是引入了结构化消息角色。当前项目区分了普通 assistant、progress、tool call、tool result 以及 context summary。loop 判定、TUI 展示和上下文压缩建立在这些状态之上。

## 3. Tool Use 作为协议

### Claude Code 的设计方案

Claude Code 里的工具调用是一套统一协议：

- 模型声明工具调用
- 系统解析工具输入
- 权限系统参与判断
- 工具执行后返回标准化结果
- 结果再进入下一轮模型推理

### 通过 MiniCode 可以看到的对应实现

MiniCode 采用了统一工具协议。工具有统一注册、统一 schema、统一执行入口和统一结果格式。本地工具与 MCP 动态接入的远端工具也被纳入同一层抽象。

## 4. Progress 与 Final 分离

### Claude Code 的设计方案

Claude Code 把“正在执行中的说明”和“真正的最终回答”分开处理。系统不会因为模型输出了一段过程性文本，就直接判断当前回合结束。

### 通过 MiniCode 可以看到的对应实现

MiniCode 也把中间态和最终态拆开了。progress 单独建模和渲染，不再一律落成最终 assistant 消息。回合结束条件也不再只依赖自然语言文本。

## 5. 权限与审批属于执行路径本身

### Claude Code 的设计方案

Claude Code 的权限系统属于执行路径的一部分。命令执行、文件修改等高风险行为都处于统一的审批和安全边界之内。

### 通过 MiniCode 可以看到的对应实现

MiniCode 也采用了相同的架构选择。命令执行前审批、文件修改前 review、单回合允许记忆、拒绝后给模型反馈，都纳入主回合执行过程。

## 6. MCP 作为动态能力接入层

### Claude Code 的设计方案

Claude Code 对 MCP 的设计重点是动态接入外部 server 暴露的能力。MCP 在这里承担能力发现、能力挂载和统一接入的角色。

### 通过 MiniCode 可以看到的对应实现

MiniCode 沿用了这个方向。项目在启动或运行时读取 MCP 配置，连接远端 server，发现其暴露的 tools，并统一挂载到本地工具表中。除了 tools 之外，resources 和 prompts 也通过统一 helper tools 暴露。

## 7. Skills 作为轻量工作流扩展

### Claude Code 的设计方案

Claude Code 的 skills 更像工作流扩展，而不是重型插件系统。重点在于：

- 用较轻的形式提供任务说明
- 允许系统在需要时加载特定工作流
- 让扩展可以直接参与模型执行过程

### 通过 MiniCode 可以看到的对应实现

MiniCode 在 skills 上采用了同样的轻量思路。项目通过本地 `SKILL.md` 发现和加载技能，把它们作为 prompt 和任务执行的一部分。

## 8. 自动上下文压缩

### Claude Code 的设计方案

Claude Code 的上下文压缩不是简单删除旧消息，而是把较早上下文转化为可继续工作的摘要，同时保留新的上下文片段。

### 通过 MiniCode 可以看到的对应实现

MiniCode 也采用了这个方向。项目会在长会话中自动检查上下文规模，在达到阈值时生成 `context_summary`，用摘要替换较早历史，同时保留最近的原始消息继续会话。

## 9. TUI 作为状态机的可视化层

### Claude Code 的设计方案

Claude Code 的终端界面不是单纯输出文本，而是在展示内部状态机。工具运行、完成、失败，进度消息、最终消息、审批状态等都属于不同状态的可视化呈现。

### 通过 MiniCode 可以看到的对应实现

MiniCode 的 TUI 也采用这个方向。当前终端界面不仅显示最终回答，还会显示工具运行状态、progress 消息、审批状态以及折叠后的工具结果摘要。

## 10. 前台工具执行与后台 Shell Task 分离

### Claude Code 的设计方案

Claude Code 不会把所有命令都当作同一种同步工具调用处理。对于会持续运行、可以脱离当前回合继续存在的 shell 命令，系统会把它们建模成独立 task，而不是继续伪装成一个尚未返回的普通工具调用。

### 通过 MiniCode 可以看到的对应实现

MiniCode 现在也开始采用这个方向。对于明确后台化的 shell 命令，系统不再把它们继续记成普通 `run_command` 的同步执行，而是注册成最小版 background shell task，由 TUI 单独展示状态。这一层不是完整复刻 Claude Code 的任务系统，但已经体现出“前台工具执行”和“后台 shell task”应当分开建模的思路。

## 11. 借鉴与轻量化的边界

### Claude Code 的设计方案

Claude Code 是完整产品级系统，很多设计建立在更大的状态管理、上下文管理和交互体系之上。

### 通过 MiniCode 可以看到的对应实现

MiniCode 保留的是核心设计方案，而不是完整搬运所有实现细节。项目当前保留的是：

- loop-first 的主结构
- 结构化消息
- 统一工具协议
- 审批嵌入执行路径
- MCP 动态接入
- skills 工作流扩展
- 自动上下文压缩
- 状态化 TUI
- 前台工具与后台 shell task 分离

它对应的是一个小体量的 Claude Code 风格参考实现，而不是完整复刻版本。
