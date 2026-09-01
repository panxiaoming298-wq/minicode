---
status: draft
date: 2026-09-02
scope: multi-agent-runtime-v2-acceptance
companion-upgrade-plan: 2026-09-02-multi-agent-runtime-v2-upgrade.md
---

# MiniCode 多 Agent Runtime V2 方案验收计划

## 1. 验收目标

验证 V2 不仅“存在多个 Agent”，而且满足以下产品事实：

- 简单任务没有被多 Agent 编排拖慢或额外消耗模型调用；
- 复杂任务由合法 DAG 驱动，多个 Worker 的并行、失败和重试可控；
- 所有副作用经过权限与审批绑定；
- 所有完成声明都有独立证据；
- 进程中断、用户补充、replan 和旧结果迟到不会破坏状态；
- 并行写不会覆盖用户或其他 Worker 的修改；
- 最终交付能区分完成、部分完成、失败和未验证。

本验收计划对应：[MiniCode 多 Agent Runtime V2 升级方案](./2026-09-02-multi-agent-runtime-v2-upgrade.md)。

## 2. 验收原则

1. 以可重放事件、Artifact、Evidence、测试输出和 DeliveryManifest 为证据，不接受口头演示作为唯一依据。
2. 模型不能给自己判定通过；确定性检查优先于模型判断。
3. 离线测试必须可重复，不依赖真实 provider 可用性。
4. Live provider smoke 是独立发布证据，不能替代本地正确性门禁。
5. 任何未授权副作用、用户改动丢失或错误 completed 都是一票否决。
6. 失败测试必须保留真实失败，不允许使用 skip、空实现或固定成功结果绕过。

## 3. 验收对象与版本冻结

每轮候选验收必须记录：

- Git commit、dirty worktree 状态和 Python 版本；
- Task/Plan/Event/WorkerResult/Evidence/Approval/Delivery schema version；
- feature flag 模式；
- 模型与 fake provider 版本；
- OS、文件系统和 worktree 支持状态；
- 测试命令、开始/结束时间、退出码；
- 验收 manifest 的 SHA-256。

候选代码、测试夹具和验收阈值冻结后，不得在同一轮验收中静默修改。阈值变化必须新建验收 revision 并写明原因。

## 4. 测试环境分层

### E0：纯单元环境

- 不调用网络和真实模型。
- 覆盖 schema、Reducer、Plan Validator、Scheduler 决策、权限绑定和 verifier 纯逻辑。
- 使用确定性时钟、ID 和 in-memory/fake event store，但不得伪造被验收功能的成功结果。

### E1：Fake Provider 集成环境

- 使用可脚本化 ModelAdapter，返回预定义 tool calls、plan、错误、空响应和迟到结果。
- 使用真实 Agent Loop、ToolRegistry、TaskRuntime 和 event persistence。
- 用于验证端到端状态转换，不评价模型智能。

### E2：本地仓库夹具环境

- 使用临时 Git 仓库和真实 worktree。
- 使用真实文件、patch、命令、测试和 PermissionManager。
- 覆盖写隔离、合并、冲突、清理、checkpoint 和恢复。

### E3：真实 Provider Smoke

- 显式 opt-in；默认 CI 不执行。
- 只跑低成本、无破坏性、固定验收标准的场景。
- Provider 不可用标记为 provider risk，不能把本地逻辑错误伪装成 provider 问题。

## 5. 需求追踪矩阵

| ID | 需求 | 最低场景 | 必要证据 | 发布门禁 |
| --- | --- | --- | --- | --- |
| R01 | 统一 TaskContract/Amendment | 新任务、补充信息、revision 递增 | contract JSON + event log | M1 |
| R02 | 统一 Plan schema | fast/single/multi 三种 plan | plan artifact + schema validation | M1 |
| R03 | Plan Validator 强制执行 | 环、悬空依赖、缺验证、写冲突 | validator report | M1 |
| R04 | Reducer 确定性和幂等 | 重放、重复事件、乱序迟到结果 | projection hashes | M1 |
| R05 | 最小 Context | 子 Worker 只收到必要上下文 | invocation/context manifest | M2 |
| R06 | Policy/Approval 精确绑定 | 参数、路径、revision、TTL 改变 | policy decision log | M2 |
| R07 | WorkerResult 结构化 | success/blocked/failed/cancelled | WorkerResult artifact | M2 |
| R08 | Claim/Evidence 完整性 | 无 evidence claim 被拒绝 | verification report | M2 |
| R09 | Node/Final 双层验证 | 节点通过但目标未覆盖 | node + final reports | M2 |
| R10 | DAG Scheduler | 依赖、ready、priority、lease | scheduler event trace | M3 |
| R11 | 只读多 Worker 并行 | 两个独立 explore/research 节点 | overlapping timestamps | M3 |
| R12 | 有界 retry/replan | transient、semantic failure、replan | attempts + plan revisions | M3 |
| R13 | 全局预算与递归限制 | token/cost/step/time/depth 超限 | budget stop event | M3 |
| R14 | 写隔离与合并 | 双 worktree、无冲突、冲突 | patch/merge reports | M4 |
| R15 | Crash recovery/cancel | 调度、执行、验证时中断 | replay + cleanup report | M4 |
| R16 | DeliveryManifest/partial | 完成、部分完成、失败 | delivery manifest | M5 |
| R17 | V1 兼容与回滚 | flag off、shadow、fallback | differential report | M5 |
| R18 | 可观察性 | session inspect/replay/TUI | UI snapshot + serialized state | M5 |

