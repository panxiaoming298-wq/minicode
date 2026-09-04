---
status: accepted
date: 2026-09-02
scope: unified-runtime-ultra-v1-acceptance
companion-upgrade-plan: 2026-09-02-multi-agent-runtime-v2-upgrade.md
implementation-status: ultra-v1-complete
---

# MiniCode 统一 Runtime 与 Ultra V1 验收计划

## 1. 目标与适用范围

本计划验收 [统一 Runtime 与 Ultra V1 升级方案](./2026-09-02-multi-agent-runtime-v2-upgrade.md)：Standard 与 Ultra 共用唯一 Runtime；Ultra V1 只实现“并行只读探索/审查 → 唯一 Writer → 独立只读 Verifier → 集成 Final Gate”。本轮不验收多 Writer、远程 Worker、公开 Auto 或递归动态委派。

对外只有 `standard` 与 `ultra` 两个 Profile；验收的内部 route 固定为 `standard-single`、`deep-single`和 `ultra-read-fanout`。

所有“通过”必须有实际命令和持久证据。Provider、OS 权限或工具缺失造成的未运行项标为 `blocked`，不得写成 passed。每个测试使用 fake adapter、fake clock、barrier/event 或临时仓库，禁止靠长 `sleep` 猜并发。

## 2. 编号修订说明

R01–R18 保留原编号。其语义已按统一 Runtime 校正：原“多个 builder/可并行 writer/双路径 runtime”等表述不再成立；R14 明确为 Ultra V1 唯一 Writer，Multi-Writer deferred；R17 的 flag 仅用于迁移，不能形成永久双 Runtime。新增要求从 R19 连续编号至 R36。

状态词：`implemented`=代码已接入真实调用链；`passed`=对应功能与安全自动化验证实际通过；`calibration-pending`=功能已实现但尚缺可信量化基线；`environment-blocked`=代码之外的平台能力阻塞；`deferred`=明确不在本轮。本轮最终状态为 `accepted`，仅 Phase 7 非目标保持 `deferred`。

## 3. 测试环境

- E0：纯单元，fake clock/fake adapter，无网络。
- E1：Runtime 集成，临时目录/临时 Git 仓库，真实线程与受控 barrier。
- E2：MiniCode 全仓回归、compile、lint/type/static（以项目已声明工具为准）。
- E3：真实 Provider smoke，可选 G6；不影响离线安全门禁结论，但阻塞必须记录。

本轮实测快照（2026-09-02, Windows, Python 3.11）：Runtime V2 专项集 `104 passed`；全仓回归为 `1512 passed, 2 skipped, 1 warning`，无失败。两个 skip 是仓库原有、仅在设置 `ANTHROPIC_API_KEY` 时运行的 Anthropic live API 用例；本轮另以已配置的 DeepSeek Provider 完成 G6。Windows junction 与 hardlink 使用真实文件系统对象覆盖，不再依赖创建 symlink 的账户权限。`ruff` 按 CI 参数检查 `minicode/` 全绿，`mypy` 全仓仍为 224 个历史基线错误（CI 上限 225），新增 Runtime V2 模块单独检查 0 个错误；`compileall` 与 `git diff --check` 通过。G4 受控基准 20 个测量样本加 3 次 warmup 通过，G6 Standard/Ultra live smoke 均通过。

## 4. Requirement → code → test → evidence 追踪矩阵

以下每行都包含 Requirement ID、场景、前置条件、步骤、预期、证据、代码、自动化测试与 Gate；状态已按本轮真实代码与命令结果回填。

