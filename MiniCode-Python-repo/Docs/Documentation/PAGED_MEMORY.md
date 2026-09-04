# 分级上下文与窗口外无损记忆

本模块是可灰度启用的 Memory V2。默认关闭；原有 `MemoryManager`、会话历史和压缩器仍可运行。启用后，原始事实先持久化，再根据任务规模选择 `INLINE`、`WORKING_SET` 或 `PAGED_MEMORY` 传给模型。

## 两个不同的“完整性”定义

存储无损指：在适用保留期内，用户输入、模型输出、真实工具回执和大型 Artifact 的原文不会被摘要、压缩或 Milvus 索引替代。摘要和 Memory Card 都只是导航层。

召回完整性指：当前步骤声明为 required 的依赖必须通过结构化依赖闭包、当前状态、版本复核和来源指针校验全部加载。它不承诺每轮把全部历史塞进模型，也不宣称向量检索能绝对无遗漏。

关键验收指标：

- `required_dependency_coverage_ratio`：required 依赖中来源可解析的比例；高风险操作必须等于 `1.0`。
- `required_facet_coverage_ratio`：指定 facet 被最终证据集合覆盖的比例。
- `missing_dependency_ids`、`unresolved_source_ids`：非空时不能把“未召回”解释为“不存在”。
- `source_references`：最终 ContextPacket 中每张证据卡对应的 Event/Artifact/hash。
- 补充检索命中率、选择率、page fault 与 required coverage 分开统计。

## 改造前的实际问题

旧路径由 `memory.py`、`memory_pipeline.py`、`memory_injector.py` 和 `context_compactor.py` 组成，适合本地短任务，但有以下限制：

- Project memory JSON 有数量/体积上限，达到上限会淘汰旧项；它不是完整事件日志。
- 压缩器主要保存摘要和近期尾部，摘要可能丢失精确数字、约束和因果关系。
- 大工具结果可能被裁剪后重新注入，原始结果与上下文展示没有统一的来源指针。
- TF-IDF/重排召回没有步骤级 required dependency 闭包，召回不到与事实不存在无法区分。
- 当前状态、决策版本、Checkpoint、Artifact 与检索索引没有统一事务边界。

Memory V2 不删除旧数据；灰度期间可以只做 shadow write。正式启用上下文控制后，Agent Loop 不再实例化或写入旧 `MemoryManager`，避免一个任务同时产生两套相互漂移的记忆事实源。

默认 SQLite、Artifact、Runtime Worker Artifact、spool 与 backup 都位于用户级 `~/.mini-code/memory-v2/`，因此不同任务和工作区共用一套无损底座，并由 tenant/workspace/task/run 字段隔离。所有 Runtime V2 Worker 还会继承同一个 `_memoryAuthorityCwd` 和稳定的 `memoryTaskId`；如果显式配置相对路径，它们始终按协调器工作区解析，而不是按临时 Writer 副本解析。旧的工作区级 `.mini-code-memory-v2` 也被快照/复制/集成明确排除。服务部署应让全部任务连接同一个 PostgreSQL/MinIO 底座。

## 组件职责

| 组件 | 职责 | 是否权威 |
|---|---|---|
| PostgreSQL / 本地 SQLite 适配器 | Task、Run、Step、append-only Event、Task State、Decision、Dependency、Artifact/Archive Registry、Checkpoint、Memory Registry、Outbox、retention；并用 PostgreSQL FTS / SQLite FTS5 BM25 执行词法召回 | 是 |
| File / MinIO / S3 Artifact Store | 大型工具结果、原始大内容和不可变事件归档对象 | 是（原文载荷） |
| Milvus | 脱敏后的 Memory Card dense 向量索引、补充候选召回 | 否，可重建 |
| Working Set | 当前 phase 热记忆 ID、TTL、命中计数 | 否，可失效 |
| Context Builder | required 闭包、版本过滤、最小 facet 覆盖、token 打包 | 否 |

项目没有引入 pgvector。PostgreSQL 只做结构化/精确/词法检索，向量检索统一在 Milvus。

## 三种模式

