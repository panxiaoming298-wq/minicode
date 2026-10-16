# 分级上下文与窗口外无损记忆

本模块是 Memory V2。原始事实先持久化，再根据任务规模选择 `INLINE` 或 `PAGED_MEMORY` 传给模型。`WORKING_SET` 旧模式已合并为 `PAGED_MEMORY` 的兼容输入，工作集组件继续保留。推荐单机配置使用 SQLite 和本地文件；`configure-local` 会明确启用该配置，未配置的旧安装仍保留兼容默认值。

## Skill 规则与 Memory 驻留的边界

2026-09-25 起，Skill 的生效范围由 Runtime 管理。`load_skill` 的短回执进入普通历史；必要正文及所选分支资源以独立、带来源的请求视图进入最终模型请求。SQLite 同一文件中的附加版本表保存 invocation、源文件、参数、渲染和释放记录，不修改 Memory 的全局 schema 版本。

`INLINE`、`PAGED_MEMORY`、旧 `WORKING_SET` 输入、关闭 Memory 和 shadow-write 均执行相同交付规则。ACTIVE 只证明该 attempt 已提交包含完整规则的请求；源文件、权限或必读资源变化会使旧规则失效并阻止依赖执行，直到明确重载或结束。

工作集 TTL、消息压缩、结果回收和 context rotation 不能宣告 Skill 结束。完成门通过、取消或宿主明确结束才释放规则；后续请求停止注入正文，原文仍可只读审计。读取旧记录不会激活它，重新调用会检查当前来源与权限。因此无需第二套 Skill TTL/LRU“自卸载缓存”。稳定 system/tools 前缀沿用现有 `PreparePromptCache`，实际缓存命中以供应商 usage 为准。

分页记忆、上下文容量和任务进度快照放在会话历史之后，使用带运行时来源标记的消息；不会计为真实用户输入。Web 按当前问题检索的项目记忆也随本轮追加，避免每次改写 system 前缀。缺失的必需规则仍保留原有系统优先级，完整工具调用组、推理内容和来源分页入口继续保留。必需规则、工具集合或保留历史发生变化时，缓存前缀仍可能改变。

只读 `final-verifier` 使用专门的请求布局：将当前必需来源与首次验收时仍完整可见的原始观察、候选答复移到 system 之后的稳定上下文段，动态状态、附件目录、新证据和当前对话留在后部。完整候选答复与原始读取都要保留，避免验收只看到截断摘要，或为了找回证据而增加调用。可选检索记忆仍可通过原始上下文页读取。此布局只由当前原生 Worker 身份和精确请求绑定启用；来源删除或变化后不恢复旧内容，检查点恢复后重新绑定。写入任务已有的候选验收上下文策略继续独立生效。

检查器的「运行 → 预算与用量 → 提示词缓存」显示供应商报告的输入命中率、命中 Token 和报告覆盖率，并区分主 Agent、子 Agent、记忆重排与其他辅助请求。命中率按命中 Token 总和除以已报告输入 Token 总和计算；未结算、缺失统计的请求不当作零命中。旧记录缺少报告标记时，只能确认正数命中；旧零值保留未知。首次请求及供应商缓存淘汰可能造成冷启动，前缀优化不保证固定命中率。

## 自动经验的保存与复核

文件记忆的自动整理只生成可供参考的经验，不修改 `AGENTS.md`、`CLAUDE.md` 或用户偏好。
一次提炼需要 3 至 5 条不同记录、至少 2 个仍可读取的来源会话；重复模板、无来源旧摘要不算独立依据。
模型必须返回结构化的保存／放弃决定和来源 ID。临时端口、测试计数、任务完成状态不能作为保存结果。
内容存在矛盾、缺乏可复用经验或与用户已确认记忆冲突时，应放弃提炼。该判断由模型完成，来源版本检查不代表事实已经验证。

主运行模式只在最终验收通过、工作区已集成且主 Agent 的最终结果记录了实际项目文件变更后，保存任务摘要并尝试整理经验。完全访问仅表示工具权限，普通问答和纯只读分析仍只保存会话记录，不触发项目摘要或经验提炼；用户明确保存、纠正记忆沿用独立流程。
分页记忆开启时也保留这类任务的简短历史摘要、来源会话和轮次，以及实际项目变更的数量和路径样本，供跨任务学习使用；原始事件、交接笔记仍由分页存储管理。旧版缺少项目交付依据的任务摘要保留查看，但不再作为自动学习依据；相关旧推论暂停引用。Shadow 模式不发布这类知识。
应用管理的 `.mini-code-memory-local/`、`.mini-code-memory-v2/` 和 `.mini-code/` 不参与项目改动统计、审阅或文件撤销；旧卡片、差异和撤销快照在读取时应用同一规则，不迁移或删除历史原文。`AGENTS.md`、`CLAUDE.md`、普通锁文件及项目共享记忆文件继续作为用户项目文件展示。
每次运行最多一次辅助请求，计入原有预算与用量；余额、期限或剩余预算不足时跳过。同一项目一小时内不重试，同一组依据及已确认记忆一天内不重复提炼。
请求使用当前配置的模型，无工具、无自动重试，限制响应大小与等待时间；模型明确放弃、服务不可用或输出不符合结构时不生成替代记忆。

新经验存入项目本地，保留每条依据的内容版本，90 天后须复核。访问次数不会延长这个期限。
同时记录用户已确认知识的版本；这类知识变化后，旧推论先暂停引用，等待复核，避免用户刚纠正的要求被在途生成覆盖。
来源被纠正、删除、归档或无法读取时，检索立即停止引用相关经验；过期和失效经验可由维护流程归档，原文保留。
暂时读取失败或来源在回收站时只暂停引用，不据此删除内容。恢复来源后仍须满足版本和期限条件。
自动维护遇到不可读或被移除的索引时暂停整理；写入前在锁内再次校验，不从旧缓存重建损坏的数据。
没有依据版本的旧自动经验保留查看，默认不再自动引用；用户可以通过“纠正内容”明确确认当前知识。
用户明确保存、纠正或固定的记忆不参与自动改写与过期淘汰。词语相似仅用于寻找候选，不能作为合并矛盾内容的依据。

## 单机存储与 D 盘目录

本机升级使用 SQLite 主存储、项目 Markdown 规则、单向 Markdown 导出。无需启动 PostgreSQL、MinIO、Milvus、etcd 或向量索引器。原 Docker 卷保留；本地初始化不导入旧 Docker 数据。

```text
D:/MiniCodeData/
  database/state.db   # 事件、分页记忆、状态、检查点、会话历史快照
  artifacts/          # 大型原文、压缩的历史消息对象、附件和归档
  sessions/           # 活跃会话恢复文件、会话列表、历史存储绑定
  exports/            # 可重新生成的 Markdown 查看副本
  backups/            # SQLite 一致性快照与配套原始文件、会话恢复文件
  spool/              # 配置了加密缓冲时使用
```