| ID / 状态 | 场景 | 前置条件 | 执行步骤 | 预期结果 | 证据 | 实现位置 | 自动化测试位置 | Gate |
|---|---|---|---|---|---|---|---|---|
| R01 `passed` | TaskContract 与 Amendment 版本化 | revision=1 的 Run | 序列化；提交 amendment；重载 | revision=2，同一 run，changed/affected 字段明确 | contract/amendment JSON | `minicode/runtime_protocol.py` | `tests/test_runtime_protocol.py`, `tests/test_runtime_reducer.py` | G0,G1 |
| R02 `passed` | 唯一 Canonical PlanGraph | 一个含依赖的 contract | compiler 生成 PlanVersion；TaskGraph round-trip | 仅一份当前 Plan 状态；Run→Version→Node→Attempt 分离 | plan snapshot | `minicode/runtime_protocol.py`, `minicode/task_graph.py` | `tests/test_runtime_architecture.py`, `tests/test_runtime_reducer.py` | G0,G1 |
| R03 `passed` | Structural PlanValidator | 循环、缺依赖、两个 writer 夹具 | 逐个验证 | 非法 DAG 和 >1 writer 被执行前拒绝；合法图不调用模型 | validation records/model call count=0 | `minicode/unified_runtime.py` | `tests/test_plan_validator_v2.py` | G1,G2 |
| R04 `passed` | Reducer 是唯一状态写入口 | 空 RuntimeState | 发送合法/重复/越序 command/result | 只经 reducer 变更；事件幂等；越序拒绝 | event log + rejection reason | `minicode/runtime_state.py` | `tests/test_runtime_reducer.py`, `tests/test_runtime_recovery.py` | G1,G3 |
| R05 `passed` | ContextManifest 完整且可失效 | 已 hash 的文件/依赖/artifact | 构建 manifest；改变 revision；提交旧 result | 字段齐全；旧 context result 不进入当前 Plan | manifest JSON + rejection | `minicode/worker_runtime.py`, `minicode/runtime_state.py` | `tests/test_context_manifest.py`, `tests/test_runtime_recovery.py` | G1,G2 |
| R06 `passed` | 中央 Policy/Approval | Worker 请求受控工具 | 创建 request/grant；由 PermissionManager adapter 执行 | 所有 Worker 使用相同规则；denied/expired 是结构化 result | approval event log + operation digest | `minicode/runtime_policy.py`, `minicode/permissions.py`, `minicode/tooling.py` | `tests/test_runtime_approval.py`, `tests/test_runtime_recovery.py` | G1,G3 |
| R07 `passed` | Structured AgentResult 四类终态 | fake workers success/fail/wait/cancel | 运行并 round-trip | 结果含 run/version/node/attempt/base/usage/warnings/failure | result JSON | `minicode/runtime_protocol.py`, `minicode/worker_runtime.py` | `tests/test_runtime_protocol.py`, `tests/test_runtime_recovery.py`, `tests/test_runtime_replay.py` | G0,G1 |
| R08 `passed` | Evidence/Artifact 完整性 | 小/大输出、源码位置、路径别名 | put/get/restart/hash tamper；替换为 hardlink | hash/size/producer/base 可追踪；篡改、越界及别名读取拒绝 | artifact files + metadata | `minicode/runtime_artifacts.py`, `minicode/path_safety.py` | `tests/test_runtime_artifacts.py`, `tests/test_large_agent_result.py` | G1,G3 |
| R09 `passed` | Node 与 Final verification 分离 | 节点均成功但集成 invariant 失败 | node verify；final verify | node record 保留，Final Gate 拒绝 completed | verification/gate records | `minicode/verifier_registry.py`, `minicode/unified_runtime.py` | `tests/test_completion_gate.py`, `tests/test_standard_runtime.py` | G2,G3 |
| R10 `passed` | 唯一 Scheduler 驱动 DAG | 有依赖/无依赖节点 | submit/claim/complete/cancel | 依赖顺序正确；创建/claim/retry 只在 Scheduler | attempt/event trace | `minicode/runtime_scheduler.py` | `tests/test_runtime_scheduler.py`, `tests/test_runtime_recovery.py` | G1,G2 |
| R11 `passed` | 真实只读并行 | 两个 read-only 节点、barrier | 同时启动；释放 barrier | 重叠运行；并发不超上限；workspace hash 零变化 | timestamps/barrier/hash diff | `minicode/runtime_scheduler.py`, `minicode/tooling.py` | `tests/test_ultra_read_fanout.py`, `tests/test_runtime_scheduler.py` | G2,G3 |
| R12 `passed` | Retry 与 replan 分离 | retry_budget=1；amendment | 首 attempt retryable；再试；amend | retry 新 Attempt 不新 Node；amend 新 PlanVersion | version/attempt trace | `minicode/runtime_state.py`, `minicode/runtime_scheduler.py` | `tests/test_runtime_recovery.py`, `tests/test_runtime_reducer.py` | G1,G3 |
| R13 `passed` | 全局预算、深度与取消 | 小 agents/token/cost/deadline/depth | 请求超限、运行中 cancel | 停止扩张；状态结构化；所有 child 最终取消 | budget ledger/resource census | `minicode/runtime_scheduler.py` | `tests/test_runtime_budget_cancel.py`, `tests/test_task_delegation_gateway.py`, `tests/test_runtime_command_cancel.py` | G2,G3 |
| R14 `passed` | 唯一 Writer 隔离 | 两 writer claim + temp workspace | 并发 claim；写变更；集成 | 仅一 write lease；第二个被结构验证拒绝；无多 writer | lease log + patch/integration record | `minicode/workspace_provider.py`, `minicode/runtime_scheduler.py` | `tests/test_plan_validator_v2.py`, `tests/test_workspace_provider.py`, `tests/test_ultra_read_fanout.py` | G3 |
| R15 `passed` | 崩溃恢复与 cancel cleanup | 持久 state、fake clock、路径别名 | 在指定故障点重启/取消；注入 hardlink/junction | claim 可回收；无 worker/process/lease/temp workspace 遗留；持久状态不跟随别名 | recovery log + resource census | `minicode/runtime_state.py`, `minicode/runtime_scheduler.py`, `minicode/tools/run_command.py`, `minicode/path_safety.py` | `tests/test_runtime_recovery.py`, `tests/test_runtime_command_cancel.py`, `tests/test_workspace_provider.py`, `tests/test_runtime_artifacts.py`, `tests/test_session.py` | G3,G5 |
| R16 `passed` | Delivery/CompletionGate | 最新 contract、集成 workspace | 用缺证据/审批/冲突/运行 worker 场景 final | 仅全部强制条件满足才 deliver | DeliveryManifest + gate reasons | `minicode/verifier_registry.py`, `minicode/unified_runtime.py` | `tests/test_completion_gate.py`, `tests/test_standard_runtime.py` | G2,G3 |
| R17 `passed` | Standard 兼容与临时 flag | 旧配置/session、flag on/off | 同一 golden task 两路径；恢复 | 默认 Standard；行为兼容；flag 是可移除迁移手段 | golden diff + model-call trace | `minicode/config.py`, `minicode/unified_runtime.py`, product entries | `tests/test_standard_runtime.py`, existing CLI/headless regression tests | G2,G4,G5 |
| R18 `passed` | 审计、Session 与可观察性 | 运行 Standard/Ultra | 保存/重启/读 trace | route、command/event、attempt、usage、verification 可序列化且不写入 Runtime credential config | session extension + trace | `minicode/session.py`, `minicode/session_contract.py`, `minicode/runtime_state.py` | `tests/test_runtime_session.py`, `tests/test_runtime_recovery.py` | G1,G5 |
| R19 `passed` | 两个公开 Profile、三个内部 route | 无 profile/standard/ultra config | resolve profiles | 缺省 standard；仅公开 standard/ultra；route 为规定三值 | profile snapshots | `minicode/runtime_protocol.py`, `minicode/unified_runtime.py`, `minicode/config.py` | `tests/test_topology_resolver.py`, `tests/test_standard_runtime.py`, existing config tests | G0,G1 |
| R20 `passed` | Ultra TopologyDecision | 可拆分/强耦合/冲突/预算不足/异常 hint 表 | resolve 每一行 | fan-out 或 deep-single 符合八条件，含 reasons/rejected；false/NaN/负值 fail closed | decision JSON | `minicode/unified_runtime.py` | `tests/test_topology_resolver.py` | G1,G2 |
| R21 `passed` | task 不能绕过 Scheduler | Runtime context + task tool | 调 task；检查 import/calls/trace | DelegationRequest 注册 Node/Attempt；无直接 AgentLoop；Standard 串行；Ultra 动态拒绝 | scheduler/delegation trace + static grep | `minicode/tools/task.py`, `minicode/delegation.py`, `minicode/runtime_scheduler.py` | `tests/test_task_delegation_gateway.py` | G0,G2,G3 |
| R22 `passed` | lease/fencing 安全 | fake clock 与两 token | 过期、旧 token、重复完成 | 100% stale 拒绝；同节点同时仅一个有效 Attempt | rejection events | `minicode/runtime_state.py`, `minicode/runtime_scheduler.py` | `tests/test_runtime_reducer.py`, `tests/test_runtime_recovery.py` | G1,G3 |
| R23 `passed` | 显式 ToolEffect | 所有 default tools 注册 | 审计 metadata；给 explorer 写调用 | 每个工具 effect/replay 明确；explorer registry 仅 read_only；写尝试 PolicyDenied | registry audit + result | `minicode/tooling.py`, `minicode/tools/__init__.py` | `tests/test_tool_effects.py` | G0,G3 |
| R24 `passed` | 取消旧 8,000 静默截断 | 输出 >8,000 chars | task/worker 完成；读取 ref | 完整字节可取、hash/size 正确、摘要明确说明 artifact | byte-for-byte comparison | `minicode/tools/task.py`, `minicode/runtime_artifacts.py`, `minicode/worker_runtime.py` | `tests/test_large_agent_result.py` | G1,G3 |
| R25 `passed` | Approval 完整 digest 绑定 | grant 对应固定 args/cwd/base | 改任一字段并复用 | 每种修改均拒绝；single-use/expiry 生效；PermissionManager allow-once 不跨操作复用 | digest matrix/event | `minicode/runtime_policy.py`, `minicode/tooling.py`, `minicode/permissions.py` | `tests/test_runtime_approval.py` | G3 |
| R26 `passed` | WAITING_INPUT 从原 Run 恢复 | node waiting_input | 提交 TaskAmendment；局部 replan | 同 run，新 contract/plan；只 supersede 受影响节点 | before/after graph | `minicode/runtime_state.py`, `minicode/unified_runtime.py` | `tests/test_runtime_reducer.py`, `tests/test_runtime_recovery.py` | G1,G3 |
| R27 `passed` | superseded/current-attempt 隔离 | plan v1 running，v2 current | v1 迟到提交；v2 提交 | v1 不污染；v2 唯一有效；duplicate no-op | reducer audit | `minicode/runtime_state.py` | `tests/test_runtime_reducer.py`, `tests/test_runtime_recovery.py` | G1,G3 |
| R28 `passed` | Writer lease 与 fencing | writer attempt 过期并重领 | 旧/新 writer 各提交 | 旧 writer 100% 拒绝；新 writer 可集成 | lease/integration trace | `minicode/runtime_scheduler.py`, `minicode/workspace_provider.py` | `tests/test_runtime_reducer.py`, `tests/test_runtime_recovery.py`, `tests/test_workspace_provider.py` | G3 |
| R29 `passed` | Verifier 确定性优先 | registry 含 deterministic/model | 执行可由 tests 判定的任务 | 顺序固定；确定性足够时 model call=0；crash 不误成功 | invocation order/count | `minicode/verifier_registry.py`, `minicode/unified_runtime.py` | `tests/test_verifier_registry.py`, `tests/test_completion_gate.py`, `tests/test_standard_runtime.py` | G1,G2 |
| R30 `passed` | Standard 快路径成本/性能 | deterministic golden set + legacy path | 20 次测量、3 次 warmup，比较 calls/token/p95/success | success +0pp；p95 +3.1578%；token +0%；model calls +0，均在门限内 | `benchmarks/runtime_v2_calibration_results.json` | `minicode/unified_runtime.py`, `benchmarks/runtime_v2_calibration.py` | `tests/test_standard_runtime.py`, `tests/test_runtime_v2_calibration.py` | G4 |
| R31 `passed` | Ultra 收益门禁 | 五个可拆分等价逻辑单元 | serial Standard vs read-fanout 各 20 次测量 | success +0pp；Ultra p95 墙钟降低 36.3853%，超过 25% 门限 | `benchmarks/runtime_v2_calibration_results.json` | `minicode/unified_runtime.py`, `benchmarks/runtime_v2_calibration.py` | `tests/test_ultra_read_fanout.py`, `tests/test_runtime_v2_calibration.py` | G4 |
| R32 `passed` | replay policy/副作用安全 | 四 replay policy fake tools | 制造成功后持久化前崩溃/unknown result | safe/idempotent 按约束；id key 复用；unknown non-idempotent 自动 retry=0 | tool invocation ledger | `minicode/tooling.py`, `minicode/runtime_scheduler.py`, `minicode/worker_runtime.py` | `tests/test_runtime_replay.py`, `tests/test_runtime_recovery.py`, `tests/test_runtime_approval.py` | G3 |
| R33 `passed` | Session/Checkpoint/Rewind 不回归 | 旧 bundle 与 checkpoints | load/save/rewind；加入 runtime extension | 旧数据可读；新 extension 可选；文件恢复语义不变 | compatibility fixtures | `minicode/session.py`, `minicode/session_contract.py` | `tests/test_runtime_session.py`, existing session/checkpoint/rewind tests | G5 |
| R34 `passed` | workspace base drift/cleanup | temp repo + user concurrent edit + alias | writer 修改；源变化；apply/cancel/crash；第二文件 commit 故障 | 停止冲突 apply；原子回滚已应用文件；不覆盖无关修改；拒绝 junction/hardlink；所有路径清理 | file hashes + temp-dir census | `minicode/workspace_provider.py`, `minicode/path_safety.py` | `tests/test_workspace_provider.py`, `tests/test_ultra_read_fanout.py` | G3 |
| R35 `passed` | CLI/TUI/config 入口 | 旧/new settings | parse no profile、standard、ultra、auto | 默认 standard；standard/ultra 可见；auto/multi-writer 不公开；progress 展示 selected route/reason | CLI/progress/config assertions | `minicode/config.py`, `minicode/main.py`, `minicode/headless.py`, `minicode/tui/input_handler.py`, `minicode/web_worker.py` | `tests/test_topology_resolver.py`, `tests/test_standard_runtime.py`, existing CLI/TUI/Web tests | G2,G5 |
| R36 `passed` | 执行权威与非目标边界 | 全仓源码 | 静态搜索 + runtime probes | AgentLoop/TurnKernel/ToolScheduler/Pipeline/Supervisor 无全局调度越权；无 remote/multiwriter/Milvus authority | architecture audit | listed production modules | `tests/test_runtime_architecture.py`, `tests/test_plan_validator_v2.py` | G0,G5 |