`INLINE` 保持原始消息形态，不调用 Milvus，也不增加摘要模型调用。Memory V2 控制上下文时会旁路旧 Memory Injector、micro/cybernetic/auto compactor 和其恢复压缩路径，避免短任务被旧逻辑有损改写；shadow-write 模式仍保持旧主路径。所有事件仍先写权威库，因此之后升级不需要迁移临时历史。

`WORKING_SET` 注入当前 Task State、Step、required dependencies、近期高保真事件和 phase 热工作集；补充检索只走权威库词法查询。

`PAGED_MEMORY` 在 WORKING_SET 基础上并发使用权威库 FTS 和 Milvus dense 候选，用 RRF 融合，再回权威库复核 tenant/task/status/type/content hash 以及会影响过滤或排序的元数据。tenant/task/status 是硬过滤；run/phase 只做软加分，不再让旧 run 或旧 phase 变成不可见。Milvus 查询本身就是热路径健康探测，不在查询前后重复增加 `has_collection` 往返。最终只发送满足 token budget 的少量证据卡；需要精确细节时通过 Event/Artifact range API 回读原文。

升级即时发生。降级只在 phase boundary，并要求连续满足 `MEMORY_DOWNGRADE_STABLE_STEPS`，避免抖动。优先级从高到低包括跨会话/重启恢复、并行子任务、上下文占用、步骤数、审计要求、大 Artifact 和累计工具结果。

`WORKING_SET` / `PAGED_MEMORY` 会按需向模型动态注册 `memory_search`、`memory_get`、`artifact_read`、`checkpoint_restore`。这些工具的 tenant/task scope 来自服务端 Runtime，模型不能自行指定；range read 受 token 上限和 hash 校验约束，并返回不跳字节/字符/行/页的 `next_start`。`memory_get` 的坐标永远相对于 Memory 自己经过 hash 验证的 source range，`start=0` 表示该卡片来源的开头；`artifact_read` 才使用整个 Artifact 的绝对坐标。无法在预算内装下一整行或一整页时会要求改用 `BYTE`/`CHAR`，不会截断后伪造游标。`INLINE` 不注册这四个 schema，因此短任务没有额外工具定义开销。

Runtime V2 的跨 Worker 大输出同样只把摘要和 Artifact 引用放进依赖消息；下游通过作用域受限的 `runtime_artifact_read` 分页读取，不会在存储后又把完整 Artifact 自动塞回模型窗口。最终 provider 请求会再次按真实消息和动态注册后的完整工具 schema 估算硬输入预算；若当前原文、required 证据或可调用工具面放不下，请求在模型调用前明确阻断，不会静默删除 required 来源。

`memory_search`、`memory_get`、`artifact_read` 与 `checkpoint_restore` 的执行回执会保留在不可变事件日志中，供审计和重放使用，但不会再次投影成 MemoryRecord、进入 Milvus 或加入 working set。检索时也会过滤升级前遗留的同类投影，避免“检索结果再次被检索”的递归污染。超出单页上限的 `memory_get` 请求会自动收敛到安全页，并返回 `has_more` / `next_start`，而不是先失败再要求模型重试。

Task State 中所有 ACTIVE 硬约束和当前决策会自动合并成 required dependency；调用方即使漏掉 `add_dependency`，Context Build 和高风险 preflight 仍会加载并验证其 Event/Artifact 来源。State 注入只携带指针和状态，正文只在必载证据卡出现一次，避免重复占用 token。

## 面试讲解：为什么这样改

一句话概括：**PostgreSQL 保证可用和可解释，Milvus 提升语义召回；并发解决尾延迟，hash 复核解决派生索引假健康，相对坐标解决“命中正确但回源读错”的抽象泄漏。**