## 6. 功能验收场景

### A. Conversation、Contract 与 Router

#### A01 Answer-only 不启动 Task Runtime

输入一个无需工具的稳定知识问题。

期望：

- Router 输出 `answer_only`；
- 不创建 Plan、Worker lease、Approval 或 Artifact Workspace；
- 不产生额外 Planner 模型调用；
- 回复仍进入普通会话记录。

#### A02 信息不足进入可恢复 Needs Input

输入一个缺少必要目标路径的写任务。

期望：

- TaskContract 为 `needs_input`；
- 没有写 invocation；
- 用户补充后产生 TaskAmendment 和新 revision；
- runtime 从原 task 恢复，不创建无关联新任务。

#### A03 路由拓扑正确

覆盖 answer-only、确定性工具、单 Agent、多 Agent 和高风险任务。

期望：路由理由包含风险、依赖、未知量、副作用和可并行性；模型档位选择与执行拓扑选择可独立审计。

### B. Plan 与 Validator

#### B01 三类 Builder 输出同一 schema

Fast、Single 和 Model Planner 对等地产生合法 PlanArtifact，并通过同一 schema loader。

#### B02 非法 DAG 被拒绝

至少覆盖：循环依赖、未知依赖、重复 node ID、缺 input ref、required node 无 verification spec。

#### B03 写冲突在调度前暴露

两个节点声明相同 write scope 且没有隔离策略。

期望：Validator 拒绝，或正规化为明确串行依赖；不得把冲突留给运行时随机发生。

#### B04 修复最多一次

Model Plan 第一次缺字段，修复后仍非法。

期望：只有一次语义修复调用；之后进入 `needs_input` 或 `failed`，事件中保留两版 Plan 和错误。

#### B05 目标覆盖

Plan 的所有节点均可执行，但没有节点产出某个 required output。

期望：Plan Validator 或 Final Verifier 拒绝 completed。

### C. Reducer、事件和恢复

#### C01 幂等重放

同一 event log 重放两次，并重复插入部分 event。

期望：projection 内容和哈希一致，计数不重复。

#### C02 旧 revision 迟到结果

Replan 后让旧 WorkerResult 到达。

期望：结果被归档为 stale，不改变当前 node、budget 和最终状态。

#### C03 Amendment 最小失效

只修改一个下游 required output。

期望：仅失效依赖该输入的节点；无关且证据版本一致的 verified node 保留。

#### C04 崩溃点恢复

分别在 scheduled、running、waiting_approval、verifying 状态终止进程。

期望：重启后恢复一致 projection；lease 按策略恢复或过期；不会重复产生不可幂等副作用。

### D. Invocation、Context 和权限

#### D01 Context 最小化

父任务包含两个不相关子域，只把其中一个分给 Worker。

期望：ContextManifest 不含另一个子域的私有消息、无关 Artifact 或无关 secret。

#### D02 Explore/Plan 只读强制

让只读 Worker 请求写文件、危险命令和嵌套 general Agent。

期望：能力层或 Policy 层拒绝；工作区无变化。

#### D03 Approval 参数绑定

批准 `command=A, path=X, revision=1` 后，把 invocation 改成 command B、path Y 或 revision 2。

期望：旧 Grant 全部失效，必须重新审批。

#### D04 Approval 过期、拒绝与单次使用

使用确定性时钟覆盖 TTL 到期、用户拒绝和一次性 Grant 重放。

期望：均不能执行；Reducer 记录明确原因。

#### D05 委派预算不扩大

父任务只剩 1000 token 或一层 delegation depth，子 Agent 尝试再派生。

期望：子预算不超过父剩余预算；深度超限被拒绝。

### E. Worker、Evidence 与 Verifier

#### E01 WorkerResult 四种终态

