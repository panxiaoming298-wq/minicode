# MiniCode 本地扩展平台

这份文档描述 MiniCode 当前实现的扩展契约。扩展不是另一套运行时，而是把
Skill、MCP、模型 Provider、Tool、Hook、斜杠命令和 Agent Profile 作为同一
安装包里的不同贡献类型。这样可以整体分发，也可以由一个包只贡献其中一项。

## 1. 包与状态边界

扩展包是一个包含 `extension.json` 的完整目录：

```text
team-review/
├─ extension.json
├─ skills/review/SKILL.md
├─ prompts/review.md
├─ agents/reviewer.md
├─ assets/review-help.md
└─ scripts/
   ├─ check.py
   └─ audit.py
```

MiniCode 支持两个作用域：

| 作用域 | 安装目录 | 用途 |
| --- | --- | --- |
| `global` | `%USERPROFILE%\.mini-code\extensions\<id>` | 所有项目可用 |
| `project` | `<workspace>\.mini-code\extensions\<id>` | 随项目共享；同 ID 时覆盖全局包 |

启用、信任和授权写在安装目录外的 `extensions-state.json`；来源、版本和内容哈希
写在 `extensions.lock`。因此更新包内容不会暗中改变信任模型，安装也不会自动等同
于信任。

## 2. 完整清单示例

```json
{
  "apiVersion": "minicode.dev/v1",
  "id": "team.review",
  "name": "Team Review",
  "version": "1.2.0",
  "publisher": "Example Team",
  "description": "团队代码评审能力包",
  "permissions": [
    "process:spawn",
    "workspace:read",
    "network:access",
    "secrets:read"
  ],
  "activationEvents": ["startup"],
  "dependencies": {
    "team.base": "^1.0.0"
  },
  "contributes": {
    "skills": [
      { "name": "team-review", "path": "skills/review" }
    ],
    "mcpServers": {
      "knowledge": {
        "url": "https://mcp.example.com/rpc",
        "transport": "streamable-http",
        "headers": {
          "Authorization": { "secretRef": "TEAM_MCP_TOKEN" }
        }
      }
    },
    "providers": {
      "team-models": {
        "label": "Team Models",
        "transport": "openai-compatible",
        "baseUrl": "https://models.example.com/v1",
        "apiKey": { "secretRef": "TEAM_MODELS_KEY" },
        "models": ["team-code-large"]
      }
    },
    "tools": [
      {
        "name": "policy-check",
        "description": "检查一个路径是否符合团队规则",
        "command": ["python", "scripts/check.py"],
        "effect": "read_only",
        "replayPolicy": "safe",
        "timeoutSeconds": 20,
        "inputSchema": {
          "type": "object",
          "properties": { "path": { "type": "string" } },
          "required": ["path"],
          "additionalProperties": false
        }
      }
    ],
    "hooks": [
      {
        "name": "audit-tool-errors",
        "event": "post_tool_use",
        "command": ["python", "scripts/audit.py"],
        "match": { "errorsOnly": true }
      }
    ],
    "commands": [
      {
        "name": "team-review",
        "aliases": ["tr"],
        "argumentHint": "<path>",
        "description": "按团队规范评审目标",
        "promptFile": "prompts/review.md"
      }
    ],
    "agents": [
      {
        "name": "safe-reviewer",
        "description": "只读评审员",
        "instructionsFile": "agents/reviewer.md",
        "executionProfile": "standard",
        "toolAllow": ["read_file", "grep_files", "ext__team_review__*"],
        "toolDeny": ["write_file", "edit_file", "run_command"]
      }
    ],
    "ui": [
      {
        "name": "review-help",
        "path": "assets/review-help.md"
      }
    ]
  }
}
```

`ui` 当前只用于声明可检查的元数据/资源，不允许扩展把任意 JavaScript 注入
MiniCode Web。管理界面由宿主统一渲染。

编辑器和构建工具可以使用仓库内的机器可读 Schema：
`openspec/specs/extension-packaging/extension.schema.json`。

## 3. 各贡献如何进入运行时

| 贡献 | 激活结果 | 关键边界 |
| --- | --- | --- |
| Skill | 进入正常 Skill 搜索和 `load_skill` | 整包保留 `references/`、`scripts/`、资源文件 |
| MCP | 注册 stdio 或 Streamable HTTP 服务 | 每个服务隔离诊断；名称自动加扩展命名空间 |
| Provider | 映射到 OpenAI-compatible 模型适配器 | Base URL 固定在描述符，API Key 使用环境引用 |
| Tool | 注册 `ext__<扩展>__<工具>` | 子进程 JSON 协议、超时、输入/输出上限、效果声明 |
| Hook | 订阅生命周期事件 | 子进程执行；能力检查不会触发 Hook |
| Command | `/命令 参数` 展开为提示词 | CLI、TUI、headless、Web worker 一致 |
| Agent | 注入指令并可约束模型/Provider/执行档位/Tool | Tool 白名单和黑名单支持通配符 |
| UI | 暴露声明式资源元数据 | 不执行扩展浏览器代码 |