1. **为什么保留两路并发，而不是只用 Milvus？** 词法检索擅长错误码、表名、函数名和中文精确短语，dense 检索擅长同义表达，两者覆盖的失败模式不同。RRF 只融合名次，不强行比较 `ts_rank` 与 cosine 两种不同量纲。并发后热路径耗时从近似 `T_fts + T_vector` 变为 `max(T_fts, T_vector)`；Milvus 失败只丢失语义补充，已经完成的权威库结果立即可用。
2. **为什么不在 Milvus 查询前先做健康检查？** 健康探测与真实查询之间仍可能发生故障，而且额外增加网络往返；真实查询在 SDK deadline 内成功才是与当前请求最相关的健康证据。显式 `health_snapshot` 仍保留完整探测和全量对账。
3. **为什么 ID 对账不够？** 同一个 `memory_id` 可能仍指向旧 embedding。仅比较 ID 会得到 missing=0、orphan=0 的假绿色，但排序已经漂移。查询候选现在携带 `content_hash/status/type/facets/run/phase` 回 PostgreSQL 逐项复核；不一致的向量票被丢弃，词法票仍可使用，并将存储状态标为 `INDEX_DEGRADED`。运维扫描同时比较 `memory_id -> content_hash`，能发现同 ID 的旧向量。
4. **为什么 `memory_get` 用相对坐标？** Memory 是调用方操作的领域对象，Artifact byte offset 是存储实现细节。如果一个尾部 chunk 的绝对范围是 `[9000,12000)`，`memory_get(start=0)` 应读取这个 chunk 的第一个字节，而不是父文件第一个字节。原始对象级定位仍由 `artifact_read` 暴露，两个 API 各自只有一种坐标语义。
5. **为什么回源后还校验 hash？** 向量只决定候选，不是事实来源。最终证据必须能沿 `memory -> source event/artifact -> exact range -> SHA-256` 验证；这样即使索引延迟、重建错误或对象被篡改，也不会把未经验证的内容交给模型。

这套设计的代价是每个 PAGED_MEMORY 检索多一个受控工作线程、Milvus 多返回少量元数据、完整性扫描需要读取 ID/hash 映射；换来的是更低的组合延迟、明确的故障边界和可以向审计解释的证据链。它不声称证明模型答案为真，只证明“模型看到的证据来自哪、是否完整、派生索引是否与权威记录一致”。

## 写入顺序与故障边界

1. 用户输入和模型输出先写 append-only `agent_event`，同时写入带 Event/source range/hash 的可重建检索卡；跨会话恢复时近期原文会重新进入热上下文并与当前消息去重。
2. Tool Registry 的 raw-result observer 在旧的 50K 展示裁剪之前读取真实 `ToolResult`；大结果先写 Artifact Store，成功后才创建有界 Tool Result Envelope。
3. Event、Artifact Registry、Memory Registry、Task State patch 和 Outbox 在一个数据库事务内提交。大型 `text/*` Tool Artifact 在原文安全落盘后，额外生成带精确 BYTE range/hash 的有界 `ARTIFACT_CHUNK`，使尾部内容也能被 FTS 和 Milvus 召回。图片、视频等非文本 Artifact 只做权威存储和指针回读，当前不生成多模态检索向量。
4. Outbox Consumer 会原子租约一批记录，批量生成 embedding 并一次 upsert；过期租约可被恢复，多消费者不会重复抢占。实时写入不逐条调用 Milvus `flush`，只在 reindex 等明确维护边界封存一次，避免小 segment 和 flush 限流。幂等写入 Milvus 失败时只重试索引，不回滚原始 Event/Artifact。Task 真删除前会进入 `DELETING` 栅栏，消费者碰到删除竞态时会撤销可能的向量写入。
5. 步骤结束时 Event、Task State patch、带 hash 的 Checkpoint 和 Step 的 `COMPLETED/FAILED` 状态在同一个权威库事务提交；任一写入失败会整体回滚，不会出现“步骤已完成但检查点不存在”。

Artifact 上传成功但数据库事务失败可能留下孤儿对象，因此提供引用扫描、宽限期和默认 dry-run GC。无效 range 不改变 Registry；暂时性超时/存储不可用只进入 `ARTIFACT_DEGRADED`，不会误标对象丢失；只有内容 hash/尺寸校验失败才标记 `CORRUPT`。`MISSING` 由能够确认对象不存在的完整性扫描/运维流程设置，`MISSING`、`CORRUPT`、`TOMBSTONED` 都拒绝读取。

## 存储健康状态