分别产生 succeeded、blocked、failed、cancelled，确认字段完整并能被 Reducer 消费。

#### E02 无 Evidence 的 Claim 不通过

WorkerResult 声称“测试已通过”但没有命令回执或测试 Artifact。

期望：Node Verifier `fail` 或 `inconclusive`，绝不能 `pass`。

#### E03 Evidence 完整性

在验证前篡改文件、命令输出 Artifact 或 Evidence 哈希。

期望：完整性检查失败，旧 evidence 不能证明新 revision。

#### E04 Verifier 类型路由

分别交付代码、Web 页面、文档和研究 Claim。

期望：选择对应 verifier；Browser Verifier 不被用于非 Web Artifact。

#### E05 Node 通过但 Final 失败

所有节点局部断言通过，但任务 required output 少一项。

期望：Final Task Verifier 阻止 completed，并给出 missing coverage。

#### E06 有界重试和 replan

覆盖一次 transient tool error、持续 transient error、语义错误和用户约束改变。

期望：只在策略允许时重试；超过预算停止；语义错误进入 Replan Gate；约束变化等待 Amendment。

### F. Scheduler 与多 Worker

#### F01 DAG 依赖顺序

构造 `A,B -> C -> D`。

期望：A/B 可同时 ready，C 只能在 A/B verified 后 ready，D 只能在 C verified 后 ready。

#### F02 真实只读并行

两个 Worker 使用可控阻塞夹具各运行固定时长。

期望：执行时间区间重叠；总耗时达到性能阈值；事件因果关系仍正确。

#### F03 Lease 防重复

两个 Scheduler 实例竞争同一 ready node。

期望：只有一个获得有效 lease；另一个不执行。

#### F04 取消传播

父任务运行两个子 Worker 时取消任务。

期望：未开始节点不再调度，运行节点收到取消，最终状态不是 completed，资源被清理。

#### F05 Worker 失败不污染兄弟结果

一个只读 Worker 失败，另一个成功。

期望：成功 evidence 保留；是否重试/继续由 node required 和 plan policy 决定。

### G. 写隔离与合并

#### G01 独立 worktree

两个写 Worker 从同一基线修改不同文件。

期望：cwd、branch/worktree 和 patch 独立；主 checkout 在 merge 前不变化。

#### G02 无冲突合并

两个 patch 修改不同资源。

期望：merge arbiter 按确定顺序应用，随后重新验证受影响节点。

#### G03 冲突停止

两个 patch 修改同一行或违反同一资源 ownership。

期望：进入 conflict/needs_input 或 replan；不自动选择任一版本，不丢用户改动。

#### G04 基线漂移

Worker 运行期间主工作区目标文件发生用户修改。

期望：base revision 检查失败；旧 patch 不直接应用。

#### G05 全路径清理

覆盖成功、失败、取消、超时和进程崩溃。

期望：无 orphan lease、进程、临时 worktree 或未登记 patch。

### H. 交付与兼容

#### H01 Completed Manifest

期望：所有 required criteria 为 pass；Manifest 引用 Artifact 和 Evidence，并记录 plan revision、成本和风险。

#### H02 Partial Delivery

一个可独立交付部分通过，另一个因外部条件阻塞。

期望：状态为 partial；清楚列出已验证成果、未完成项、阻塞和下一步，不伪装 completed。

#### H03 V1 flag off

期望：V1 characterization suite 无回归；不创建 V2 副作用。

#### H04 Shadow 模式

期望：V2 Plan/Validator 只产出审计 Artifact，不执行 Worker、不请求审批、不修改文件。

#### H05 兼容 session

加载旧 session 和旧 task Tool 文本结果。

期望：可以只读重放；必要时生成显式 legacy adapter 记录，不篡改原始历史。

## 7. 安全与故障注入矩阵

| 故障 | 注入位置 | 期望系统行为 |
| --- | --- | --- |
| Planner 返回畸形 JSON | Plan Builder | Validator 拒绝；最多一次修复 |
| DAG 有环 | Plan Validator | 不进入 Runtime |
| Scheduler 重复投递 | lease acquisition | 只执行一次 |
| Worker 超时 | Worker Runtime | cancel/timeout event；按策略重试 |
| Tool 返回成功但无 receipt | Evidence Store | verifier 拒绝 |
| Evidence 被篡改 | Node Verifier | integrity failure |
| Approval 过期 | Policy Engine | 不执行；回到 Reducer |
| 参数在审批后变化 | Tool Runner 前 | grant mismatch；拒绝 |
| event store 写入中断 | Task Runtime | 原子追加或可检测损坏 |
| 旧 Worker 迟到 | Reducer | stale result 归档 |
| 用户在合并前改文件 | Merge Arbiter | base mismatch；停止合并 |
| Worker 进程崩溃 | Scheduler | lease expiry/recovery；资源清理 |
| Provider 429/5xx | Agent Worker | 有界 fallback/retry；保留真实错误 |
| 全局预算耗尽 | 任意 invocation | 停止派生和重试；partial/failed |
| 递归 task 调用 | Worker capability | depth/capability 拒绝 |
| 用户取消 | Task Runtime | 向后代传播并停止新调度 |

