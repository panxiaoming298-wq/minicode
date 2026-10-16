# Web 会话分叉与工作树

在已完成轮次末尾点击“从此处分叉”，选择执行环境后创建。子会话保留到该轮的对话和完整上下文，打开后等待新的输入。代码使用**创建时的当前工作区状态**，包括未提交修改；它不是所选轮次当时的文件版本。

Git 项目默认创建独立 Worktree，也可以选择 Local 共享原目录。非 Git 项目和独立对话只提供 Local。新项目任务的输入区也可以选择 Local / Worktree。

## 历史保存在哪里

会话元数据、当前运行消息以及 JSON / 增量保存 / session bundle 继续使用现有 `~/.mini-code/sessions` 存储。完成轮次的展示记录与可压缩的模型消息独立保存。

当现有 `memoryBackend=postgres`（或 `MEMORY_BACKEND=postgres`）时，历史复用同一个数据库、同一组存储适配器，**没有新增历史表**：

| 现有表 | 会话历史用途 |
| --- | --- |
| `agent_event` | `SESSION_TURN_STATUS` 追加事件：稳定 turnId、顺序、运行状态、分叉可用状态 |
| `agent_checkpoint` | `snapshot.checkpoint_type=SESSION_TURN` 的不可变轮次清单：模型消息、展示消息、输入历史和压缩 / 工作记忆状态的引用 |
| `agent_artifact` | `SESSION_HISTORY_OBJECT` 对象登记；正文通过现有 Artifact Store 保存 |
| `agent_memory` | 继续存放可检索的项目 / 任务记忆，不充当完整会话的权威历史 |

正文沿用 `artifactStoreType` / `ARTIFACT_STORE_TYPE`：使用 MinIO / S3 时进入现有对象存储，使用 file 时进入 `artifactStoreRoot`，默认 `~/.mini-code/memory-v2/artifacts`。PostgreSQL 存元数据与引用。不会新增数据库、中间件或复制运行时数据库。

`004_session_history.sql` 只增加部分索引和 schema version 12。会话历史直接调用现有 authority，不依赖项目记忆的 `enabled` / `shadow_write` 开关，也不创建检索索引任务。任务恢复只选取 `TASK_STATE` 类型的 checkpoint；旧 checkpoint 没有类型字段时仍按任务状态处理。

未使用 PostgreSQL 的本地安装保持文件方式：轮次清单位于 `sessions/<sessionId>/history`，去重正文位于 `sessions/history_objects/*.json.gz`。`sessions/.history-backend.json` 固定存储后端及命名空间，PostgreSQL 连接或对象存储故障会报错，不会偷偷写入另一套历史。已有历史的数据库、租户或对象存储位置需要显式迁移后才能切换。

## 对话增多是否占更多空间

会增加。每条排队输入都是独立轮次，轮次状态和清单随轮次数增长，但不是每轮复制一份完整对话：

- 消息和上下文按内容哈希去重、gzip 压缩；相同正文共用对象。
- 消息序列采用每块 32 项的持久引用，未变化的前缀继续复用。
- 上下文压缩产生新的序列引用，之前完成轮次仍可展示和分叉；不受当前 300 条消息展示限制影响。
- 分叉复制所选历史的清单和引用，正文仍去重。父子会话各自拥有历史记录，共用正文归属于安装命名空间，删除父会话不会破坏子会话。
- 永久删除会话时回收不再被任何保留轮次引用的历史正文。远端物理删除失败留下的孤立对象由已有 Artifact GC 后续处理。

历史 task 使用独立 `session_*` 命名空间，与项目记忆的任务保留策略分离。会话删除不删除项目记忆。归档保留历史与工作树。

子会话可通过只读的 `session_history_read` 工具分页读取自己保存的历史和工具结果原文。父任务的大结果 Artifact 标识仍受任务权限限制，不被当作跨任务授权；历史原文不依赖父会话文件，也不需要重放历史工具调用。

## 工作树与应用成果

工作树放在 `~/.mini-code/worktrees/<worktreeId>/checkout`，以 detached HEAD 创建。服务从实际 Git 仓库根创建，并保留项目相对仓库根的偏移。因此在 MiniCode 外层仓库中选择 Python 或 Web 子目录也能正确运行。

创建时复制已跟踪文件的当前内容、删除状态和未忽略的新文件，源目录与暂存区保持原样。未跟踪的依赖、构建缓存、数据库文件、模块 `Data/Test` 产物不复制。分叉窗口可显式指定额外需要的 Git 忽略文件；右侧变更面板可展开查看实际白名单。首版不支持子模块、链接或特殊文件快照。