## 5. 关键端到端场景

### A. Standard 快路径

运行一个简单读取问答和一个简单改名任务。断言默认 profile 为 Standard、`max_active_workers=1`、确定性单节点 Plan、模型适配器调用次数与旧 AgentLoop 相同、planner/final-model verifier 调用为零。调用 `task` 时必须看到 DelegationRequest、Node、Attempt 与 Reducer event，并证明父子串行。复跑现有 PermissionManager、Session、Checkpoint、Rewind 与 TurnKernel evidence guard 测试。

### B. Ultra 路由

表驱动输入至少含：两个独立只读调查方向、强耦合根因调试、高写冲突、预算不足。第一项选择 `ultra-read-fanout`，其余选择 `deep-single`；所有 decision 都包含输入指标、reasons、rejected routes。Ultra 选择 deep-single 不失败，也不伪造多个 Worker。

### C. 调度一致性

用 fake clock 令 claim 过期，再发旧 fencing result；用 barrier 制造重复 result、同节点竞争 claim、Plan v1 迟到与 claim 后崩溃。有效提交数必须始终为 1，stale/superseded 结果拒绝率必须为 100%，重启后能回收 claim。

### D. 只读隔离与唯一 Writer

对两个 Explorer 使用只读 registry 和受控 barrier，运行前后递归 hash 工作区，差异为 0。尝试调用 write tool 应得到 `PolicyDenied`。随后两个 Writer 竞争 write lease，仅一个成功；Writer 在隔离 workspace 产出 changed_resources，再基于 hash 安全集成。Verifier 的工作区前后 hash 也为 0。