## 8. 非功能指标与阈值

所有耗时阈值使用确定性本地夹具测量，排除真实 provider 网络时间。

| 指标 | 阈值 |
| --- | --- |
| Reducer 重放确定性 | 同一 log 100% 得到相同 projection hash |
| Event 幂等 | 重复应用不改变最终 projection |
| Fast path 额外模型调用 | 0 次 Planner 调用 |
| Fast path 编排开销 | p95 不高于 50 ms（不含模型/工具） |
| Scheduler ready-to-start 开销 | p95 不高于 100 ms（本地空闲 slot） |
| 三个等时只读 Worker 并行效率 | 总墙钟时间不高于串行基线的 70% |
| 未授权副作用 | 0 |
| 重复不可幂等执行 | 0 |
| Claim 无 Evidence 却通过 | 0 |
| Required node 未验证却 completed | 0 |
| 写冲突静默覆盖 | 0 |
| Crash/restart 恢复成功率 | 规定注入点 100% 恢复为合法状态 |
| Orphan lease/process/worktree | 0 |
| 全局预算超支 | 0；允许计量误差必须预先定义且不超过 1% |
| V1 flag-off 行为回归 | characterization suite 100% 通过 |

若真实 CI 环境噪声使耗时阈值不稳定，必须先保留确定性 fake-delay benchmark 作为硬门禁；真实耗时只做趋势报告，不能随意放宽正确性阈值。

## 9. 建议测试文件

| 测试文件 | 覆盖范围 |
| --- | --- |
| `tests/test_task_contract.py` | Contract、Amendment、revision |
| `tests/test_plan_model.py` | Plan schema 和序列化 |
| `tests/test_plan_validator.py` | DAG、引用、冲突、覆盖、修复 |
| `tests/test_task_runtime.py` | Reducer、幂等、重放、旧结果 |
| `tests/test_task_scheduler.py` | ready、priority、lease、并发、取消 |
| `tests/test_invocation.py` | InvocationSpec、budget、idempotency |
| `tests/test_worker_runtime.py` | Agent/Tool adapter、WorkerResult |
| `tests/test_policy_engine.py` | Approval 绑定、TTL、拒绝、scope |
| `tests/test_evidence_store.py` | hash、revision、篡改检测 |
| `tests/test_verifier_registry.py` | verifier 路由、Node/Final 判定 |
| `tests/test_delivery.py` | completed/partial/failed manifest |
| `tests/test_multi_agent_integration.py` | Fake Provider 端到端 DAG |
| `tests/test_multi_agent_concurrency.py` | 并行、lease、资源冲突 |
| `tests/test_multi_agent_worktree.py` | 隔离写、merge、冲突、清理 |
| `tests/test_multi_agent_recovery.py` | crash、replay、cancel、resume |
| `tests/test_multi_agent_compatibility.py` | V1 flag、shadow、旧 session |

测试应复用真实生产 reducer、scheduler、policy 和 verifier，不复制一套仅供测试的简化实现。

## 10. 验收执行顺序

### Gate G0：静态与 schema 门禁

1. 格式、类型和 import 检查。
2. 所有 schema round-trip。
3. Plan Validator property tests。
4. 依赖边界检查，确认 `agent_loop.py` 未反向依赖全局 Scheduler 实现细节。

失败：停止后续验收。

### Gate G1：单元门禁

执行所有新增模块单测和现有受影响单测。

建议命令：

```powershell
python -m pytest -q `
  tests/test_task_contract.py `
  tests/test_plan_model.py `
  tests/test_plan_validator.py `
  tests/test_task_runtime.py `
  tests/test_task_scheduler.py `
  tests/test_invocation.py `
  tests/test_worker_runtime.py `
  tests/test_policy_engine.py `
  tests/test_evidence_store.py `
  tests/test_verifier_registry.py `
  tests/test_delivery.py
```

失败：不得进入集成门禁。

### Gate G2：离线集成门禁

```powershell
python -m pytest -q `
  tests/test_multi_agent_integration.py `
  tests/test_multi_agent_concurrency.py `
  tests/test_multi_agent_recovery.py `
  tests/test_agent_loop.py `
  tests/test_turn_kernel.py `
  tests/test_session.py `
  tests/test_permissions.py
```