外部 Tool 的标准输入为 JSON：

```json
{
  "protocolVersion": 1,
  "tool": "policy-check",
  "input": { "path": "src" },
  "context": {
    "cwd": "D:/project",
    "runId": "...",
    "nodeId": "...",
    "attemptId": "...",
    "toolCallId": "...",
    "idempotencyKey": "..."
  }
}
```

标准输出必须是单个 JSON 对象，例如：

```json
{ "ok": true, "output": "检查通过" }
```

## 4. 权限与密钥

安装、信任、启用是三个独立状态：

1. 安装只复制并验证文件，默认不信任。
2. 信任时只能授予清单已经声明的权限；不能追加未声明权限。
3. 禁用会保留包、信任和锁信息，但下一次构建运行时不会激活贡献。

当前运行时识别的权限包括：

- `workspace:read` / `workspace:write`
- `process:spawn` / `shell:execute`
- `network:access`
- `secrets:read`
- `env:inherit`

Tool/Hook 默认只获得最小环境。只有包同时声明并获授 `env:inherit` 时才继承完整
进程环境；调用 shell 解释器还需要 `shell:execute`。密钥只写引用：

```json
{ "Authorization": { "secretRef": "TEAM_MCP_TOKEN" } }
```

或者：

```json
{ "Authorization": "secret://env/TEAM_MCP_TOKEN" }
```

MiniCode 不把解析后的值写入状态、锁、诊断、任务事件或 API 响应。缺少变量时，
对应贡献会显示 `blocked`，其他扩展仍可继续加载。

## 5. 安装与运维命令

来源可以是本地目录、ZIP、Git URL，也可以是只有 `SKILL.md` 的完整 Skill 目录。
普通 Skill 目录会被自动包装为只贡献 Skill 的 v1 扩展。

```text
/extensions
/extension-inspect project:team.review
/extension-install ./team-review --scope=project
/extension-install https://example.com/team-review.git --scope=global --trust
/extension-trust project:team.review
/extension-untrust project:team.review
/extension-enable project:team.review
/extension-disable project:team.review
/extension-health project:team.review
/extension-rollback project:team.review
/extension-uninstall project:team.review
```

更新先进入 `.staging` 并完成校验，再原子替换。旧版本进入 `.backups`；卸载包进入
`.trash`，命令会返回可恢复路径。托管安装包被修改后，内容哈希不再匹配锁，健康
状态变为 `integrity_mismatch`，不会激活。

## 6. 本机 Web API

管理 API 只绑定 MiniCode 的本机服务边界：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/extensions?workspace=...` | 列表、贡献摘要和 MCP 诊断 |
| `POST` | `/api/extensions/install` | 安装目录、ZIP 或 Git 包 |
| `PATCH` | `/api/extensions/{scope}/{id}` | 启停、信任和授权 |
| `GET` | `/api/extensions/{scope}/{id}/health` | 逐项健康检查 |
| `POST` | `/api/extensions/{scope}/{id}/rollback` | 回滚到上一份备份 |
| `DELETE` | `/api/extensions/{scope}/{id}` | 可恢复卸载 |

响应包含 manifest、scope、版本、trust、权限、贡献数量和健康原因，但不包含密钥值。

## 7. 阿里云百炼

百炼已作为一等 Provider 接入，无需额外扩展：

```powershell
$env:MINI_CODE_PROVIDER = "bailian"
$env:MINI_CODE_MODEL = "qwen-plus"
$env:DASHSCOPE_API_KEY = "你的百炼 API Key"
$env:DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
minicode-py --validate-config
```

默认模型列表包含 `qwen-plus`、`qwen-max` 和 `qwen-turbo`。不同地域的 API Key 与
Base URL 需要匹配；北京地域默认使用上面的兼容端点。

ChatGPT 订阅与 OpenAI API 账单彼此独立，订阅本身不会自动提供可供本项目调用的
OpenAI API Key。无论 OpenAI 还是百炼，都应在各自开发者控制台创建并单独计费。

## 8. 发布前检查

一个可共享扩展至少应验证：

1. `/extension-health` 全部通过；
2. 未信任时没有贡献进入运行时；
3. 缺少一个密钥时只阻塞对应贡献；
4. Tool/Hook 超时和非 JSON 输出能被隔离；
5. 更新失败不替换当前版本，成功更新可以回滚；
6. 包内没有符号链接、路径穿越和密钥明文；
7. 项目包与全局包同 ID 时，项目包的覆盖行为符合预期。