### E. Result、Artifact 与 Evidence

生成超过 8,000 字符且包含尾部哨兵的输出；重启 store 后通过 ArtifactRef 逐字节取回，尾部哨兵、hash、size 均正确。篡改内容时读取失败。Evidence 可定位源码范围、producer node 与 base revision，Verifier 使用完整 artifact 而非摘要。

### F. Approval、Input 与恢复

对 approval 的 arguments、cwd、environment scope、base revision、operation digest 逐项变更，旧 grant 均失效；过期与拒绝均由 Reducer 处理，不进入 Verifier。进程在 WAITING_APPROVAL/WAITING_INPUT 重启后恢复；TaskAmendment 只使依赖变更字段的节点 superseded。

### G. 取消、预算与副作用

在 Explorer、Writer、Verifier、审批等待各阶段取消，最终资源清单必须没有 child worker、process、lease、临时 workspace。分别触发 max_agents/tokens/cost/deadline/retry/depth；Runtime 停止扩张或受控失败。结果不明确的 `never_auto_retry` 工具调用次数必须为 1，`idempotent_with_key` 重试复用同一 key。

### H. Verification

构造节点 acceptance 分别通过、集成测试失败的仓库；Final Gate 必须拒绝完成。Registry 记录确定性 verifier 的顺序；只有确定性工具不足且预算允许时才调用 model verifier。Standard light 与 Ultra strict 共用接口；严格模式缺 Evidence/Artifact、存在冲突或活跃 Worker 时一律失败。

