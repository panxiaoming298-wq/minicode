# MiniCode Python 后端

本目录提供 MiniCode 的 Python Agent 运行时、CLI/TUI、Headless、本机 Web API 与存储管理命令。项目定位、核心设计、整体架构和前后端完整上手步骤统一维护在[根目录 README](../README.md)。本文件保留后端安装、命令、工具行为和诊断参考，同时作为 Python 包的说明文件。

[项目总览](../README.md) · [完整安装步骤](../README.md#快速开始) · [Web 工作台](../MiniCode-Web/README.md) · [源码导航](#源码导航)

## 后端安装与入口

以下命令在本目录执行，需要 Python 3.11+。文件搜索需要支持 PCRE2 的 ripgrep，Git 操作和 Worktree 需要 Git；只运行终端与 Headless 时无需构建前端。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[memory]"
.\.venv\Scripts\python.exe -m minicode.install
.\.venv\Scripts\python.exe -m minicode.main --validate-config
.\.venv\Scripts\python.exe -m minicode.main --profile deep
```

macOS / Linux 将解释器路径换为 `.venv/bin/python`。Web 所需依赖使用 `.[web,memory]`，完整启动与数据初始化见[根目录快速开始](../README.md#快速开始)。

下文的 `minicode-*` 命令默认已激活虚拟环境：Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1`，macOS / Linux 使用 `source .venv/bin/activate`。也可以始终调用虚拟环境解释器和对应的 `python -m` 模块。

| 命令 | 对应模块 | 用途 |
| --- | --- | --- |
| `minicode-py` | `minicode.main` | 交互式终端、会话查看与恢复。 |
| `minicode-headless` | `minicode.headless` | 非交互式单次任务。 |
| `minicode-web` | `minicode.web_api` | 本机浏览器工作台 API 与前端托管。 |
| `minicode-memory` | `minicode.paged_memory.admin` | 本地存储配置、导出、备份与恢复。 |
| `minicode-readiness` | `minicode.readiness` | 运行时与供应商就绪诊断。 |
| `minicode-structure-check` | `minicode.structure_check` | 工程结构检查。 |
| `minicode-provider-smoke` | `minicode.provider_smoke` | 供应商配置与接口冒烟检查。 |

CLI / Headless 以当前目录为工作区。处理其他项目时，先切换到目标目录，再调用已安装命令或 MiniCode 虚拟环境的解释器。执行模式通过 `--profile standard|deep|ultra`、`executionProfile` 或 `MINI_CODE_EXECUTION_PROFILE` 选择，入口默认值见[执行模式与工作目录](../README.md#执行模式与工作目录)。

### 模型与数据配置

安装向导用于选择 Provider、模型、Base URL 和 API Key。也可以通过启动进程的环境变量配置，例如：

```powershell
$env:MINI_CODE_PROVIDER = "deepseek"
$env:MINI_CODE_MODEL = "你的模型 ID"
$env:DEEPSEEK_API_KEY = "你的 API Key"
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
minicode-py --validate-config
```

项目支持的 DeepSeek 配置还包括 `DEEPSEEK_THINKING_MODE=enabled|disabled`、`DEEPSEEK_REASONING_EFFORT=low|high|max` 和 `DEEPSEEK_MODEL_FALLBACKS`。实际模型 ID 和参数支持情况以使用的供应商接口为准。

阿里云百炼通过兼容接口接入时，使用 `MINI_CODE_PROVIDER=bailian`、`DASHSCOPE_API_KEY` 和 `DASHSCOPE_BASE_URL`，模型名称通过 `MINI_CODE_MODEL` 设置。配置项示例见 [.env.example](./.env.example)；原生 Python / Web 不会自动加载该文件，Docker Compose 才会读取 `.env`。

模型与用户设置默认保存在 `~/.mini-code/settings.json`，已调整状态目录时使用对应位置。数据根通过 `memoryDataRoot` 或 `MINI_CODE_DATA_ROOT` 配置，SQLite 保存结构化状态，本地文件保存大型原文和附件。未显式指定数据根的旧安装沿用用户状态目录下的兼容位置。`configure-local` 初始化目标目录但不迁移旧数据；Markdown 导出是查看副本，恢复应使用备份。目录、初始化、导出和恢复步骤统一见[分页记忆与存储说明](./Docs/Documentation/PAGED_MEMORY.md)。

## 会话与恢复

| 终端内命令 | 作用 |
| --- | --- |
| `/session` | 查看当前会话快照。 |
| `/sessions` | 列出当前工作区的已保存会话。 |
| `/session-replay` | 回放会话及运行上下文。 |
| `/memory` | 查看当前工作区的记忆状态。 |
| `/checkpoints` | 查看文件编辑检查点。 |
| `/rewind-preview` | 预览回退会影响的文件编辑。 |
| `/rewind` | 按检查点、步数或编辑组执行文件回退。 |
| `/readiness` | 查看运行时、供应商与 fallback 诊断。 |

也可在启动命令中直接查看或恢复已保存会话：

```powershell
minicode-py --inspect-session latest
minicode-py --replay-session latest
minicode-py --preview-rewind latest
minicode-py --resume latest
```

可将 `latest` 换为具体会话 ID。会话分叉、Worktree 应用、冲突与恢复见[会话分叉与工作树](./Docs/Documentation/SESSION_FORK_WORKTREE.md)。

## Headless 与验收契约

```powershell
minicode-headless --profile deep "解释这个项目的结构和主要入口"
```

新版运行时可以按次选择。下面的命令只预览配置，不调用模型、工具或记忆服务：

```powershell
minicode-headless --preflight --runtime primary --profile deep --max-tokens 64000 --deadline-seconds 240
```

需要明确验收时，可再传 `--task-file <已审查的本地 JSON>`。实际运行会执行该文件的验收命令，但不会因此授予 Agent 工具权限；预期产物也不是写入白名单。未配置新版运行时的终端入口默认沿用 legacy。

契约使用 `minicode-headless-task-v1` 格式。例如，要求任务生成合法的 `report.json`，可以使用下面的验收内容：

```json
{
  "schemaVersion": "minicode-headless-task-v1",
  "taskRequiresWorkspaceWrite": true,
  "acceptanceCriteria": ["report.json 是合法的 JSON 文件"],
  "expectedOutputs": ["report.json"],
  "runtimeVerificationCommands": [
    {
      "name": "validate-json",
      "kind": "invariant",
      "command": ["python", "-m", "json.tool", "report.json"],
      "timeout": 30,
      "covers": [1]
    }
  ]
}
```

将它保存为自己审查过的本地 JSON 文件，通过 `--task-file` 指定，并让任务提示词说明实际需要生成的内容。`command` 是参数数组，`covers` 对应从 1 开始的验收条件编号；检查执行环境必须能找到其中的解释器。先用 `--preflight` 检查配置，实际运行时移除该参数。契约不应包含密钥，验收命令按 Runtime 的检查机制执行，需在运行前核对。

验收检查默认直接使用当前候选或集成工作区，不再为每次检查复制整个项目。原有候选工作区仍按任务的写入策略管理。测试临时数据、缓存、覆盖率文件与浏览器报告进入该工作区的 `.temp/verification/check-*`，不自动删除或回滚已有文件。Windows 浏览器的临时用户目录另用系统临时目录中的短路径，执行结束后清理，以兼容浏览器的路径限制；截图和报告仍保留在检查产物目录中。自定义检查可用环境变量 `MINICODE_VERIFICATION_OUTPUT_DIR` 保存报告，或用 `MINICODE_VERIFICATION_TEMP_DIR` 保存临时数据；任意脚本写到其他位置的输出不会被自动重定向。

需要隔离时，在运行配置或 Headless 验收 JSON 中显式设置 `"runtimeVerificationIsolation": true`；也可在 `runtimeVerificationCommands` 的单条命令中设置 `"isolate": true`。单条设置优先，`false` 表示直接执行，未设置时默认直接执行。检查仍执行真实命令、遵守原有命令权限、取消和超时限制，不会自动重试。

检查前后源码、配置、锁文件和已有测试数据的版本必须一致，否则检查失败并保留实际文件变化，需要修复后重新验收。npm 项目根目录的 `node_modules/`、`dist/` 和根级 `*.tsbuildinfo` 属于生成的依赖和产物，不计入源码版本，也不写入候选源码补丁；`src/dist/` 等源码路径及 `Data/` 不在此排除范围。npm 构建产物保留在当前工作区，复用浏览器检查结果时会重新验证 `dist/` 的内容，产物变化后重新执行检查。

## 工具目录与搜索

### 按需加载与执行回执

默认 `toolProfile=full`，内置的 JSON/CSV、编解码、正则、摘要、时间戳、文本、压缩和 HTTP 工具
都进入可用目录。legacy 与 primary AgentLoop 均通过 `discover_tools` 按需展示完整参数定义：
核心工具保持可见，其余工具先浏览或搜索，再按准确名称选择，每次最多选择 8 个。
选择只改变后续模型请求的工具展示，不执行工具，也不授予文件、网络或 Worker 权限。

需要精简目录时，可显式设置 `MINI_CODE_TOOL_PROFILE=core`，或在设置文件中设置
`"toolProfile": "core"`；环境变量优先。变更目录配置后重新启动当前运行进程。

通用工具会检查必填字段、字段类型和取值，拒绝未知参数。随机字符串长度必须为整数，
ISO 时间转时间戳必须带时区；HTTP 请求超时范围为 `(0, 120]` 秒，请求头的值必须为字符串。
CSV 表头重复或数据行多出/缺少列时会报错，避免转换时静默丢失数据。

HTTP 正文最多读取 1 MB、展示 10,000 字符，超过任一边界都会显示 `BODY_TRUNCATED: yes`。
通用长输出也会明确标注截断；`outputTruncated` 与工具执行是否成功分别记录，成功响应不代表展示了完整正文。

失败不会自动启动重试。模型在同一 AgentLoop 轮内重复发出相同工具和参数的临时失败调用时，
仅 `safe` 重放策略允许最多两次重试；其他策略会返回 `NotExecuted`，要求先核对结果或走工具支持的幂等流程。
该计数限于当前轮，不提供跨进程或外部服务的去重保证。
压缩/解压写入中途失败、写请求结果不确定或工具超时，会阻止同批次后续写入并保留每个调用的回执；
已有部分输出会保留供检查，不会宣称已回滚或已完成。

### 搜索与代码导航

每台机器安装一次 ripgrep，Docker 镜像已经包含该依赖：

| 平台 | 安装命令 |
| --- | --- |
| Windows | `winget install --id BurntSushi.ripgrep.MSVC --exact` |
| macOS | `brew install ripgrep` |
| Debian/Ubuntu | `sudo apt-get install ripgrep` |

安装后重新打开终端，通过 `rg --version` 和 `rg --pcre2-version` 检查。若程序不在 `PATH` 中，可通过 `MINI_CODE_RIPGREP_PATH` 指定可执行文件的完整路径。

模型仍调用原有的`grep_files`，参数保持不变。底层流式读取 `rg --json`，
保留工作区路径检查、原有 include/exclude 匹配规则，以及 `路径:行号:内容`
输出和最多 5 行上下文。隐藏路径、构建目录及常见二进制扩展名继续排除；
为保持原有搜索范围，不自动应用 `.gitignore`、`.ignore`、`.rgignore` 和用户个人的
ripgrep 配置。重叠上下文合并显示，仅返回能严格解码为 UTF-8 的结果行。

单次搜索最多运行 10 秒，最多返回 200 个匹配行、50,000 个输出字符，超大 JSON
事件也有读取上限。达到结果上限会明确标注截断；缺少程序、超时、取消、引擎错误均返回
失败回执，不伪装成“没有匹配”。正则使用 `auto` 引擎，前后查找和反向引用需要
PCRE2 支持；不保证兼容 Python `re` 的全部扩展，无法处理的表达式会返回引擎错误，
不会静默退回 Python 逐文件搜索。

`find_references` 与 `grep_files` 共用结构化搜索后端、路径过滤和资源限制。
它接受一个 Python 标识符，按大小写敏感的完整标识符搜索 `.py` 文件，附带前后各 2 行
上下文。结果明确标为**候选引用**：定义、注释、字符串以及不同作用域中的同名项都可能
命中，重命名前需要进一步核实。两个搜索工具的结果路径均相对于搜索目录，显式文件则
相对于该文件的父目录。`find_symbols`、`get_ast_info` 继续使用 Python AST 分析；
普通文本搜索优先使用 `grep_files`，命令工作流使用 `run_command`。

## 扩展

MiniCode 使用统一的 `minicode.dev/v1` 扩展包。一个包可以贡献 Skill、MCP、OpenAI-compatible Provider、外部 Tool、Hook、斜杠 Command、Agent Profile 和声明式 UI 资源。支持项目级与全局级作用域，以及目录、ZIP、Git URL 或普通 Skill 目录导入。

```text
/extension-install ./my-extension --scope=project
/extension-inspect project:my-extension
/extension-trust project:my-extension
/extension-health project:my-extension
```

安装默认不等于信任；可执行贡献只有在权限被显式授予后才会激活。托管包有内容完整性锁、更新备份、回滚和可恢复卸载。完整清单格式、权限模型、Tool JSON 协议和管理 API 见 [MiniCode 本地扩展平台](./Docs/Documentation/superpowers/reports/2026-06-05-minicode-local-extension-workflow.md)。

## Docker 运行

在本后端目录首次创建 `.env`，已有文件时直接编辑现有配置：

```powershell
# 仅首次创建时执行
Copy-Item .env.example .env
```

填入模型凭据，并核对工作区和数据路径，然后启动所需入口：

```powershell
docker compose build
docker compose run --rm cli
docker compose run --rm headless "解释这个项目的结构"
```

Compose 项目名固定为 `minicode`，定义 `cli` 和 `headless` 两种入口。从外层工作区执行时，增加 `-f ./MiniCode-Python-repo/docker-compose.yml`。`MINI_CODE_WORKSPACE` 指定挂载到 `/workspace` 的工作区，未设置时使用本后端目录。

宿主机 `MINI_CODE_DATA_ROOT`（未设置时为 `D:/MiniCodeData`）挂载到容器的 `/data/minicode`，其他平台需在 `.env` 中改为实际绝对路径。用户配置使用具名卷 `minicode-home`。当前配置无需远程数据库、对象存储或向量索引服务；中间件管理遵守[工作区约定](../AGENTS.md)，保留已有运行时卷。

## 检查与诊断

后端测试与浏览器检查所需依赖：

```powershell
python -m pip install -e ".[dev,web,memory]"
python -m playwright install chromium
python -m pytest -q --import-mode=importlib
```

默认 pytest 收集 `tests/` 和 `Package/AgentRuntime/Test`。其他结构模块的镜像测试与完整检查链见 [.github/workflows/ci.yml](./.github/workflows/ci.yml)，测试通过情况以当前环境的实际执行输出为准。

运行时诊断与工程结构报告可以单独执行；下面的报告写入对应模块的 `Data/`：

```powershell
minicode-readiness --json --fail-on blocked
minicode-readiness --bundle-out Main/MinicodeFrontline/Data/Checks/readiness-bundle --fail-on blocked
python -m minicode.release_readiness --check-readiness-bundle Main/MinicodeFrontline/Data/Checks/readiness-bundle
minicode-structure-check --root . --hotspots 5 --max-dependency-upstream 4 --check-material-inventory --report Package/EngineeringStructure/Data/Checks/structure-compliance.json
python -m minicode.release_readiness --check-structure-compliance-artifact Package/EngineeringStructure/Data/Checks/structure-compliance.json
```

`--fail-on blocked` 报告供应商 warning，但仅在运行时 blocked 时失败；要求供应商和 fallback 全部就绪时，可使用 `--fail-on warning`。真实模型能否调用仍取决于本机凭据和网络通道。

| readiness 导出选项 | 内容与用途 |
| --- | --- |
| `--examples-out` | 只读的 fallback 配置建议，不写入凭据或修改设置。 |
| `--doctor-out` | 供人工查看的运行时与供应商诊断。 |
| `--repair-plan-out` | 脱敏 JSON 修复计划。 |
| `--patch-preview-out` | 脱敏 settings merge patch 预览，由人工审查和合并。 |
| `--bundle-out` | 上述诊断、离线 fallback 模拟与文件 manifest 的完整检查包。 |

`minicode.release_readiness` 提供 manifest、脱敏、patch preview、离线模拟与发布报告校验。Manifest 记录文件存在性、大小和 SHA-256，用于发现缺失或漂移。离线模拟不调用真实供应商；供应商 live-smoke 的失败仍需保留诊断证据。更多选项通过 `python -m minicode.readiness --help`、`python -m minicode.release_readiness --help` 查看，发布检查还可参考 `benchmarks/release_readiness.py`。

## 源码导航

| 模块 | 作用 |
| --- | --- |
| `minicode/agent_loop.py` | 主 model/tool loop、runtime event flow 和产品集成。 |
| `minicode/turn_kernel.py` | step policy、phase transition、widening 和 verification gate。 |
| `minicode/session.py` | durable session、inspect/replay 视图、checkpoint 和 rewind helper。 |
| `minicode/cli_commands.py` | `/session`、`/replay`、`/rewind`、`/readiness` 这类本地产品命令。 |
| `minicode/memory.py` | 长期项目记忆管理和检索入口。 |
| `minicode/working_memory.py` | 在 compaction 压力下仍会保留的 working memory 条目。 |
| `minicode/memory_pipeline.py` | memory retrieval、injection、reflection writeback 和优化闭环。 |
| `minicode/paged_memory/` | INLINE/PAGED_MEMORY 上下文、SQLite 存储、原文分页、检索、生命周期与恢复工具；WORKING_SET 为兼容输入。 |
| `minicode/product_surfaces.py` | readiness、hooks、instructions、delegation、extensions 等用户可见摘要。 |
| `minicode/readiness.py` | 独立 readiness CLI，用于本地检查和 CI 门禁。 |
| `minicode/release_readiness.py` | 面向 release 的 runtime smoke 与 provider readiness 检查。 |
| `minicode/model_switcher.py` | 有界 fallback 和 failover 选择逻辑。 |
| `minicode/runtime_profiles.py` | `single`、`single-deep` 等 runtime profile。 |
| `minicode/cybernetic_orchestrator.py` | runtime control 生命周期总控。 |

## 技术文档

如果你想继续看更深的实现与产品化记录，可以从这里开始：

- [工作区总览与快速开始](../README.md)
- [Web 工作台说明](../MiniCode-Web/README.md)
- [项目学习手册](./Docs/Documentation/AgentProjectGuide/README.md)
- [会话分叉与工作树](./Docs/Documentation/SESSION_FORK_WORKTREE.md)
- [子代理持续协作](./Docs/Documentation/AGENT_COLLABORATION.md)
- [浏览器截图与检查](./Docs/Documentation/BROWSER_CHECKS.md)
- [Optimization Summary](./Docs/Documentation/OPTIMIZATION_SUMMARY.md)
- [Memory Theory](./Docs/Documentation/memory_theory.md)
- [分级上下文与窗口外无损记忆](./Docs/Documentation/PAGED_MEMORY.md)
- [Minicode-lite Productization Design](./Docs/Documentation/superpowers/specs/2026-06-05-minicode-lite-productization-design.md)
- [Minicode-lite Build Plan](./Docs/Documentation/superpowers/plans/2026-06-05-minicode-lite-productization-build.md)
- [Minicode-lite Verify Report](./Docs/Documentation/superpowers/reports/2026-06-05-minicode-lite-productization-verify.md)
- [Main MiniCode Repository](https://github.com/LiuMengxuan04/MiniCode)