模型配置、凭据和扩展配置继续使用原用户配置目录。项目 `AGENTS.md`、`CLAUDE.md` 等人工规则保留在项目内并随 Git 管理；提示词读取项目根的这两种规则文件，不自动扫描导出目录。

关闭正在运行的 Web 后端，在 Python 项目目录执行：

```powershell
.\.venv\Scripts\python.exe -m minicode.paged_memory.admin configure-local --data-root D:/MiniCodeData
.\.venv\Scripts\python.exe -m minicode.web_api --check --workspace .
```

该命令保留模型和密钥配置，保存 `memoryDataRoot`、SQLite/文件后端配置，初始化新目录和数据库。它不会迁移或删除旧数据。活跃 Web 服务持有的锁会阻止运行时切换数据根。

所有原生入口使用同一用户设置；也可用 `MINI_CODE_DATA_ROOT` 覆盖数据根。`MEMORY_SQLITE_PATH`、`ARTIFACT_STORE_ROOT`、`MEMORY_EXPORT_ROOT`、`MEMORY_BACKUP_ROOT` 和 `MEMORY_LOCAL_SPOOL_ROOT` 是各路径的显式覆盖。相对路径由协调器工作区解析，隔离 Worker 不另建一份数据库。

```powershell
# 导出全部普通任务；可增加 --task-id 或 --workspace-id 筛选数据库中的任务/项目 ID
.\.venv\Scripts\python.exe -m minicode.paged_memory.admin --use-settings export-markdown
.\.venv\Scripts\python.exe -m minicode.paged_memory.admin --use-settings backup
.\.venv\Scripts\python.exe -m minicode.paged_memory.admin --use-settings restore-drill D:/MiniCodeData/backups/<backup>.tar.gz
# 恢复到新目录，拒绝覆盖运行中的目录或任何非空目录
.\.venv\Scripts\python.exe -m minicode.paged_memory.admin --use-settings restore D:/MiniCodeData/backups/<backup>.tar.gz --target D:/MiniCodeRestored
.\.venv\Scripts\python.exe -m minicode.paged_memory.admin configure-local --data-root D:/MiniCodeRestored
```

导出按租户隔离，以一致性读事务生成任务状态、决策、记忆预览和来源范围，原子替换同一筛选条件的查看副本。手工编辑不会写回 SQLite。生成标记及导出目录用于阻止重复采集；读取导出文件仍保留工具审计事件，但不再生成检索记忆。备份不依赖这些可重建导出文件。

Web 中打开已有会话的「检查器 → 上下文」，在「当前会话任务记忆」点击「导出 Markdown」即可下载同类查看副本。下载只包含该会话的分页任务状态、决策与记忆预览；长期记忆文件、完整聊天原文和附件不在此导出范围。服务端根据会话确定任务与租户，不接受客户端扩大范围；下载按只读快照生成，不在服务器导出目录落盘。没有任务资料、会话在回收站或存储无法读取时，页面显示错误并允许重试。该文件不用于导入或恢复。

SQLite 使用 WAL、10 秒锁等待和短事务；会话操作的嵌套写入使用保存点，错误时回滚整个快照发布。备份使用 SQLite backup API，并在发布备份文件前校验数据库与原文引用。恢复保留会话存储 ID，重新绑定恢复目录。

Docker 中的 CLI/headless 如仍需使用，Compose 将宿主机 `${MINI_CODE_DATA_ROOT:-D:/MiniCodeData}` 挂载到 `/data/minicode`，容器内配置使用该 Linux 路径。Compose 项目名仍为 `minicode`。Compose 仅保留 CLI/headless，不再提供远程数据库、对象存储或向量索引服务。

## 存储与检索职责

SQLite 是事件、分页记忆、任务状态、决策、依赖关系、检查点和会话历史的唯一数据库。大型原文、压缩历史消息和附件以本地不可变文件保存，SQLite 记录路径、哈希和归属。人工 Markdown 规则随项目维护；导出 Markdown 是可重建的查看副本。

PostgreSQL、Milvus、MinIO/S3 适配器及其 Python SDK 依赖已经退役；向量索引器、索引 Outbox、Embedding API/模型设置及相关管理命令已删除。旧数据库中的额外列或旧索引队列表不会在启动时被删除，也不再参与运行。旧 Docker 数据卷保留，不导入新存储。

普通检索使用 SQLite FTS 与关键词匹配，并按租户、任务、状态和来源过滤；中文及标识符的分词、关键词排序、去重、相邻原文召回继续保留。必要依赖通过结构化关系和来源校验进入上下文，不依赖补充检索是否命中。

原文持久化成功后才允许压缩模型请求中的展示内容。事件、状态、决策和检查点在同一 SQLite 事务内提交；同一轮重复写入保持幂等。大型文件的字节范围与内容哈希用于校验分页来源，摘要和记忆卡片不能代替原文。

## 两种上下文模式与独立重排策略

| 模式 | 行为 |
| --- | --- |
| `INLINE` | 短任务直接使用近期完整对话，继续持久化原始事件 |
| `PAGED_MEMORY` | 当前状态、必需依赖、工作集、SQLite 关键词补充检索，以及启用后的交接笔记和显式轮换；缺失细节可回读原文 |

模式可按任务规模自动切换，也可用 `MEMORY_MODE_OVERRIDE` 指定。分页模式不需要向量服务，也不会因为没有向量数据库而报告存储降级。

工作集仍是分页上下文中优先保留当前阶段内容的组件，有自己的预算、有效期和来源；合并的是重复的运行模式。使用 `PAGED_MEMORY` 不等于每次调用重排模型。自动模式先达到分页门槛，再由独立重排许可、`memoryRerankEnabled` 和 `memoryRerankPolicy`（`adaptive` / `always`）等策略决定是否允许尝试重排；缓存、共享预算、截止时间和调用次数限制继续生效。

2026-09-24 起，兼容规则如下：

- 显式 `memoryModeOverride: "WORKING_SET"` 或关闭自动选择后的 `memoryModeDefault: "WORKING_SET"` 仍可使用，运行时显示 `PAGED_MEMORY`，保留本地检索、不请求模型重排的行为。
- 未启用下文的上下文利用率策略时，原自动进入 WORKING_SET 的较低门槛直接进入 `PAGED_MEMORY`；原较高门槛控制独立重排许可。跨会话、恢复、并行及审计条件继续有效。
- 旧配置键 `memoryWorkingSetContextRatio` / `memoryWorkingSetStepThreshold` 是旧策略的分页门槛（默认 0.30 / 12），`memoryPagedContextRatio` / `memoryPagedStepThreshold` 是自动重排许可门槛（默认 0.60 / 40）。启用 `memoryContextUtilizationEnabled` 后，旧 30%、步数和累计工具量不再单独触发分页；60% 仍只参与独立重排许可。工作集 token 预算键含义不变。
- 显式选择 `PAGED_MEMORY` 可跳过自动重排许可门槛，但仍受重排总开关与实际调用策略限制。如需持续分页且完全使用本地检索，新配置使用 `PAGED_MEMORY` 加 `memoryRerankEnabled: false`。
- 旧 SQLite 检查点先按原始内容校验哈希，再在内存中转换模式并恢复原重排许可；旧行和历史事件不重写。新检查点保存规范模式及独立重排许可、稳定次数和观察标识，跨进程恢复不会因统一模式误增许可。

