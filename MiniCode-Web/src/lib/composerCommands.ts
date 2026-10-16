export type ComposerCommandId = "compact" | "context" | "help" | "tools" | "skills" | "mcp"
  | "model" | "config" | "extensions" | "sessions" | "history" | "status" | "state" | "session"
  | "cost" | "tasks" | "checkpoints" | "delegation" | "memory" | "instructions" | "hooks"
  | "readiness" | "permissions" | "task-file" | "ls";

export type ComposerInspectorSection = "tools" | "mcp" | "instructions" | "memory" | "health"
  | "usage" | "plan" | "checkpoints";

export type ComposerNavigation =
  | { kind: "inspector"; tab: "files" | "run" | "agents" | "capabilities" | "context"; section?: ComposerInspectorSection }
  | { kind: "settings" }
  | { kind: "extensions"; view: "skills" | "extensions" }
  | { kind: "history"; search: boolean };

type ComposerCommandAction = ComposerNavigation | { kind: "compact" } | { kind: "help" }
  | { kind: "permissions" } | { kind: "task-settings" };

export interface ComposerCommand {
  id: ComposerCommandId;
  name: string;
  description: string;
  action: ComposerCommandAction;
  requires?: "session" | "project";
  disabledReason?: string;
}

export const COMPOSER_COMMANDS: readonly ComposerCommand[] = [
  { id: "compact", name: "整理上下文", description: "保存交接笔记，收起旧上下文；原始历史保留", action: { kind: "compact" }, requires: "session" },
  { id: "context", name: "查看上下文", description: "查看当前会话的上下文占用与明细", action: { kind: "inspector", tab: "context" }, requires: "session" },
  { id: "help", name: "命令帮助", description: "展开浏览器可用的命令与技能，输入名称筛选", action: { kind: "help" } },
  { id: "tools", name: "工具列表", description: "打开能力面板，查看模型可调用的工具", action: { kind: "inspector", tab: "capabilities", section: "tools" } },
  { id: "skills", name: "技能管理", description: "浏览、安装与使用当前作用域的技能", action: { kind: "extensions", view: "skills" } },
  { id: "mcp", name: "MCP 连接", description: "查看 MCP 服务器、连接状态与工具数量", action: { kind: "inspector", tab: "capabilities", section: "mcp" } },
  { id: "model", name: "模型设置", description: "打开设置，查看或选择模型与提供商", action: { kind: "settings" } },
  { id: "config", name: "应用设置", description: "管理模型连接、Agent 协作与运行偏好", action: { kind: "settings" } },
  { id: "extensions", name: "扩展管理", description: "查看已安装扩展及其启用、信任与健康状态", action: { kind: "extensions", view: "extensions" } },
  { id: "sessions", name: "会话列表", description: "打开任务栏，浏览项目与历史对话", action: { kind: "history", search: false } },
  { id: "history", name: "搜索历史", description: "打开任务栏搜索，查找项目与历史对话", action: { kind: "history", search: true } },
  { id: "status", name: "运行状态", description: "查看当前会话的执行状态与实时事件", action: { kind: "inspector", tab: "run" }, requires: "session" },
  { id: "state", name: "运行详情", description: "打开运行面板，查看执行计划、结果与验证", action: { kind: "inspector", tab: "run" }, requires: "session" },
  { id: "session", name: "当前会话", description: "查看当前会话的运行记录与恢复检查点", action: { kind: "inspector", tab: "run" }, requires: "session" },
  { id: "cost", name: "用量与费用", description: "查看当前运行的 Token 用量与费用记录", action: { kind: "inspector", tab: "run", section: "usage" }, requires: "session" },
  { id: "tasks", name: "运行计划", description: "查看当前任务的执行安排与节点进度", action: { kind: "inspector", tab: "run", section: "plan" }, requires: "session" },
  { id: "checkpoints", name: "恢复检查点", description: "查看当前会话保存的可恢复边界", action: { kind: "inspector", tab: "run", section: "checkpoints" }, requires: "session" },
  { id: "delegation", name: "子代理", description: "打开子代理面板，查看协作与执行进度", action: { kind: "inspector", tab: "agents" }, requires: "session" },
  { id: "memory", name: "记忆状态", description: "查看当前作用域的记忆分类与条目数量", action: { kind: "inspector", tab: "context", section: "memory" } },
  { id: "instructions", name: "规则与偏好", description: "查看规则文件及最近请求的实际加载情况", action: { kind: "inspector", tab: "context", section: "instructions" } },
  { id: "hooks", name: "自动化钩子", description: "查看 Hooks 在工具执行、会话保存等事件上的启用情况", action: { kind: "inspector", tab: "context", section: "health" } },
  { id: "readiness", name: "模型配置就绪度", description: "查看主模型、备用模型的本机配置检查结果", action: { kind: "inspector", tab: "context", section: "health" } },
  { id: "permissions", name: "权限模式", description: "定位当前对话的权限模式选择器", action: { kind: "permissions" } },
  { id: "task-file", name: "任务验收设置", description: "展开任务设置，查看或选择验收文件", action: { kind: "task-settings" }, requires: "project" },
  { id: "ls", name: "项目文件", description: "打开文件面板，浏览当前项目目录", action: { kind: "inspector", tab: "files" }, requires: "project" },
];

export function matchingComposerCommands(commands: readonly ComposerCommand[], value: string): ComposerCommand[] {
  const match = /^\/([^\s]*)$/.exec(value);
  if (!match) return [];
  const query = match[1].toLocaleLowerCase();
  return commands.filter(command => `${command.id} ${command.name} ${command.description}`.toLocaleLowerCase().includes(query))
    .sort((a, b) => Number(b.id === query) - Number(a.id === query)
      || Number(b.id.startsWith(query)) - Number(a.id.startsWith(query)));
}

export function exactComposerCommand(value: string): ComposerCommandId | undefined {
  const command = value.trim().toLocaleLowerCase();
  return COMPOSER_COMMANDS.find(item => command === `/${item.id}`)?.id;
}

// Use the existing model/tool workflow in the same session. Submission is not
// a successful rotation: its native tool receipts remain the source of truth.
export const CONTEXT_ROTATION_REQUEST = [
  "请手动整理当前会话的上下文，本轮只执行交接笔记与上下文轮换，不开展新的业务任务。",
  "先确认当前任务支持整理上下文，再根据可验证的历史来源更新交接笔记，包括已确认结论、已排除方案、未解决问题和下一步。",
  "保留原任务要求与未完成事项，笔记保存成功后执行一次手动上下文轮换。",
  "保持原始历史、用户要求和必需证据完整，不修改项目文件，不重放历史业务工具调用。",
  "只有收到轮换成功的真实回执后才报告成功；若工具不可用、当前模式不支持、没有可整理内容或调用失败，请如实说明原因，不能把普通文字总结当成完成了轮换。",
  "最后简短报告结果和保留的下一步，然后等待用户继续。",
].join("\n");

/** Recognize the command's saved prompt without folding ordinary conversation. */
export function isContextRotationRequest(content: string): boolean {
  return content.replace(/\r\n/g, "\n").trim() === CONTEXT_ROTATION_REQUEST;
}