## 6. 故障注入矩阵

| 故障点 | 注入方式 | 必须观察到的安全结果 | 对应需求 |
|---|---|---|---|
| claim 后、Worker 启动前崩溃 | hook 抛异常 + fake clock 过 lease | claim 回收，新 fencing；旧 result 拒绝 | R15,R22 |
| 工具成功后、Result 持久化前崩溃 | commit hook 中断 | replay policy 生效；非幂等 unknown 不重试 | R15,R32 |
| Event/State 写入中断 | atomic writer failpoint | 旧 snapshot+完整 event 恢复或事务拒绝，无半状态 | R04,R15,R18 |
| lease 过期 Worker 继续提交 | advance fake clock | 100% rejected | R22,R28 |
| Worker 中 TaskContract 被修改 | barrier 后 amendment | 旧 context/plan result superseded | R05,R26,R27 |
| 审批等待中重启 | persist/recreate coordinator | 仍 WAITING_APPROVAL；grant 校验后恢复 | R06,R25 |
| Provider 429/timeout/invalid | fake adapter scripted errors | bounded retry，预算扣减，terminal reason 结构化 | R12,R13 |
| Writer 中途取消 | writer hook/barrier | 无集成半成品，write lease/temp 清理 | R14,R34 |
| Verifier 自身崩溃 | verifier raises | VerificationFailed；不得 completed | R09,R29 |
| Artifact 内容成功、元数据失败 | metadata failpoint | 可恢复孤儿或安全重写；不返回无效 ref | R08,R15 |
| patch/worktree apply 冲突 | 改源 base hash | 停止 apply；保留用户修改；Final Gate fail | R14,R34 |