`rerank_admission` 指标记录许可、总开关和原因，`rerank` 指标记录实际尝试、调用数与用量。配置摘要输出规范模式，另用 `legacy_mode_inputs` 和 `rerank.legacy_local_only` 说明旧配置兼容行为。源码中的 `MemoryMode.WORKING_SET` 暂时保留用于解码，不再有独立上下文构建分支。

## 有效上下文判断与工具结果预览

以下两个优化开关默认关闭，可独立开启和回退；SQLite、本地原文、Markdown 规则和单向导出保持原有职责。

| 用户设置键 | 环境变量 | 行为 |
| --- | --- | --- |
| `memoryContextModePolicyEnabled` | `MEMORY_CONTEXT_MODE_POLICY_ENABLED` | 根据有效上下文压力与轮换基准选择自动模式 |
| `memoryToolResultProjectionEnabled` | `MEMORY_TOOL_RESULT_PROJECTION_ENABLED` | 改善大结果预览，并对近期完整工具批次的可选正文生成有来源的预览 |

环境变量优先。设置为 `true` 后由新启动的 Worker/CLI 读取；已运行的执行器继续使用启动时的配置。设置为 `false` 可以分别恢复旧模式策略或旧结果展示，不需要删除数据库字段。若已启用下文的上下文利用率策略，回退它依赖的模式、弹性预算、笔记或轮换开关之前，应先关闭 `memoryContextUtilizationEnabled`；配置会拒绝缺少依赖的组合。

若同时开启下文的工具正文回收策略，关闭 `memoryToolResultProjectionEnabled` 前还需先关闭 `memoryToolResultReclaimPolicyEnabled`。

模式策略保留累计历史统计，另算轮换后尚未按软预算裁剪的内容压力，计入必需上下文和工具预留，最后仍按实际消息和工具定义做硬预算检查。新轮换事件保存请求边界处的计数基准；检查点同时保存计数来源和降档观察，恢复时继续使用。新进程未恢复检查点时，不减去其他 run 的工具计数，而结合新 run 计数和轮换后可见工具历史保守判断。旧轮换事件缺少基准时继续使用累计计数，下一次成功轮换建立新基准。

模式和重排许可的自动降级分别要求阶段边界与稳定观察（默认 5 次）；启用有效上下文判断时，同一个 run/step/phase 重复构建不能累加次数。有效轮换期间保持 `PAGED_MEMORY`，避免自动进入 `INLINE` 后把旧历史重新带回；这个最低要求本身不开放模型重排。跨会话、重启恢复、并行和审计等条件继续有效，手动模式和关闭自动选择时的配置仍优先。轮换身份、笔记或历史前缀无效时回到既有完整历史/分页保护。

增强预览优先保留状态、错误、头尾片段，以及当前问题点名且在结果中较少出现的词或标识符。`read_file` 预览优先展示正文；标注的 source line 是保存的工具输出中的行，可能包含文件元数据头，并非直接等同于工作区文件行号。预览是导航信息，不能替代完整证据。

多个中等结果即使分别未达到落盘阈值，也会按近期消息预算分配可选正文空间。只对已在当前任务 SQLite 中完整保存、哈希匹配的成功结果建立预览；失败、导航、强制内联和必需来源保留原文。声明必需证据分面的请求暂不缩减工具正文，避免预算外的必需卡片被漏判。每个预览保留状态、关键片段、内容哈希和 `memory_get(source_event_id=..., unit="BYTE", start=0)` 回读位置；回读访问保存的原版本，不重新执行工具。

必需正文、调用参数和最小有效回执可能超过近期软预算，此时记录 `minimum_exceeds_budget`，最终硬预算检查仍可拒绝过大的请求。原始历史与完整工具批次不变；请求投影和共享预算再次渲染使用同一个已验证快照，不额外检索或调用模型。

指标包括 `context_pressure`（完整历史、有效历史、当前阶段及累计计数）、最终消息/工具定义的 token 估算、`provider_budget_exceeded`、`tool_result_projection` 和已有检索/重排观测。估算不是供应商账单；输入变短可能增加回读次数，应同时比较整个任务的模型用量与验收结果。本次没有增加工作集查询缓存、Snip 工具或按固定时间清理旧正文的路径。

## 工具正文回收收益与稳定性

`memoryToolResultReclaimPolicyEnabled` 在现有结果投影中增加最低收益和有界复用，依赖 `memoryToolResultProjectionEnabled`。库默认关闭；本机已验证并启用。当前控制上下文的 Memory V2 仍绕开旧 MicroCompactor，不按空闲分钟数决定信息失效。

每批新回收或进一步缩减，默认至少释放 **512 个正文 token，且达到本批受影响正文的 10%**；两项同时满足才采用新投影。已经回收过的结果，收益基线使用先前选择的回执，不重复计算从原文到回执之间已经省下的部分。收益不足时沿用原有回执，首次回收则暂时保留原文；软分区预算可以因此暂时超出，最终请求硬门禁继续生效。进入 90% 保护阶段或有效输入估算达到硬上限时，可允许有正收益但不足门槛的回收，诊断记为 `pressure_override`。

同一 Runtime、用户目标、任务约束、验收条件、模型和轮换 epoch 内，优先保留此前的回执正文，新结果先让出可选空间。预算上升不会自动把旧回执展开成完整原文；小幅下降不会反复改写旧正文，只有新增收益足够时才进一步缩减。目标、约束、模型、epoch 或轮换有效状态改变，切回 INLINE，或关闭策略时，复用状态失效。

每次请求仍重新读取并验证原生 SQLite 来源、哈希和保护条件。错误、控制消息、必需来源、强制内联和主动记忆回读不参与回收；调用 ID 重复、调用/结果不匹配或批次尚未完成时，相关正文不获得回收资格。某来源后来成为必需证据时立即停止复用其预览。原始文件随后变化不会改变过去保存的结果；`memory_get` 返回对应来源 ID 的历史版本，不重新执行工具。

复用状态仅属于当前 Runtime，最多 128 条，回执及索引元数据序列化体积最多 1 MiB，按使用顺序淘汰。它不进入 SQLite 事实表或检查点；进程恢复从原生来源重新校验和生成。一个请求生成的投影快照保持冻结，后续请求或共享预算重新渲染不会修改它。

用户设置示例：

```json
{
  "memoryToolResultProjectionEnabled": true,
  "memoryToolResultReclaimPolicyEnabled": true,
  "memoryToolResultReclaimMinTokens": 512,
  "memoryToolResultReclaimMinRatio": 0.10
}
```