| 状态 | 进入条件 | 允许行为 | 恢复条件 |
|---|---|---|---|
| `NORMAL` | 权威库、Artifact 和配置的索引均健康 | 正常读写和高风险操作 | 不适用 |
| `INDEX_DEGRADED` | Milvus 连接/查询失败、候选元数据或 hash 与权威库不一致、Outbox 写入失败 | Event/State/required 依赖、Artifact 和词法 fallback 可用；required coverage 完整时可继续 | Milvus 健康、ID/hash 对账一致且 Outbox 追平 |
| `ARTIFACT_DEGRADED` | 主 Artifact Store 写入/读取失败 | 小事件可落权威库；大结果只能进入加密有界 spool 并标记 `PENDING_UPLOAD` | spool 按 hash 上传、Registry 原子改为 ACTIVE、复核通过 |
| `AUTHORITY_UNAVAILABLE` | PostgreSQL/SQLite 无法读写或校验 | 本实现直接 fail closed，不退化为进程内真相，不推进 State/Decision/Checkpoint | 权威库恢复并通过健康与一致性检查后新建/恢复运行 |
| `RECOVERING` | 正在回放 spool 或修复派生索引 | 仅恢复、校验和安全读取；高风险操作拒绝 | 回放完成，版本/hash/引用检查通过 |

本实现没有为 PostgreSQL 写入提供离线 WAL。权威库中断时选择明确 fail closed；加密 spool 只用于已经真实执行而主 Artifact Store 暂时不可用的大型工具回执。

Artifact spool 使用 AES-GCM、容量上限和最小剩余磁盘空间保护；记录 tenant/task、`sequence_no`、`correlation_id`、`content_hash`、`idempotency_key`。恢复时按任务内序号回放，由幂等键派生稳定 Artifact ID/对象键；即使上传已成功但 Registry 状态提交超时，重试也只会覆盖同一对象。内容 hash 校验成功后才把 Registry 从 `PENDING_UPLOAD` 原子切换到 `ACTIVE`；若进程在提交后、删除 spool 前中断，下次恢复会在复核主对象后清理残留副本。无法安全写入 spool 时当前步骤暂停，而不是只留下摘要。

```powershell
python -m minicode.paged_memory.admin --cwd . replay-artifacts --tenant-id local --task-id TASK
```

该命令在顺序幂等回放后立即执行引用与 payload hash 完整性扫描；未全部恢复时返回非零状态。

## 数据模型与迁移

PostgreSQL 迁移位于 `minicode/paged_memory/sql/001_postgres.sql`。本地 SQLite 适配器维护等价表和事件 append-only trigger，当前 schema version 为 7；旧 SQLite 初始化时会在事务中重建 Memory source-identity 唯一索引并保留原行，唯一键包含 source byte range，不会把内容相同但位置不同的 Artifact chunk 错误合并。SQLite 使用事务同步的 FTS5 投影；PostgreSQL 持久化统一分词文档并建 `GIN(to_tsvector('simple', ...))` 索引。核心表：

- `agent_task`、`agent_run`、`agent_step`
- `agent_event`、`agent_task_state`
- `agent_decision`、`agent_dependency`
- `agent_artifact`、`agent_checkpoint`
- `agent_memory`、`agent_outbox`、`memory_working_set`
- `memory_retention_policy`、`memory_archive_manifest`、`memory_deletion_audit`

`agent_event` 只允许 INSERT；授权 hard delete 在受控事务中临时打开删除开关。Task State 使用 `version` 乐观锁；决策/约束集合使用事务内 merge patch，避免并发写入以陈旧快照覆盖彼此。Decision 写入会把旧 ACTIVE 版本设为 `SUPERSEDED`，普通召回只接受 `ACTIVE/OPEN`。

初始化：

```powershell
python -m minicode.paged_memory.admin --cwd . init-db
```

服务进程首次连接 PostgreSQL 时也会幂等执行迁移。迁移只创建不存在的对象和新 schema version，不删除旧表或旧 Memory JSON。

## Milvus Collection

默认 collection 为 `agent_memory_v2`，主键为字符串 `memory_id`。字段包含 tenant/workspace/user/task/run/step scope、memory type/status/phase、短 title/preview、source event/artifact/range、importance、token count、时间、content hash、facets 和 `dense_vector`。