## 7. Gate G0–G6

### G0：静态、schema 与文档门禁

本轮结果：`passed`。

- 所有协议可序列化且 version 明确；Enum/state 无非法重复。
- 两份文档链接有效、代码块闭合、Mermaid 基本可解析、R01–R36 连续唯一、Gate 映射完整。
- 静态审计证明 `tools/task.py` 不 import/call AgentLoop；产品只有一个 Worker Scheduler/Reducer/Canonical PlanGraph 权威。
- 所有 default tools 有显式 effect/replay；无公开 Auto/multi-writer/remote route。

### G1：单元与状态机门禁

本轮结果：`passed`。

- protocol、Reducer、PlanValidator、lease/fencing、approval digest、artifact、context、verifier registry 测试全通过。
- 过期 lease/fencing 与 superseded result 拒绝率 100%；重复 Result 有效提交次数恰为 1。
- fake clock 驱动时间测试，不因墙钟 sleep 产生偶发结果。

### G2：统一 Runtime 离线集成门禁

本轮结果：`passed`。

- Standard、deep-single、read-fanout 均经过同一 Runtime/Scheduler/Reducer/CompletionGate。
- Standard task 受 Scheduler 治理且串行；Ultra 动态委派关闭。
- 至少两个 read-only Worker 由 barrier 证明真并行；依赖节点不提前执行；并发不超 profile。
- node success + integration failure 时 Final Gate 拒绝完成。

### G3：安全、隔离与故障门禁

本轮结果：`passed`。

- Explorer/Reviewer/Verifier 对工作区修改数为 0。
- 任一时刻只有一个有效 write lease；stale writer 100% rejected。
- 取消后 worker/process/lease/temp workspace 遗留数为 0。
- 修改后的操作复用旧 approval 成功数为 0。
- 结果不明确的非幂等外部调用自动重试次数为 0。
- base drift、apply conflict、Artifact 元数据失败、Verifier crash 均 fail closed。