环境变量分别为 `MEMORY_TOOL_RESULT_RECLAIM_POLICY_ENABLED`、`MEMORY_TOOL_RESULT_RECLAIM_MIN_TOKENS`、`MEMORY_TOOL_RESULT_RECLAIM_MIN_RATIO`，优先于设置文件；最小 token 为正整数，比例须满足 `0 <= ratio < 1`。关闭策略总开关可恢复原有结果投影，不影响来源、轮换或历史。新 Worker/CLI 加载设置，运行中的执行器不热更新。

`tool_result_projection.reclamation` 记录决策原因、新增释放量、最低门槛、复用/改变数量、保留条数和序列化体积。空间够用且没有旧回执时不启动回收。策略减少旧正文的不必要变化，但请求中的其他部分仍可能变化；不会据此宣称缓存必然命中或费用必然降低，`cache_cost_estimated` 明确为 `false`。供应商真实用量和缓存命中仍需单独观察。实施记录见 `Package/AgentRuntime/Data/Engineering/ResultReclamation/`。

## 运行上下文上限与弹性分区预算

`contextWindow` 是本地运行上限，实际取它与已识别模型上下文能力的较小值；同时存在旧键 `maxContextTokens` 时也取较小值。未知模型继续使用保守的 128000 能力兜底，不通过设置虚增窗口。CLI、Web Worker、无 ContextManager 的 AgentLoop 及 Memory Runtime 使用统一解析；ContextManager 的模型切换、fork 和持久化恢复保留上限。