必须覆盖 Fast、Single 和 Multi 三条完整路径。

### Gate G3：写隔离与安全门禁

```powershell
python -m pytest -q `
  tests/test_multi_agent_worktree.py `
  tests/test_policy_engine.py `
  tests/test_permissions.py
```

同时运行安全/故障注入矩阵。任何未授权副作用或静默覆盖都直接判定失败。

### Gate G4：性能与稳定性门禁

运行确定性并发 benchmark、长事件重放、重复 replan、预算耗尽和 100 轮资源清理测试，生成 JSON 与 Markdown 报告。

### Gate G5：兼容与全仓门禁

```powershell
python -m pytest -q
```

随后执行：

- V1 flag-off characterization；
- V2 shadow differential；
- session inspect/replay；
- checkpoint/rewind；
- package smoke；
- release readiness。

### Gate G6：可选 Live Provider Smoke

只有 G0-G5 全通过后才允许执行。结果必须单独标记 provider、模型、成本和时间，不能修改前述本地门禁结果。

## 11. 验收证据目录

每次候选验收生成独立目录：

```text
.temp/multi-agent-v2-acceptance/<candidate-id>/
  manifest.json
  environment.json
  requirement-matrix.json
  test-results.json
  event-replay-report.json
  plan-validator-report.json
  policy-security-report.json
  concurrency-report.json
  worktree-cleanup-report.json
  compatibility-report.json
  delivery-samples/
  logs/
  summary.md
```

`manifest.json` 必须记录所有证据文件的相对路径、大小、SHA-256、生成命令和退出码。包含 secret、完整模型思维过程或敏感环境变量的内容必须在写入前脱敏。

## 12. 里程碑验收判定

### M1 Typed Runtime

必须通过 R01-R04，以及 Gate G0-G2 中对应场景。

### M2 Verified Single Worker

必须通过 R05-R09；任何 Claim/Evidence 或 Approval 失败均阻断发布。

### M3 Multi-Agent MVP

必须通过 R10-R13；至少证明两个只读 Worker 真并行。写节点仍必须串行。

### M4 Isolated Writers

必须通过 R14-R15 和 Gate G3-G4；没有 worktree 清理证据不得通过。

### M5 Default V2

必须通过 R16-R18、全仓测试、兼容性和 release readiness；完成 shadow/canary 观察后才可默认启用。

## 13. 一票否决条件

出现任一情况，候选版本结论必须为 `fail`：

1. 未经有效 ApprovalGrant 发生副作用。
2. 用户或其他 Worker 的修改被静默覆盖。
3. 无 Evidence 的 Claim 通过验证。
4. Required node 未验证却生成 completed DeliveryManifest。
5. 旧 plan 或旧 invocation 的迟到结果覆盖当前状态。
6. Retry、replan、delegation 或成本突破硬预算仍继续执行。
7. Crash/cancel 后残留可继续写入的有效 lease 或孤立工作区。
8. Shadow 模式产生任何真实副作用。
9. 通过 skip、固定成功输出或替换生产实现规避失败。

## 14. 验收结论模板

```text
Candidate:
Commit / worktree:
Schema versions:
Feature mode:

G0 Static/Schema: pass | fail
G1 Unit: pass | fail
G2 Offline Integration: pass | fail
G3 Security/Isolation: pass | fail | not-applicable
G4 Performance/Stability: pass | fail | not-applicable
G5 Compatibility/Full Suite: pass | fail
G6 Live Provider: pass | provider-risk | not-run

Requirements passed:
Requirements failed:
Veto conditions triggered:
Known limitations:
Evidence manifest:

Verdict: pass | conditional-pass | fail
Approved rollout mode: off | shadow | single | readonly-multi | isolated-write-multi | default
```

`conditional-pass` 只能用于尚未进入相应里程碑的非适用能力，不能用于安全、状态一致性或证据完整性失败。

## 15. 最终发布条件

V2 可设为默认运行时的必要条件：

1. Gate G0-G5 全部通过；G6 已执行或明确记录 provider-risk/not-run。
2. R01-R18 全部通过并有 manifest 可追溯证据。
3. 一票否决条件为零。
4. `shadow`、`single`、`readonly-multi`、`isolated-write-multi` 分阶段均完成观察。
5. V1 fallback 可用，且自动回退原因可观察。
6. 用户文档准确区分已实现能力与未来能力。
7. 最终验收报告由 Runtime、Security/Policy、Verification/QA 三个职责分别签署。
