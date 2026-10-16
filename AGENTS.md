# MiniCode 工作区约定

## Docker Desktop 中间件分组

- 本工作区需要的向量数据库、关系型数据库、缓存、消息队列、对象存储及其他存储中间件，统一纳入 Docker Desktop 的 `minicode` Compose 应用组。
- 新增中间件时，优先在 `MiniCode-Python-repo/docker-compose.yml` 中增加服务、网络和具名卷；不要为单个中间件创建独立 Compose 项目或使用独立的 `docker run` 常驻容器。
- Compose 项目名保持为 `minicode`。启动、停止和检查中间件时，应通过该 Compose 文件操作，避免改变项目名导致 Docker Desktop 出现多个 MiniCode 应用组。
- 除非当前功能确实需要，不预装向量数据库或其他中间件；新增后必须配置健康检查和具名卷，且不得在未获明确要求时删除已有运行时卷。

## OpenSpec 共享工作流

- 通用 OpenSpec 技能安装在用户级目录，所有工作区都可调用：Codex 使用 `$CODEX_HOME/skills`（默认 `~/.codex/skills`），MiniCode 使用当前状态目录的 `skills/`。新项目需要自己的 OpenSpec 初始化，不能把其他项目的规格当作当前项目的规格。
- 本工作区的 OpenSpec 根目录是 `MiniCode-Python-repo`，规格和变更保存在其已有的 `openspec/` 中；从外层工作区调用时，将 OpenSpec 命令的工作目录设为该后端目录。
- 后端的项目级适配文件分别位于 Codex 的 `.agents/skills/openspec-*` 与 MiniCode 的 `.claude/skills/openspec-*`；外层工作区可使用 `minicode-openspec` 技能定位官方工作流，直接调用全局技能时也须遵守本工作区的共享根目录约定。
- 刷新后端技能时，在后端目录运行 `openspec init --tools 'codex,claude' --profile core`；PowerShell 中的双工具参数必须加引号。