### G4：性能、兼容率与收益门禁

本轮结果：`passed`。命令 `python benchmarks/runtime_v2_calibration.py --samples 20 --warmups 3 --standard-worker-ms 1000 --ultra-unit-ms 1200 --fail-on-regression` 退出 0；完整证据见 `benchmarks/runtime_v2_calibration_results.json` 与 `.md`。这是无网络、同一 deterministic Worker 的 Runtime 开销/并发校准，不外推为 Provider 模型质量结论。

- Standard：legacy/unified 成功率均 100%；p95 从 1000.8427ms 到 1032.4471ms（+3.1578%）；平均 token 均 128（+0%）；平均 model call 均 1（+0）。
- Ultra：等价串行 p95 6034.6978ms，read-fanout p95 3838.9541ms，墙钟改善 36.3853%；两者成功率均 100%、平均 token 均 500。
- 基准刻意使用 1.2s 的独立逻辑单元来验证“收益高于协调成本”的适用区间；微任务仍应由 TopologyDecision 选择 `deep-single`，不能从本结果推导所有 Ultra 请求都应 fan-out。

### G5：全仓、兼容、恢复与产品入口门禁

本轮结果：`passed`，无环境阻塞。`python -m pytest -q --tb=short` 为 `1512 passed, 2 skipped, 1 warning`；两个 skip 是未设置 `ANTHROPIC_API_KEY` 时按设计跳过的原有 Anthropic live API 用例。Windows 路径安全测试改用无需管理员权限的真实 junction/hardlink，产品断言全部通过。

- 全仓 unit/integration、compile/import、项目已配置 lint/type/static 检查通过，或每个环境阻塞有准确记录。
- 旧 config/session/checkpoint/rewind fixture 通过；默认 profile=standard。
- CLI/TUI/Web/Headless 产品入口走统一 Runtime；展示 selected route，Ultra deep-single 展示原因。
- Web 输入框右下角只公开 Standard/Ultra；选择值随单次运行请求传入并写入 RunRecord，运行中锁定。`deep-single` 继续作为 Ultra 的内部 route，不作为第三个公开模式。
- 故障重启后 state/event/approval/artifact 可恢复且敏感字段已脱敏。

### G6：可选 Live Provider Smoke

本轮结果：`passed`。真实 DeepSeek `deepseek-v4-pro` Provider 上，Standard 返回 `OK`、route=`standard-single`、延迟 7386.41ms；Ultra 返回 `OK`、route=`deep-single`、延迟 39592.50ms。两次 readiness/trace 均可用，证据已脱敏保存为 `benchmarks/runtime_v2_provider_smoke_standard.json` 与 `benchmarks/runtime_v2_provider_smoke_ultra.json`。Ultra 在该不可拆分 smoke 上选择 deep-single 正是预期行为。

在明确配置的真实 Provider 上各运行一个 Standard 和 Ultra smoke，记录模型、时间、退出状态和脱敏 trace。429、网络、凭据或本机服务不可用属于环境阻塞，不覆盖 G0–G5，也不能声称通过。

## 8. 一票否决条件

任一项出现，本轮不得标记 Ultra V1 完成：

1. Standard 绕过统一 Scheduler/Reducer，或存在第二套可推进 Plan 状态。
2. `task` 直接创建未登记 AgentLoop/Worker。
3. 简单 Standard 增加 LLM planner 或 final model verifier 调用。
4. Explorer/Reviewer/Verifier 能修改工作区，或两个 Writer 同时持有效 lease。
5. stale lease/fencing/superseded/duplicate Result 被当前 Plan 接受。
6. 大输出静默截断，或 Verifier 只能读取摘要。
7. 参数/cwd/base 变化仍可复用旧 approval。
8. 取消后遗留 Worker、子进程、lease 或 temp workspace。
9. unknown 非幂等副作用被自动重试。
10. Final Gate 不检查集成 workspace，或节点 success 自动等价任务 completed。
11. 文档/报告宣称未执行的测试或未实现的 Multi-Writer 已完成。

## 9. 证据目录与命名