- 默认 embedding 为本地 FastEmbed `BAAI/bge-small-zh-v1.5`（512 维）；原先的 feature-hash 只允许在测试中显式开启，不再冒充语义向量。
- collection 描述中固化 `index_id`，该 ID 由 collection/provider/model/revision/dimension/metric/preprocessing version 生成。任一项不同都拒绝复用旧 collection，避免混入不兼容向量。
- dimension 来自 `MILVUS_DENSE_DIM`/embedding provider，启动时校验，业务逻辑不写死。
- metric、index type、search params 均配置化，默认 `COSINE` + `AUTOINDEX`。
- 读一致性默认为 `MILVUS_CONSISTENCY_LEVEL=Strong`，新写入在不高频 `flush` 的前提下也能被紧随其后的检索/对账看见；对新鲜度要求低、更看重尾延的高吞吐部署，可显式改为 `Bounded`。
- 每次查询强制使用服务端 tenant/task scope 和 ACTIVE/OPEN 状态。
- Milvus 返回候选后必须回权威库复核 ID、content hash、status、memory type、facets、run 和 phase；任一字段不一致就取消该候选的向量 RRF 贡献。旧 Decision、TOMBSTONED、陈旧向量或越权记录不会直接进入 ContextPacket。
- title/preview 在 embedding 与写 Milvus 前做秘密模式脱敏；原始内容只留在权威存储。
- `memory_id` 是 upsert 主键，`content_hash` 和 Outbox idempotency 防止重复实体。
- Outbox 带不可变 `index_id`，Indexer 只消费目标索引的任务；`verify`/health 对账权威库与 Milvus 的 ACTIVE/OPEN `memory_id -> content_hash` 映射，同时报告 missing/orphan/hash mismatch。

初始化和消费 Outbox：

```powershell
python -m minicode.paged_memory.admin --cwd . init-index
python -m minicode.paged_memory.admin --cwd . process-outbox --batch-size 100
python -m minicode.paged_memory.admin --cwd . run-indexer --batch-size 100 --poll-seconds 1
# 只替换一个任务的派生向量，不影响共享 Collection 中的其他任务
python -m minicode.paged_memory.admin --cwd . reindex --tenant-id local --task-id TASK --rebuild
# 不带作用域才是整个共享 Collection 的全量重建
python -m minicode.paged_memory.admin --cwd . reindex --rebuild
```

`--task-id` 必须与 `--tenant-id` 同时提供。带 tenant/task 作用域的 `--rebuild` 只删除并回填该作用域；未指定作用域时才会 drop/recreate 整个可重建 Collection。全量重建属于维护操作，应先暂停独立 Indexer，避免与实时 Outbox 写入竞争。

升级前已存在的文本 Artifact 可先预览、再幂等回填分块；命令仅处理 `text/*`，图片、视频和其他二进制 Artifact 不会被读取或建索引：

```powershell
python -m minicode.paged_memory.admin --cwd . backfill-artifact-chunks --tenant-id local --task-id TASK
python -m minicode.paged_memory.admin --cwd . backfill-artifact-chunks --tenant-id local --task-id TASK --apply
python -m minicode.paged_memory.admin --cwd . process-outbox
```

从 v1 升级时使用新的 `agent_memory_v2` 做蓝绿重建：先 `init-index`，再对 v2 `reindex --rebuild`，用 `verify` 确认 missing/orphan/hash mismatch 均为 0 后才切换应用和 Indexer。旧 `agent_memory_v1` 不会被自动删除，回退期结束后再按受控流程处理。

## 本地 Docker Compose memory profile

Compose 顶层项目名固定为 `minicode`。PostgreSQL、MinIO、etcd、Milvus 以及以后确有需要的存储中间件都必须加入这一份 `docker-compose.yml`，因此 Docker Desktop 只显示一个 `minicode` 应用组；不得用独立 `docker run` 或另一 Compose 项目常驻。镜像均固定版本：PostgreSQL `16.4-alpine`、etcd `3.5.25`、MinIO `RELEASE.2024-12-18T13-15-44Z`、Milvus `2.6.22`。端口使用 55xxx/59xxx 范围，降低与已有服务冲突的概率；数据使用 named volumes，不随普通 `down` 删除。

`memory-indexer` 的默认 PostgreSQL URL 会从 `MEMORY_POSTGRES_USER` / `MEMORY_POSTGRES_PASSWORD` / `MEMORY_POSTGRES_DB` 同步组装；显式 `MEMORY_DATABASE_URL` 仍优先。`memory-minio-init` 也使用同一 `ARTIFACT_STORE_BUCKET`，避免自定义凭据或 bucket 后应用与中间件配置分叉。