`projectId` / `projectPath` 识别原项目，`workspace` 始终是实际执行目录。侧栏按原项目归组；文件浏览、模型工具、权限、Standard / Deep / Ultra 的成果集成都绑定实际执行目录。Worker 自身的临时写隔离继续使用，最终只集成到该会话的工作树。子会话重新建立系统提示、工具、权限、运行与计费状态，不继承可执行的历史文件回退点。

在右侧“变更”选择“应用回原项目 · 预览”，检查文件和差异后确认应用：

1. 比较工作树与创建时或上次成功应用时的基线，包含文本、新增、删除与二进制文件。二进制文件展示状态和大小。
2. 应用时重新核对预览中的源 / 目标内容、Git HEAD 和基线代数。目标等于基线时更新，已等于结果时跳过，其余情况整次停止。
3. 使用临时文件、内容备份与 `integration.json` 日志。正常失败恢复先前内容；中断后在下一次预览、应用或清理时恢复。若恢复期间目标又被外部修改，停止恢复并保留备份。
4. 成功后推进基线，保留未提交改动，不变更目标暂存区。同一预览重复应用不会重复写入。项目目录之外有成果变更时，整次应用被阻止。

源快照、应用和清理与相关目录的 MiniCode 写任务互斥；Git 元数据操作按共同仓库加锁。不同工作树内的写任务仍可并行。外部编辑器不遵守 MiniCode 锁，预览校验可以发现校验前的漂移；它不是对所有外部程序的操作系统级写禁令。

归档只保留会话。永久删除时，未应用成果需要明确选择保留目录或丢弃工作树；其他会话仍在使用的工作树会保留。目录缺失后历史仍在，界面显示不可运行，不回退到原项目。运行中的工作树不能删除。

## Web API

| 接口 | 请求 / 行为 |
| --- | --- |
| `GET /api/sessions/{id}/turns` | `before` / `limit` 分页，返回轮次和不可分叉原因 |
| `POST /api/sessions/{id}/fork` | `turnId`、`mode`、`requestId`、可选 `includeIgnored`；返回后台操作 |
| `POST /api/sessions` | 支持 `mode`、`requestId`、`includeIgnored`；带 requestId 时返回幂等后台操作，旧 Local 请求仍同步返回 session |
| `GET /api/worktree-operations/{id}` | `preparing` / `ready` / `failed` 与具体结果 |
| `GET /api/worktrees/{id}/changes` | 相对应用基线的文件差异 |
| `POST /api/worktrees/{id}/apply-preview` | 固化具体预览并返回 previewId |
| `POST /api/worktrees/{id}/apply` | `previewId`、`requestId`，返回后台操作 |
| `DELETE /api/worktrees/{id}` | `requestId`，可选显式 `discard=true` |
| `DELETE /api/sessions/{id}` | 归档后删除；可选 `worktree=keep` / `discard`，默认只清理无新成果的工作树 |

服务端从持久记录读取源与目标路径，应用接口不接受浏览器指定目标目录。同一 requestId 配不同请求内容会拒绝；终态失败重试使用新 requestId，网络断线重试保留原 requestId。

首版不包含 CLI 分叉交互、历史文件状态回滚、三方合并、Local / Worktree 迁移或已丢失目录的自动重建。旧会话只认证消息与输入历史吻合、工具调用配对完整且轮次边界清楚的历史，不用 `memory_continuity` 诊断信息补造快照。

## 验证

在后端执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_session_worktrees.py tests/test_session_history_postgres.py tests/test_web_api.py tests/test_paged_memory_runtime.py -q --basetemp Main/MinicodeFrontline/Data/Test/fork-validation
```

PostgreSQL 集成测试使用现有 Compose 服务，在独立随机 schema 中执行并清理该 schema，Artifact 内容位于测试目录。可通过 `MINICODE_TEST_POSTGRES_DSN` 指定测试数据库；不操作运行时卷。

在前端执行：

```powershell
npm test
npm run build
npm run test:worktree
```

浏览器验收自动启动真实 FastAPI 和 Vite，使用 `Main/MinicodeFrontline/Data/Test/WorktreeBrowser-*` 下的临时用户目录和中文 / 空格路径 Git 仓库，覆盖桌面与移动视口。它不调用付费模型；运行隔离测试使用确定的模型响应驱动真实 AgentLoop、文件工具和成果集成。