证据统一写入项目合法的临时/测试输出或 MiniCode Runtime 状态目录，不污染仓库根目录；最终 CI artifact 可按下列逻辑结构收集：

```text
runtime-v2-evidence/<run-id>/
  baseline.json
  profile.json
  topology-decision.json
  contract-r<N>.json
  plan-v<N>.json
  events.jsonl
  attempts/<node>/<attempt>.json
  context-manifests/
  artifacts/
  verification/
  resource-census.json
  test-report.json
  benchmark.json
```

文件内容必须脱敏；ArtifactRef 的 hash/size 可交叉校验；测试日志引用 Requirement 与 Gate。工作目录中的临时证据在测试结束后清理，CI/Session store 中的证据按现有保留策略保存。

本轮持久证据为 `benchmarks/runtime_v2_calibration_results.json`、`benchmarks/runtime_v2_calibration_results.md`、`benchmarks/runtime_v2_provider_smoke_standard.json` 和 `benchmarks/runtime_v2_provider_smoke_ultra.json`；Provider 文件不含 credential、完整环境或原始 trace。

## 10. 执行顺序与 Milestone 判定

严格按升级方案 Phase 0→6 实施并依次执行 G0→G5；G6 可选。Phase 7 Multi-Writer 只保留设计且为 deferred。

| Milestone | 进入条件 | 退出判定 | 可独立回滚 | Requirements |
|---|---|---|---|---|
| M0 Baseline | 测试环境可用 | golden/调用次数/延迟 token 基线已记录 | 无产品变更 | R17,R30 |
| M1 Protocol | M0 | G0 + protocol/reducer 单元通过 | flag off/忽略 extension | R01-R08,R18 |
| M2 Unified Standard | M1 | Standard/task 通过 G1-G2 | 临时 flag 回旧串行 | R10,R17,R19-R21,R30,R33 |
| M3 Read Fan-out | M2 | 只读并行、budget/cancel/artifact 通过 G2-G3 | route=deep-single | R11-R13,R20,R22-R27,R31-R32 |
| M4 Exclusive Writer | M3 | isolation/verifier/final gate 通过 G3 | Ultra=deep-single/Standard | R09,R14-R16,R28-R29,R34 |
| M5 Product Ready | M4 | G0-G5 全部通过；G6 如运行则证据脱敏 | 默认 Standard/临时 flag | R01-R36 |

## 11. 文档与实现一致性检查

代码修改前和完成后各运行一次轻量检查：

1. 两个 companion link 的目标存在。
2. fenced code block 数量成对；Mermaid block 包含受支持的 graph/flowchart/sequenceDiagram/stateDiagram-v2 声明。
3. `R01..R36` 定义行恰好各一条、无断号；`G0..G6` 标题恰好各一条。
4. Phase 0–7、M0–M5、Standard/deep-single/ultra-read-fanout 术语一致。
5. 不含“Ultra 必须多 Agent”“Standard 必须低推理”“本轮 Multi-Writer completed”等冲突结论。
6. 文档模块路径全部在仓库中存在，或明确标注为本轮新增文件。

## 12. 最终验收报告模板

```markdown
# Unified Runtime / Ultra V1 Acceptance Report

- revision / platform / Python:
- implementation status:
- baseline: passed / failed / skipped / environmental blockers
- G0: status, commands, evidence
- G1: status, commands, counts, evidence
- G2: status, commands, counts, evidence
- G3: status, fault matrix, resource census
- G4 Standard: success delta, p95 delta, token delta, model-call delta
- G4 Ultra: success/wall-clock delta or calibration-pending rationale
- G5: full-suite and compatibility results
- G6: passed / failed / blocked / not-run with reason
- R01-R36: implementation file, test, result, evidence
- known deviations/deferred: only factual unfinished items
- veto conditions: none / list
- git status --short:
- git diff --stat:
- final decision: accepted / conditional / rejected
```

只有 G0–G5 的安全与一致性硬条件全部满足、无一票否决项时，才可写 `accepted`。Ultra 收益数据不足时可写 `conditional: calibration-pending`，但不得把功能安全缺失解释为待校准。

本轮实际决定：`accepted`。G0–G5 全部通过，无一票否决项；可选 G6 也已用真实 Provider 通过。Phase 7 Multi-Writer 仍是明确非目标，不影响 Ultra V1 的完成判定。