```powershell
docker compose --profile memory up -d memory-postgres memory-minio memory-minio-init memory-etcd memory-milvus memory-indexer
docker compose --profile memory ps
```

容器内应用配置示例：

```dotenv
MEMORY_V2_ENABLED=false
MEMORY_SHADOW_WRITE=true
MEMORY_BACKEND=postgres
MEMORY_DATABASE_URL=postgresql://minicode:CHANGE_ME@memory-postgres:5432/minicode
ARTIFACT_STORE_TYPE=minio
ARTIFACT_STORE_ENDPOINT=http://memory-minio:9000
ARTIFACT_STORE_BUCKET=minicode-artifacts
ARTIFACT_STORE_ACCESS_KEY=CHANGE_ME
ARTIFACT_STORE_SECRET_KEY=CHANGE_ME
MILVUS_ENABLED=true
MILVUS_URI=http://memory-milvus:19530
```

在宿主机直接运行 Python 时，将主机名改为 `localhost`，端口分别为 PostgreSQL `55432`、MinIO `59000`、Milvus `59530`。详细默认值在 `.env.example`。

停止服务但保留数据：

```powershell
docker compose --profile memory down
```

不要使用 `down -v`，除非已明确授权删除持久数据并完成备份。

## 旧 Memory 回填和索引重建

默认从 `.mini-code-memory/memory.json` 读取，原文件不修改：

```powershell
python -m minicode.paged_memory.admin --cwd . backfill --dry-run
python -m minicode.paged_memory.admin --cwd . backfill --task-id legacy-project --continue-on-error
python -m minicode.paged_memory.admin --cwd . process-outbox
python -m minicode.paged_memory.admin --cwd . verify --tenant-id local --task-id legacy-project
```

命令支持 `--limit`、`--since`、`--until`、task scope、dry-run 和中断后幂等续跑。每条旧记忆先形成带 source file hash 的 Event，再形成 Memory Registry/Outbox；重复运行由 idempotency key 和 content hash 去重。

## 事件归档（Codex 式冷存储）

归档不是 Markdown 摘要，也不把正文塞进向量库。选中的 append-only Event 按确定性顺序写成 canonical JSONL，再压缩为不可变 `.jsonl.gz` 载荷进入当前配置的 Artifact Store；PostgreSQL/SQLite 只保存 `memory_archive_manifest`，记录 tenant/task scope、Event 边界和数量、压缩前后两个 SHA-256、对象 URI、codec、规则、backup class、验证与回迁时间。Milvus 不保存归档正文。

创建后会立即从 Artifact Store 回读，并校验对象 hash、解压后 canonical hash、JSONL header、tenant/task scope、Event 数量、首尾边界和每条 Event 的可解析性。归档 ID、Artifact ID 和创建 Event 均由内容 hash 稳定派生，重试不会生成重复对象。每段大小受 `MEMORY_ARCHIVE_MAX_EVENTS` 和 `MEMORY_ARCHIVE_MAX_UNCOMPRESSED_BYTES` 限制，超过上限明确失败，不能静默少存。

```powershell
# 所有写操作默认 dry-run，显式 --apply 才落盘
python -m minicode.paged_memory.admin --cwd . archive-events --tenant-id local --task-id TASK
python -m minicode.paged_memory.admin --cwd . archive-events --tenant-id local --task-id TASK --before 1788364800 --apply

# 执行 MEMORY_RETENTION_RULES_JSON 中 archive_after_seconds 已到期的规则
python -m minicode.paged_memory.admin --cwd . archive-due --tenant-id local --task-id TASK
python -m minicode.paged_memory.admin --cwd . archive-due --tenant-id local --task-id TASK --apply

python -m minicode.paged_memory.admin --cwd . list-archives --tenant-id local --task-id TASK
python -m minicode.paged_memory.admin --cwd . verify-archive --tenant-id local --task-id TASK --archive-id ARCHIVE_ID
python -m minicode.paged_memory.admin --cwd . read-archive --tenant-id local --task-id TASK --archive-id ARCHIVE_ID --offset 0 --limit 100
python -m minicode.paged_memory.admin --cwd . restore-archive --tenant-id local --task-id TASK --archive-id ARCHIVE_ID
python -m minicode.paged_memory.admin --cwd . restore-archive --tenant-id local --task-id TASK --archive-id ARCHIVE_ID --apply
```