当前 `deepseek-flash` 已按 [DeepSeek 官方模型说明](https://api-docs.deepseek.com/quick_start/pricing/) 识别为 1M 能力，本机选择 256000 的运行上限。它是容量上限，不要求每次填满。按默认输出预留 4096、安全余量 1024，消息与工具定义合计输入上限为 250880；工具至少预留 2048，实际 schema 更大时按实际估算扣除。任务总 token 额度仍由运行预算单独控制。

在用户 `settings.json` 中启用：

```json
{
  "contextWindow": 256000,
  "memoryElasticContextBudgetEnabled": true
}
```

新选项默认关闭，可用 `MEMORY_ELASTIC_CONTEXT_BUDGET_ENABLED` 环境变量覆盖。启用时，未显式配置的基准额度为：

| 分区 | 普通任务 | Primary＋Ultra |
| --- | ---: | ---: |
| 工作集 | 3000 | 3000 |
| 补充记忆 | 2000 | 2000 |
| 近期实际会话 | 4000 | 12000 |
| 历史事件卡片 | 1500 | 1500 |

已有 `memoryRecentEventTokenBudget` 控制近期实际会话，显式设置继续优先；新增 `memoryRecentMemoryTokenBudget` / `MEMORY_RECENT_MEMORY_TOKEN_BUDGET` 独立控制历史事件卡片。工作集和补充记忆继续使用原配置键。关闭新选项且没有显式配置时回到旧的 2000/1200/1500 行为。

仅启用弹性分配时，先保留有需求分区的基准份额，再把空余额度优先给近期会话、工作集和补充记忆；同时启用下文的利用率策略时，改为按需求和基础权重扩容。先扣除固定上下文与工具预留，并消除相同卡片的重复计费；不增加检索轮次。卡片不能拆分留下的空隙可供近期会话使用。必需项与完整最新工具批次可以超过软额度，最终请求仍由硬门禁检查。`context_allocation` 报告基准、需求、分配、借用和实际选中的记忆 token。

同时启用结果预览时，空间够用的结果保留原文；需要缩减时，保留重要摘录并按各结果份额增加有来源行号的连续正文，保持缩进。对于可解析的 Python 文件，优先包含命中的完整函数及直接命名调用的辅助函数；其他内容采用连续邻近窗口。这不是完整静态依赖分析，缺少的间接依赖仍须回读。额外邻近扩展限制在 12 行，避免为用满预算带入大量无关注释。来源范围仍相对于保存的工具输出。

这些变化不修改原始历史、SQLite 模式或本地来源文件。新启动的 Worker/CLI 读取新配置；已运行的进程沿用其已加载版本。未启用下文的利用率策略时，关闭 `memoryElasticContextBudgetEnabled` 可回退分配和正文扩展；`contextWindow` 可独立调回 128000。

## 按需求增长与 80% / 85% / 90% 保护

2026-09-24 新增 `memoryContextUtilizationEnabled`。库默认关闭，本机已验证并启用。它依赖 `memoryContextModePolicyEnabled`、`memoryElasticContextBudgetEnabled`、`memoryHandoffNotesEnabled` 和 `memoryContextRotationEnabled` 同时启用。

新策略保留 `INLINE` / `PAGED_MEMORY` 两种运行模式和工作集组件。低压力下不会仅因 12 步、旧 30% 比例、累计工具量或大附件数量收紧当前正文；跨会话、恢复、并行和审计仍可提前要求分页能力。分页的分区数字是基础份额：连续近期会话、工作集、通过相关性筛选的补充记忆和近期事件正文均可按需求扩容。先保障有需求分区的基础份额，再按基础份额作为权重分配空余容量；某分区需求不足时，空余继续分给其他有需求的分区。来源卡片保持完整，不能使用的零散额度可让其他完整卡片或近期会话使用。低于补充检索优先级（45）的可选诊断仍只使用基础份额，不因窗口空闲而扩张。

候选准备与最终分配使用同一可选容量上限。近期事件不会在分配前被固定裁至 1500 token，补充检索也不再被基础额度总和提前限制；原有候选数量、相关性筛选、去重、来源验证和超时限制继续生效，不增加检索轮次。`context_allocation.policy` 为 `demand-context-sections-v3`，同时报告各分区的 `growth_limits`。

扩展池上限按可用模型输入、固定必需内容、工具定义预留、增长预留和状态包装余量共同计算，并为轮换建议线保留空间。整个任务的共享 token / 金额额度依然独立约束实际请求；256000 窗口不代表每个任务都有使用这么多输入的剩余额度。正文更多也可能增加每轮输入费用，不承诺节省总 token。

| 压力阶段 | 默认阈值 | 行为 |
| --- | ---: | --- |
| `write_note` | 80% | 自动模式进入分页能力，提示保存有来源的笔记 |
| `rotate` | 85% | 有覆盖当前历史且来源有效的新笔记时自动轮换，否则提示先完成交接 |
| `protect` | 90% | 阻止新增业务工具，在容量上限内保留交接证据并继续受限恢复 |

原生请求按总窗口规划三条参考线；显式输出预留或输出恢复需要更多空间时，可提前触发整理。常规 400000 窗口的参考线为 320000 / 340000 / 360000。发送前先完成分页和可选内容分配，再用实际请求计量更新压力；已经收起、仅保存在可检索历史中的内容不再抬高当前占用。诊断仍保留 `unpaged_input_tokens`，便于追溯初始历史预测。没有原生请求预算的旧调用路径继续按可用输入容量估算。

请求计量独立于累计费用预算的保守预留。支持的准确模型/路由使用本地固定版本 tokenizer；已知 DeepSeek Flash 模型及版本别名的兼容路由使用同一 tokenizer 作参考，标记为 `reference_tokenizer`，不能据此宣称提供商实测。字面特殊 token 只对该片段保守计数，不会让整段正文退回字符估算。其他文本显示中英文估算，但容量保护仍使用字节上界，避免随机标识符、Base64 和 emoji 被显著低估。同一执行者、同一用途下，模型、路由、请求头、计量方法及历史前缀完全一致时，可采用上次提供商实测输入加新增内容估算；无法识别 tokenizer 时也只对新增部分保留字节保护。前缀被改写、分页或轮换则重新计量，不能按旧比例直接缩小新请求。完整实测记录随原有用量账本恢复，部分响应和旧版本计量记录不能作为新计量的校准依据。诊断 `context_input` 只保存数字、方法和指纹，不保存正文。

容量保护在计量值上增加 10%（至少 1024 token）的误差余量，再结合下一批工具增长预测和输出空间判断整理时机。未知媒体或协议字段仍使用保守兜底检查。最终发送使用同一计量方式复核完整请求与实际输出额度，累计 Token/金额预算依然按原有独立账本预留和结算。因此保护可能先于显示的输入占用百分比触发；保护余量不表示已用空间。

增长预留默认 8192 token，小窗口最多先预留输入容量的十分之一；本 epoch 已观察到的更大单个工具结果可以提高预留。执行每批工具前还会按结果大小提示、调用参数和实测峰值预测整批增长，超过保护线时整批新增业务操作均不执行，避免多个并行调用分别通过却合计超限。预测不是严格输出上界，最终真实请求仍按完整工具 schema、原生异步任务视图、输出和共享预算做硬检查。

保护期间允许 `memory_note_update`、`context_rotate`、工具发现、受限的记忆/附件回读，以及已有异步任务的 `poll_agents` / `wait_agents` / `cancel_agent`；新增业务读写、派生新任务和恢复执行等操作暂缓。原有 Worker 权限继续生效。保护不会终止已经运行的异步任务，下一次请求仍从原生调度状态生成任务视图。

达到 90% 时不再把可选预算池骤降到 10500（Primary＋Ultra 为 18500）。恢复步骤仍按相同的可用容量、增长预留和轮换目标分配正文，并以 `recovery_context_preserved` 标记；总预算不足的可选内容仍可分页，必需内容超限仍拦截发送。旧历史前缀只在新笔记通过覆盖与来源校验、轮换成功后整体回收；未验证或过时的笔记不会触发自动轮换。

笔记保存时由服务端封存当前历史前缀的长度和哈希。自动保护性轮换必须使用覆盖当前历史、来源验证通过且含 `next_steps` 的新版本笔记；笔记之后新增用户输入、业务结果或历史被改写时，需要先刷新。成功的笔记更新/工具发现回执和普通进度文字可作为后缀保留。旧笔记仍能读取，启用新策略后须刷新才能用于轮换。手动轮换在此策略下也接受同样的历史覆盖校验。

默认最多进行 6 个不同模型步骤的保护性交接尝试，同一步重复构建不累计。计数随检查点保存，换 epoch、退出保护或收到新的用户输入后重置；超限则明确暂停新增工作，原历史和任务状态继续保存。显式强制 `INLINE` 且进入保护时会明确停止，因为该模式不支持交接轮换。程序不会把失败的笔记保存或轮换当作成功。

本机设置使用以下值：

```json
{
  "memoryContextUtilizationEnabled": true,
  "memoryContextNoteWarningRatio": 0.80,
  "memoryContextRotationTargetRatio": 0.85,
  "memoryContextProtectionRatio": 0.90,
  "memoryContextGrowthReserveTokens": 8192,
  "memoryContextRecoveryMaxSteps": 6
}
```

对应环境变量为 `MEMORY_CONTEXT_UTILIZATION_ENABLED`、`MEMORY_CONTEXT_NOTE_WARNING_RATIO`、`MEMORY_CONTEXT_ROTATION_TARGET_RATIO`、`MEMORY_CONTEXT_PROTECTION_RATIO`、`MEMORY_CONTEXT_GROWTH_RESERVE_TOKENS`、`MEMORY_CONTEXT_RECOVERY_MAX_STEPS`，环境变量优先。阈值必须满足 `0 < 笔记提醒 < 轮换建议 < 保护 < 1`。将总开关设为 `false` 即回到此前策略，历史、笔记、检查点和 SQLite 数据均保留。新 Worker/CLI 生效，不热替换正在执行的任务。

`context_utilization` 指标记录阶段、当前/预测压力和恢复步数；`context_tool_forecast` 记录整批调用预测，`context_allocation` 的策略名变为 `demand-context-sections-v2`，显示基础、需求和实际分配。实施与验证记录见 `Package/AgentRuntime/Data/Engineering/ContextUtilization/Plan.md` 和同目录 `Validation.md`。

## 当前函数与一层 Python 依赖

`memoryCodeFocusEnabled: true`（环境变量 `MEMORY_CODE_FOCUS_ENABLED=true`）开启版本校验后的代码工作集。此功能在 PAGED_MEMORY（包括旧 WORKING_SET 兼容输入）中生效，库默认关闭；本机可独立启用或回退，不改变 256000 上限或分区额度。

目标选择依次考虑当前用户请求下的明确读取目标、成功编辑及聚焦到单一函数的读取窗口；没有明确操作时，使用用户点名且能唯一匹配的函数。例如 `read_file(path="pkg/service.py", focus_symbol="Handler.run", start_line=1, line_count=200)` 明确指定函数；`focus_symbol` 是保留提示，不改变读取窗口。同名或多义目标不会自动猜测。新的用户文本清除旧的操作意图绑定，普通依赖阅读不会覆盖已指定的函数。

来源来自真实成功工具回执，按任务、执行者、工作区及 Worker 契约隔离。只将实际读到的窗口拼接为完整模块；未读部分不会由磁盘校验补进正文。Python 普通绝对/相对导入、别名和同文件函数可以解析一层直接调用。支持跨文件完整函数正文和相关导入语句，不执行导入、不扫描全仓库；局部遮蔽、重导出、模块/包歧义、闭包、动态分派及未知包根等记录为未解析。没有读过的依赖给出候选路径，仍需正常读取。

每次新请求检查文件内容 SHA-256，文件改变、删除或来源不完整时停止注入相应当前正文并提示重新读取；同尺寸、同时间戳的改动也会失效。旧工具输出保持原样，是历史版本。轮换或主执行者恢复后从 SQLite 重新构建目标；Worker 之间不自动继承目标。本机制不替代原生文件读取义务，也不声明运行时调用图完整。

每次最多检查最近 256 条相关代码回执、8 个文件、每文件 256 KiB、每文件 32 个读取窗口；最多补入 4 个直接依赖。当前显式目标优先保留文件名额。Python 语法分析还限制为 20000 个节点、100 层深度，每个目标检查前 32 个调用；超限或无法解析时保守降级。无关日志不会占用相关回执名额。

完整函数作为较高优先级的 CODE_FOCUS 工作集卡片参与原有预算，不额外占一份不受控额度。函数放不下时整块省略，保留预算允许的状态提示和回读路径；卡片标注工作区文件行号、文件哈希和原始工具事件 ID。共享运行预算仍可分页这些可选卡片，必需指令和证据优先。一次请求生成后冻结，再次渲染不读取磁盘。指标 `code_focus` 区分已验证的候选正文、实际选入的 `selected_body_ids`、过期来源与未解析调用；`ready` 表示候选验证完成，不代表全部正文都已装入请求。

关闭 `memoryCodeFocusEnabled` 可回退代码工作集；既有工具预览、SQLite 历史和其他上下文功能保持各自配置。新进程加载新代码/设置；已有进程不会热更新。此功能提高已观察代码的连续性，不保证自动捕捉所有未来编辑意图。

## 原生异步任务状态跨分页保留

开启原生异步 Agent 的 Runtime，在 primary 的 OpenAI/Anthropic 请求发出前，自动从调度器权威状态生成一次当前任务视图。它在历史分页和显式轮换之后加入实际请求，因此即使旧 `spawn_agents` 回执已离开上下文，模型仍能看到任务 ID、会话/代次、运行状态、取消标记、结果是否可读及相关等待和问题。此能力跟随既有原生异步配置，无须增加记忆开关；`INLINE`、`PAGED_MEMORY` 和旧模式兼容输入均适用。

每次请求、预算预约等待后的重试都会重新读取状态；一个视图在同一 Reducer 锁内生成，并验证当前 run、primary attempt、plan、contract、幂等键、租约及进程所有权。只在实际请求中加入，不追加到聊天历史、交接笔记或轮换锚点。重启后从已验证的 Runtime 快照重新生成，不依赖模型在笔记中抄写任务列表，也不在 SQLite 中新增第二份调度状态。

任务正文和制品仍用 `poll_agents(task_ids=[...])` 等原生工具读取，未完成工作继续用 `wait_agents` 等待。终态、已显示、已消费消息都不等于结果已处理或任务已验收。系统尚无通用的结果处理确认记录，因此保守保留终态任务的身份和结果入口；失败被已验证的重派任务解决时另有标记，旧失败证据仍保留。旧代次和旧输入版本不自动成为当前有效证据。

任务视图参与共享预算报价、最终输入预约和上下文硬上限检查；不能作为可选旧记忆被分页删除。任务描述与错误正文只提供有截断标记的短预览，任务身份与状态不截断。结构化视图超过 32 KiB，或必需状态与其他必需输入放不进预算时，在 HTTP 前明确拒绝请求。无任务、子 Worker 和辅助模型调用不添加 primary 任务视图。

请求诊断中的 `agent_context` 记录任务 ID、状态版本、视图哈希、字节数和最终预算检查。新启动的 Worker/CLI 加载此实现；已运行进程不热替换。上述保证覆盖原生 `spawn_agents` 任务，不扩展旧后台 Shell 的内存注册表。

## 动态候选预算与可选重排序

`MEMORY_CANDIDATE_LIMIT` 与 `MEMORY_LEXICAL_CANDIDATE_LIMIT` 控制关键词候选窗口。启用 `MEMORY_ADAPTIVE_RETRIEVAL` 后，候选不足、重复或缺少要求的证据方面时，可在最大预算内扩展窗口。`MEMORY_CONTEXT_CARD_LIMIT` 控制最终卡片数量。

`MEMORY_RETRIEVAL_TIMEOUT_SECONDS` 限制单次查询；`MEMORY_CONTEXT_RETRIEVAL_TIMEOUT_SECONDS` 限制补充检索、重排序和复核的整体时限。SQLite 查询超时后恢复连接状态，后续操作可正常使用。

可选 LLM 重排序由 `MEMORY_RERANK_ENABLED`、`MEMORY_RERANK_PROVIDER`、`MEMORY_RERANK_MODEL`、`MEMORY_RERANK_BASE_URL` 和 `MEMORY_RERANK_API_KEY` 配置。它对关键词候选评分，与 Embedding、向量数据库无关。保留独立时限、熔断、按任务隔离的缓存和来源版本复核；失败时保留已经验证的关键词结果。

除完整候选批次缓存外，同一运行内可复用未改变卡片的独立相关性评分。候选换序或增加时，仅为未命中的卡片请求评分；问题、租户/任务、提供商与模型/凭据路由、卡片内容或来源版本改变时失效。缓存最多保留 512 张卡片，命中不续期，继续使用既有 TTL；评分只影响可选候选排序，不能代表来源真实、验收通过或跳过后续来源复核。

## 有界检索与最终请求去重

自动补充检索的查询仍限制为 400 字符，以当前用户请求为主，保留首尾和有界的中间原文片段，优先包含代码标识、文件名、版本与否定/条件词。中间扫描最多 16,384 字符、24 个匹配，不解析整个超长日志，也不改写原始消息。只有当前请求验证为 `current` 的交接笔记才提供至多两个线索（首个 `next_steps` 和首个 `open_questions`，合计最多 80 字符）；过期、无效或仅用于恢复封存历史的笔记不提供新线索。单条缓存只复用相同用户输入、目标、阶段和笔记线索的查询文本，不缓存检索结果或跳过来源检查。

`memory_search` 卡片补充记录中的 `run_id`、`step_id` 和 `created_at`，用于定位历史观察。`created_at` 是卡片创建时间，补建索引时不等于原始事件发生时间，也不是当前状态证明；代码或环境变化后仍需重新验证。`history_search`、`memory_get` 和已有原文分页继续提供历史入口。

轮换生效后，可选原文投影卡片在最终请求中再次去重。每次构建最多用 50 毫秒（且不超过配置的上下文检索时限）准备来源证明，按当前 tenant/task 校验来源；预算调整和重复渲染复用这个快照，不再查询数据库。只有最终请求包含对应角色的完整原文、来源哈希与卡片快照仍匹配时才省略卡片正文。卡片留在上下文包中，原文被裁短或移出请求时自动恢复；必需证据、要求的分面、错误、决策和约束不参与该去重。来源缺失或读取超时则保留卡片。

以上优化不增加数据库迁移、模型调用或中间件，不改变笔记版本冲突处理、轮换提交和硬预算门禁。局部输入减少不等于整个任务用量必然减少；固定样例、耗时开销和回归结果记录于 `Package/AgentRuntime/Data/Engineering/PagedMemoryOptimization-20261011/Validation.md`。

## 可选的任务交接笔记

启用上下文利用率控制和自动轮换时，分叉、重试及长对话续聊会在模型请求前先检查完整上下文。运行时优先验证并复用已有交接笔记；仍达到整理阈值而无法直接恢复时，先进入交接准备，模型只获得笔记、轮换、受限历史查询及已有 Agent 控制工具。实际工具定义、技能规则与输出余量一起参与检查。容量充足的请求直接执行。

准备阶段使用当前任务的来源与修订号，父任务的证据编号不会直接成为分叉的有效证据。原始会话和全部用户要求保留，模型把关键决定、完成情况、待核实问题和下一步写入交接笔记。笔记验证成功后自动轮换，完整普通请求通过原窗口容量检查才恢复业务工具；已完成操作不会自动重放。准备与超限恢复共用有界请求次数，只有真实容量不足才可能进入既有临时借用流程，用户设置保持原值。

进度记录会提示“正在整理本轮上下文”和“上下文已准备好”，便于区分准备与业务执行。请求诊断 `context_borrowing.trigger=context_preparation` 表示提前准备，`normal_context_admission_failed` 表示真实请求超限后的恢复。主界面优先显示最近请求的提供商实际输入（含缓存），返回前显示该请求的校准值或文本估算；容量保护估算和整理参考线放在占用详情中。旧记录也优先显示已经保存的实测输入，不需要迁移或重新调用模型。新启动的后端进程加载这些行为，已有进程不热替换。

交接笔记为已有历史检索增加模型主动维护的工作记录，默认关闭。Memory V2 接管上下文且进入 `PAGED_MEMORY` 时，开启后可用 `memory_note_update`；`INLINE` 和 shadow write 不启用笔记。旧 `WORKING_SET` 输入自动按分页模式兼容。当前单机架构沿用 SQLite Event/TaskState 和本地文件来源，不依赖 PostgreSQL 或向量数据库，也不增加独立摘要模型调用。项目 Markdown 规则和单向导出继续保留各自职责。

在启动 MiniCode 后端的 PowerShell 中设置以下环境变量，再重新启动后端，使新建 Runtime 读取配置：

```powershell
$env:MEMORY_HANDOFF_NOTES_ENABLED = 'true'
# 可选：显式指定上限会覆盖按任务档位选择的默认值。
# $env:MEMORY_HANDOFF_NOTE_TOKEN_BUDGET = '2048'
```

也可在已有用户设置中使用 `memoryHandoffNotesEnabled: true`。未显式设置预算时，普通任务的交接笔记上限为 2,048 token；`runtimeOrchestration: primary` 且 `executionProfile: ultra` 时为 4,096。可通过 `memoryHandoffNoteTokenBudget` 显式指定 256–4,096 的上限，`MEMORY_HANDOFF_NOTE_TOKEN_BUDGET` 环境变量优先；留空使用用户设置或上述默认值。已有显式设置（包括 1,200）继续生效。

该上限包含笔记正文、来源 Event ID、JSON 结构和笔记说明文字，按实际估算长度计入独立的固定上下文开销，不预先占满上限，也不占用最近记忆的基础份额。各字段最多 8 条、单条最多 1,000 字符、整份最多 16 个来源的校验继续生效；超限拒绝保存并保留旧笔记，完整请求仍受消息、工具 schema 与输出预留的总预算限制。开关关闭后不再注册工具或注入笔记，历史记录继续保留。上述设置不会强制短任务进入分页模式。

模型先从 `Handoff note status` 获取 `revision`，按需通过 `discover_tools` 选择 `memory_note_update`，然后提交：

```json
{
  "expected_revision": 0,
  "reason": "stage_complete",
  "confirmed_findings": [
    {"text": "登录测试通过，未修改数据库结构。", "source_event_ids": ["实际来源 Event ID"]}
  ],
  "ruled_out": [],
  "open_questions": ["过期会话是否维持原错误响应？"],
  "next_steps": ["检查过期会话分支。"]
}
```

示例来源必须替换为本任务真实 Event ID，可从记忆来源或工具回执中的 `source_event_id` 获取；工具调用 ID、`result_id`、Artifact ID 不能代替 Event ID。`confirmed_findings` 和 `ruled_out` 的每一项都必须提供来源；另外两个字段记录问题和行动。四个数组必须全部提交，更新为完整替换，空数组可以清空对应部分。每个数组最多 8 项、单项文本最多 1,000 字符、每条结论 1–4 个来源，一份笔记最多 16 个不同来源。来源必须属于当前 tenant/task，且完整原文及 hash 可验证；笔记自身和记忆导航回执不能递归作为证据。来源可验证不代表模型结论必然正确。

笔记提示要求结论保留适用条件，排除项写明原因及何时需要重新考虑；未经验证的判断进入 `open_questions`，`next_steps` 写可执行动作。沿用原有四字段格式和完整来源校验，不新增独立的事实确认流程。

`reason` 支持 `stage_complete`、`budget_pressure`、`pause`、`manual`，省略时为 `manual`。阶段边界和预算压力会提示模型考虑更新；运行时不会强制每轮重写。工具具有写入效果，受已有 Worker 权限限制，只读 Worker 无权调用。版本冲突会明确失败，必须重新构造上下文、读取最新版本后再更新，不能自动重放旧笔记。

正文作为不可变 Event 保存；TaskState 只保存各执行者的最新版本、来源 ID 和 hash，二者在同一事务内提交。主执行者的笔记可跨 run 恢复；Worker 的笔记绑定 run 与稳定 node ID，可在同一 Worker 恢复时继续使用，不被其他 Worker 自动注入或覆盖。普通 `memory_get` 仍遵循已有的任务级历史读取范围；笔记分属执行者并不建立同任务内的新保密边界。

每次构建上下文都重新验证来源、任务契约和 Worker assignment。笔记同时绑定写入时最新用户输入：后续用户文本变化会保守地标为 `stale`，即使新文本只是“继续”，模型也需要重新评估后更新；相同输入和相同契约的正常恢复仍可加载。进度和检查点变化不会单独使笔记过期。过期、损坏、缺失来源或超过配置预算时，正文不自动注入，状态中保留版本供重新提交。用户指令和 required 证据始终优先于笔记。

当完整的成功更新调用仍在近期历史中，显示层复用其中的笔记字段并附来源提示，避免重复注入正文；调用和回执仍作为整体保留。离开近期历史后再从存储加载。未应用显式轮换时，共享请求预算可以分页省略这份辅助笔记，并通过现有 `runtime_context_read` 回读原上下文页；不会因此移除必需的用户约束。已应用轮换时，有效交接笔记成为必须携带的信息。

指标增加 `handoff_note_tokens` 和 `handoff_note`，后者包含版本、`empty/current/stale/invalid_source/over_budget` 状态、是否入选及是否建议更新。数据库初始化执行可重复的增量字段迁移；关闭功能不删除字段或笔记。笔记与历史检索协作不保证总 token 或任务成功率必然改善。

## 可选的显式上下文轮换

`context_rotate` 在同一任务内切换工作上下文：下一次模型请求收起已结束的探索对话和普通旧记忆卡，带上有效交接笔记继续工作。SQLite 原始事件、用户可见会话和本地原文保持完整，旧细节仍可通过 `memory_search`、`memory_get`、`artifact_read` 查找。它是 MiniCode 自己的实现，不调用 Codex 的 `new_context`。

代码默认关闭。在用户设置中同时配置 `memoryHandoffNotesEnabled: true`、`memoryContextRotationEnabled: true`；或在启动进程前设置：

```powershell
$env:MEMORY_HANDOFF_NOTES_ENABLED = 'true'
$env:MEMORY_CONTEXT_ROTATION_ENABLED = 'true'
```

环境变量优先。Web 的新任务 Worker 会重新读取用户设置；已运行任务不热切换，环境变量修改需要重启相应启动进程。此开关不强制短任务进入分页模式，工具只在 `PAGED_MEMORY`（包括旧模式兼容输入）可用。关闭 `memoryContextRotationEnabled` 可以停止应用轮换投影，历史和笔记不会被删除。

模型先用 `memory_note_update` 保存当前有效笔记，`next_steps` 至少一项；成功后读取回执中的笔记版本，以及 `Context rotation status` 中的 `epoch`，调用：

```json
{"expected_epoch": 0, "expected_note_revision": 1, "reason": "stage_complete"}
```

`reason` 支持 `stage_complete`、`budget_pressure`、`manual`，省略时为 `stage_complete`。工具可通过 `discover_tools` 发现，遵循 Worker 写权限、串行执行和禁止自动重试的策略。每份笔记只可用于一次新轮换；冲突后需刷新上下文，不能重复提交旧版本。阶段完成或预算紧张时考虑使用，不要求每轮调用。

轮换边界由服务端固定为发出本轮模型请求前的完整历史前缀，模型不能指定删除范围。本轮轮换调用及同批工具结果留在边界之后，按完整调用组保留。所有用户消息、系统信息、运行时控制和必需来源继续保留；调用未配对、ID 重复、笔记缺失或契约改变会拒绝轮换。较早的普通卡片不再自动加入，但主动回读得到的原文继续进入近期上下文。

原始历史使用 `event-log-v1` 追加日志，与每轮请求的动态状态快照分离。用户输入、持久控制、工具调用与结果拥有固定序号、事件 ID 和内容哈希链；日志记录与链头在同一 SQLite 事务提交。通过 `runtime_context_message` 创建的原生状态快照可更新、移动，所有新增快照类型遵循同一规则，不再维护名称排除清单。普通用户文本不能仅靠快照名称获得这种待遇。

轮换 Event 保存日志流、边界事件、前缀指纹、受保护消息位置、笔记来源和契约绑定，与对应执行者的 epoch 指针在同一事务提交。笔记及轮换提交时再次读取并验证日志，避免依赖过期的内存检查。主执行者可跨 run 恢复，Worker 沿用 run/node/assignment 绑定。检查点中的 `_historyEvent` 引用用于从已验证日志重建缺失的请求视图，不进入模型请求、不增加输入 token 估算，也不触发工具重执行。

旧边界（无版本、`stable-history-v1/v2/v3`）只有原始前缀通过原版本指纹校验后才会迁移。迁移证明独立保存，原笔记和原轮换事件不改写；以后状态快照刷新无需重封旧历史。真实事件改写、重复 ID、乱序、日志丢失或旧证明损坏会报告原因与事件位置，并在 AgentLoop 调用模型前停止；不会通过重新计算指纹接受损坏内容，也不会反复调用整理工具。契约变化导致的笔记过期仍沿用已有刷新或历史笔记恢复流程。

轮换生效后交接笔记为 required，不能被共享预算分页移除，内容仍是辅助记录，不能覆盖用户要求或替代验收证据。用户约束、笔记及工具 schema 放不下时，总预算门禁照常阻止请求，不会扩大模型窗口或任务额度。

`context_rotation` 指标包含 epoch、状态、边界与省略消息数/估算 token。状态包括 `ready`、`active`、`invalid_history`、`history_mismatch`、`note_unavailable`、`stale`、`invalid_source`；关闭时指标为 `disabled`。单个请求减少旧历史不代表整个任务必然节省 token，写笔记、轮换回执和再次回读都计入实际开销。

## 维护命令

在 Python 项目目录执行，原生配置增加 `--use-settings`：

```powershell
.\.venv\Scripts\python.exe -m minicode.paged_memory.admin --use-settings init-db
.\.venv\Scripts\python.exe -m minicode.paged_memory.admin --use-settings verify --tenant-id local --task-id <task>
.\.venv\Scripts\python.exe -m minicode.paged_memory.admin --use-settings gc-artifacts --tenant-id local
.\.venv\Scripts\python.exe -m minicode.paged_memory.admin --use-settings backfill --source <legacy-memory.json> --tenant-id local --task-id <task> --dry-run
```

`backfill` 仅用于显式导入旧 JSON 记忆；生成的 Markdown 和导出目录不可作为导入来源。`backfill-artifact-chunks` 可为历史文本附件补建缺少的原文分页卡片，不重写原始内容。

事件归档命令包括 `archive-events`、`archive-due`、`list-archives`、`verify-archive`、`read-archive` 和 `restore-archive`；归档保留原文、哈希及注册信息。

`set-retention` 设置保留期和 legal hold；`delete-task` 需要明确的授权、操作者及原因，清理本地文件与数据库记录并保存删除审计。`gc-artifacts` 默认预览，添加 `--apply` 才清理满足宽限期且无引用的文件。

## 备份与恢复

使用前文的 `backup`、`restore-drill` 和 `restore`。备份使用 SQLite 一致性快照，配套保存原始文件和会话恢复元数据，并校验哈希。恢复拒绝非空目标，恢复后使用 `configure-local` 选择新目录。运行中的 SQLite 不应只复制 `state.db` 作为备份。

`MEMORY_BACKUP_ENCRYPTION_KEY` 可启用加密备份；`MEMORY_LOCAL_SPOOL_KEY` 可启用有容量限制的加密缓冲。这些能力继续使用可选 `cryptography` 依赖。Token 计数继续使用可选 `tiktoken`。

## 验收与诊断

健康检查包含 SQLite 和本地附件目录；诊断字段使用 `authority_write_latency_ms`、`authority_availability_state`、`supplemental_memory_tokens` 和 `lexical_latency_ms`，已移除 PostgreSQL/Milvus 专用指标。

重点验证新库与已有 SQLite 库、事务回滚、并发写入、会话重启及分叉、跨租户隔离、原文分页、决策更新、归档与删除、备份恢复、Markdown 单向导出，以及 Web 设置和启动过程。