当前安全语义是“先做可验证的冷副本，热库原 Event 保留”，所以创建归档本身删除数始终为 0；它不会绕过 append-only、required dependency 或 legal-delete 门禁。`restore-archive --apply` 会按原 ID、时间、类型、正文、JSON 和 hash 幂等补回缺失 Event，并明确报告是否需要重建派生投影。若未来要靠归档释放 PostgreSQL 空间，必须另行实现并验证窗口外读取 fallback、required 引用闭包和授权 source-prune，不能把“已复制”误当成“可安全删除”。

## 保留、legal hold、真实删除和孤儿 GC

默认保留期由 `MEMORY_RETENTION_DEFAULT_DAYS` 设置。摘要、阶段结束、Milvus 回填都不会触发原文删除。`TOMBSTONED` 只是逻辑失效，不等于满足真实删除请求。

```powershell
python -m minicode.paged_memory.admin --cwd . set-retention --tenant-id local --task-id TASK --days 365 --actor admin
python -m minicode.paged_memory.admin --cwd . set-retention --tenant-id local --task-id TASK --legal-hold --actor compliance
python -m minicode.paged_memory.admin --cwd . gc-artifacts --tenant-id local
python -m minicode.paged_memory.admin --cwd . gc-artifacts --tenant-id local --apply
python -m minicode.paged_memory.admin --cwd . delete-task --tenant-id local --task-id TASK --authorized --actor admin --reason "verified request"
python -m minicode.paged_memory.admin --cwd . deletion-audit --request-id DELETE_REQUEST_ID
```

真实删除要求显式授权、actor 和 reason；legal hold 优先级最高。删除传播到 Artifact（包括归档对象与已晋升对象的 spool 副本）、Archive Manifest、Milvus、Working Set 和关系库正文，只保留 request ID、执行状态、时间、计数、actor/reason/task 的不可逆 hash，不保留被删正文。如果配置了 Milvus 但删除时索引不可用，流程 fail closed，不会先删权威记录后遗留向量副本。Artifact GC 默认 dry-run，只删除超过宽限期、tenant scope 正确且 Event、Memory、Checkpoint、Task State、Artifact/Archive Registry 引用都不存在的对象。

`MEMORY_RETENTION_RULES_JSON` 是有序规则数组，可按 `tenant_ids`、`memory_types`、`event_types`、`artifact_types`、`sensitivity_levels`、`task_statuses` 匹配，并配置 `retention_seconds`、`archive_after_seconds`、`legal_hold`、`deletion_mode`、`backup_class`。legal hold 始终覆盖到期删除。

## 备份、恢复、RPO/RTO

本地 SQLite + File Artifact 路径提供一致性备份、逐文件 hash、SQLite integrity check、Artifact content hash 校验和 restore drill：

```powershell
python -m minicode.paged_memory.admin --cwd . backup
python -m minicode.paged_memory.admin --cwd . restore-drill ~/.mini-code/memory-v2/backups/FILE.tar.gz
```

设置 `MEMORY_BACKUP_ENCRYPTION_KEY` 后使用 AES-GCM 认证加密，格式为 `.tar.gz.enc`。生产默认目标为 RPO 24 小时、RTO 4 小时，可配置；restore drill 输出实际耗时和恢复后的 Event/State/Artifact 数量。

PostgreSQL/MinIO 生产部署必须使用平台原生能力：PostgreSQL `pg_dump` 加 WAL/PITR，MinIO bucket versioning/replication，并把两者放入同一恢复点和密钥管理策略。管理命令会拒绝把 SQLite-only 导出伪装成 PostgreSQL/远程对象存储灾备。Milvus 不做权威备份，恢复后从 `agent_memory` 幂等 `reindex --rebuild`。

本次在本地 SQLite/File 路径实际执行的 drill：backup verify `valid=true`、`file_count=2`、`artifact_count=1`；restore `restore_drill_success=true`、Event `3`、State `1`、Artifact `1`，耗时 `0.056s`，低于 4 小时 RTO 目标。该次临时测试未设置加密 key，因此验证报告为 `encrypted=false`；这不代表生产备份允许明文。

## 可观测性

`PagedMemoryRuntime.metrics_snapshot()` 返回不含正文、Prompt、密钥的结构化快照，`emit_metrics(callable_or_sink)` 可桥接现有日志、Prometheus 或 OpenTelemetry。快照包含模式原因、分区 token、候选/选择/去重数、required dependency/facet coverage、page fault、Milvus/Artifact/Context/权威写延迟、Outbox lag/retry、工具原始与提示大小、来源解析失败、存储可用性和 spool 字节数。GC、真实删除、备份验证和 restore drill 分别返回孤儿候选数、传播延迟、备份年龄和恢复结果；未执行的生命周期指标明确为 `null`，不会伪造成功值。

## 灰度与回滚

推荐顺序：

1. 保持 `MEMORY_V2_ENABLED=false`、设置 `MEMORY_SHADOW_WRITE=true`，只记录与构造候选包。
2. 验证事件数、Artifact hash、required coverage、跨租户隔离、Outbox lag 和 token 指标。
3. 对少量 task 使用 `MEMORY_MODE_OVERRIDE`，再打开自动模式。
4. 正确性无回退后设置 `MEMORY_V2_ENABLED=true`。

快速回滚只需把两个 flag 都设为 `false`。权威数据保留，不删除表、不删除 volumes；旧 Agent 消息路径立即恢复。索引可随时删除并从 Registry 重建。

## 测试和离线评测

```powershell
pytest -q tests/test_paged_memory_archive.py tests/test_paged_memory_policy.py tests/test_paged_memory_store.py tests/test_paged_memory_runtime.py tests/test_paged_memory_lifecycle.py tests/test_paged_memory_eval.py
python benchmarks/paged_memory_eval.py
```

启动 memory profile 并安装 `memory` extra 后，执行真实 PostgreSQL + MinIO + Milvus 往返测试：

```powershell
$env:MINICODE_MEMORY_EXTERNAL_INTEGRATION="1"
pytest -q tests/test_paged_memory_external_integration.py
```

该用例使用每次随机 task ID，验证事务写入、MinIO 原文与 range read、Outbox→Milvus、task scope 隔离、required preflight、Checkpoint 重启恢复、Event 冷归档的 MinIO 写入/双 hash 回读和授权清理；未显式设置开关时会跳过，不会用本地 double 冒充外部服务。

本机隔离 `.venv` 的最终全量回归为 `1653 passed, 3 skipped, 2 warnings`；其中 2 项因未设置 `ANTHROPIC_API_KEY` 跳过仓库原有 Live API 用例，1 项是全量运行时未开启的外部存储联调。两项 warning 来自测试依赖的 Starlette/httpx/anyio 弃用提示。Memory V2 专项为 `104 passed, 1 skipped`；显式开启外部联调后另有 `1 passed`。

本次确定性离线评测（不调用模型）的实测结果：

| 场景 | 全历史输入 token | 实际模型输入 token | 降幅 |
|---|---:|---:|---:|
| INLINE 短任务 | 164 | 164 | 0%（保持原样） |
| WORKING_SET 中任务 | 2,147 | 1,542 | 28.18% |
| PAGED_MEMORY 长任务 | 19,499 | 1,475 | 92.44% |

required dependency、required facet 和硬约束召回门禁均为 100%；ACTIVE 决策/约束还会作为隐式 required 项进行来源复核。这些是生产 estimator 对真实序列化输入的结果，不是缓存费用推算；因为没有外部模型凭据，不能据此宣称模型任务成功率或答案质量提升。

2026-09-04 已在本机 Docker Desktop 的单一 `minicode` Compose 应用组完成真实联调：PostgreSQL、MinIO、etcd、Milvus 四个常驻服务均为 `healthy`，`memory-minio-init` 以退出码 0 建桶；外部用例结果为 `1 passed`。联调使用 PyMilvus `MilvusClient` API，并真实验证了 FastEmbed 512 维写入、Strong 读一致性、PostgreSQL FTS、MinIO payload/hash/range read、Outbox 批处理和授权清理。
