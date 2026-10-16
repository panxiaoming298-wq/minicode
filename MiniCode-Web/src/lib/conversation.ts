import type { ExecutionSnapshot, SessionTurn, TimelineItem } from "../types";
import { executionAgents } from "./agentActivity";
import { agentMessageView, correctedConsultationIds, isAgentCommunicationTool } from "./agentMessages";
import type { ToolTimelineItem } from "./runSummary";
import { hasContextRotationCommit, recoveredContextRotationIds } from "./contextRotationRecovery";
import { recoveredEditToolIds } from "./editRecovery";
import { correctedCommandResults } from "./commandRecovery";
import { toolAvailabilityFailure } from "./toolAvailability";

export interface ConversationRound {
  id: string;
  user?: Extract<TimelineItem, { type: "user" }>;
  items: TimelineItem[];
}

/** An in-flight guide is part of the current task, not a new task boundary. */
export function latestTurnItems(items: TimelineItem[]): TimelineItem[] {
  const start = items.reduce((last, item, index) => item.type === "user" && !item.steeringInputId ? index : last, 0);
  return items.slice(start);
}

export function turnElapsedMs(turn?: Pick<SessionTurn, "turnId" | "createdAt" | "completedAt">): number | undefined {
  // Legacy rows carry session-wide timestamps, not the duration of this turn.
  if (!turn || turn.turnId.startsWith("legacy-")) return undefined;
  const { createdAt, completedAt } = turn;
  if (typeof createdAt !== "number" || typeof completedAt !== "number"
    || !Number.isFinite(createdAt) || !Number.isFinite(completedAt)
    || createdAt <= 0 || completedAt < createdAt) return undefined;
  return (completedAt - createdAt) * 1000;
}

export function conversationRounds(items: TimelineItem[]): ConversationRound[] {
  const rounds: ConversationRound[] = [];
  for (const item of items) {
    if (item.type === "user") rounds.push({ id: item.id, user: item, items: [] });
    else {
      if (!rounds.length) rounds.push({ id: item.id, items: [] });
      rounds[rounds.length - 1].items.push(item);
    }
  }
  return rounds;
}

export function needsAttention(item: TimelineItem): boolean {
  return item.type === "error"
    || item.type === "approval" && !item.resolved
    || item.type === "tool" && ["error", "unknown"].includes(item.status) && !isInterruptedTool(item) && !isToolSchemaDeferral(item)
    || item.type === "assistant" && item.deliveryState === "rejected"
    || item.type === "progress" && ["failed", "stopped"].includes(item.status);
}

/** Match native cancellation receipts, without treating arbitrary errors as cancellations. */
export function isCanceledTool(item: ToolTimelineItem): boolean {
  return item.status === "error" && /^Canceled: (?:parent Runtime canceled this tool call|Tool canceled (?:before|while awaiting) permission approval|Tool canceled while awaiting permission|file edit canceled before writing)/.test((item.output || "").trim());
}

/** A native pre-execution steering interruption is not a failed operation. */
export function isInterruptedTool(item: ToolTimelineItem): boolean {
  return item.status === "error" && item.toolRecovery?.status === "interrupted"
    && item.toolRecovery.kind === "human_steering";
}

/** Native pre-execution deferrals remain unresolved until a later success receipt. */
export function isToolSchemaDeferral(item: ToolTimelineItem): boolean {
  return item.status === "error" && (item.toolInput?.status === "schema_required"
    || (item.output || "").trim().startsWith("ToolSchemaRequired:"));
}

export function deferredToolPresentation(item: ToolTimelineItem, active = false): { kind: string; label: string; summary: string } | undefined {
  if (item.status !== "error") return undefined;
  if (isToolSchemaDeferral(item)) {
    const loaded = item.toolInput?.schemaLoadedAtStep != null;
    return { kind: "工具定义加载", label: loaded ? "工具定义已加载" : active ? "正在加载工具定义" : "工具定义待加载",
      summary: loaded ? "完整参数说明已传给后续模型请求；原调用未执行，后续操作以各自回执为准。"
        : active ? "正在自动加载工具用途和完整参数说明，随后由模型重新生成调用，任务继续。"
          : "原调用因尚未加载完整参数说明而暂缓；此记录没有定义加载完成的回执。" };
  }
  const output = (item.output || "").trim();
  if (output.startsWith("Skill rules have not been delivered in this attempt.")) {
    return { kind: "规则待送达", label: "规则待送达，未执行",
      summary: "技能规则尚未送达当前执行上下文，本次调用没有执行；收到完整规则后再继续。" };
  }
  if (/^(?:Context protection: this operation was not executed\.|The whole tool batch may exceed the context protection limit\.|\[Memory preflight blocked\] The operation was not executed because)/.test(output)) {
    return { kind: "上下文暂缓", label: "上下文受限，暂未执行",
      summary: "上下文容量保护在执行前拦下了本次调用；需先整理上下文或缩小操作范围，再决定后续操作。原始记录已保留。" };
  }
  return undefined;
}

/** Count unresolved receipts by cause, not as distinct bugs in project code. */
export function toolFailureCount(items: TimelineItem[], recovered = recoveredToolIds(items), historical = new Set<string>()): number {
  return items.filter(item => item.type === "tool" && item.status === "error" && !recovered.has(item.id)
    && !historical.has(item.id) && !isCanceledTool(item) && !isInterruptedTool(item) && !isToolSchemaDeferral(item)).length;
}

/** Finished work retains failure history without implying a pending action. */
export function toolFailureLabel(count: number, active: boolean): string {
  return active ? `${count} 项调用待处理` : `${count} 次调用失败记录`;
}

export function unparsedToolInput(item: ToolTimelineItem): boolean {
  return Boolean(item.toolInput?.argumentError) || /^ModelToolArgumentsRejected: (?:classification=(?:invalid_arguments|incomplete_arguments_at_output_cap)|invalid or incomplete JSON object);/.test(item.output || "");
}

export function toolIssueSummary(items: TimelineItem[], recovered = recoveredToolIds(items)): string {
  const counts = new Map<string, number>();
  for (const item of items) {
    if (item.type !== "tool" || item.status !== "error" || recovered.has(item.id) || isCanceledTool(item) || isInterruptedTool(item) || isToolSchemaDeferral(item)) continue;
    const output = item.output || "";
    const kind = item.toolInput && ["invalid", "schema_required"].includes(item.toolInput.status)
      || /^(Input validation error|ModelToolArgumentsRejected|ToolSchemaRequired)/.test(output) ? "参数错误"
      : /^(Edit denied|Command denied|Permission denied|User denied)|User guidance:/.test(output) ? "审批未通过"
      : deferredToolPresentation(item)?.kind
        || (/Context protection|whole tool batch may exceed the context protection limit/.test(output) ? "上下文受限"
      : /^(NotExecuted:|PolicyDenied:|CandidateWriteRefused:)/.test(output) ? "执行被拦截"
      : toolAvailabilityFailure(output, item.toolName)?.label || "调用报错");
    counts.set(kind, (counts.get(kind) || 0) + 1);
  }
  return [...counts].map(([kind, count]) => `${count} 次${kind}`).join("，");
}

function canonicalInput(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalInput).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, entry]) => `${JSON.stringify(key)}:${canonicalInput(entry)}`).join(",")}}`;
  return JSON.stringify(value) ?? "undefined";
}

function recoveryInput(item: ToolTimelineItem): unknown {
  // A corrected note replaces the same owner/revision even when its text or
  // sources change. Require the native save receipt, never an assistant claim.
  if (item.toolName === "memory_note_update" && item.attemptId
      && item.input && typeof item.input === "object" && !Array.isArray(item.input)) {
    const revision = (item.input as Record<string, unknown>).expected_revision;
    if (typeof revision === "number" && Number.isSafeInteger(revision) && revision >= 0) {
      let confirmed = item.status === "error";
      if (item.status === "success") {
        try {
          const receipt = JSON.parse(item.output || "");
          confirmed = receipt?.status === "saved" && receipt.revision === revision + 1
            && typeof receipt.source_event_id === "string" && receipt.source_event_id.length > 0;
        } catch { /* Incomplete/legacy output cannot prove a corrected save. */ }
      }
      if (confirmed) return ["handoff-revision", item.attemptId, revision];
    }
  }
  return ["exact-input", item.input];
}

/** Later success must identify the same operation; a final answer alone is not recovery. */
function verifiedAlternative(failed: ToolTimelineItem, success: ToolTimelineItem): boolean {
  if (failed.toolName !== "discover_tools" || success.toolName !== "request_verification"
    || failed.toolRecovery?.kind !== "verification_alternative" || success.toolRecovery?.kind !== "verification_alternative"
    || canonicalInput(failed.input) !== canonicalInput({ names: ["run_command"] })) return false;
  try {
    const receipt = JSON.parse(success.output || "");
    return receipt?.status === "passed" && receipt.all_criteria_covered === true && receipt.candidate_unchanged === true
      && Array.isArray(receipt.checks) && receipt.checks.length > 0 && receipt.checks.every((check: { passed?: boolean }) => check?.passed === true);
  } catch { return false; }
}

export function recoveredToolIds(items: TimelineItem[]): Set<string> {
  const pending = new Map<string, ToolTimelineItem[]>();
  const nativePending: ToolTimelineItem[] = [];
  const recoveryPending: ToolTimelineItem[] = [];
  const runtimePending = new Map<string, ToolTimelineItem[]>();
  const recovered = new Set([...recoveredContextRotationIds(items), ...recoveredEditToolIds(items),
    ...correctedCommandResults(items).keys(), ...correctedConsultationIds(items)]);
  for (const item of items) {
    if (item.type === "user") { pending.clear(); runtimePending.clear(); nativePending.length = 0; recoveryPending.length = 0; continue; }
    // Old snapshots lack ownership; keep their failures visible rather than guessing.
    if (item.type !== "tool" || item.input == null || !item.executionId || !item.nodeId) continue;
    // Handoff corrections use their native terminal receipt, never input equality.
    if (item.toolName === "submit_consultation") continue;
    if (item.commandResult && (!item.commandResult.settled
      || item.status === "success" && item.commandResult.exitCode !== 0)) continue;
    if (item.toolName === "context_rotate" && item.status === "success" && !hasContextRotationCommit(item)) continue;
    if (item.toolRecovery) {
      if (item.status === "error" && item.toolRecovery.status === "agent_action") recoveryPending.push(item);
      if (item.status === "success" && item.toolRecovery.status === "recovered" && item.attemptId) {
        for (const failed of recoveryPending) {
          if (failed.toolCallId && item.toolRecovery.resolves?.some(link => link.callId === failed.toolCallId && link.step === failed.toolRecovery?.step)
            && item.executionId === failed.executionId && item.nodeId === failed.nodeId
            && item.attemptId === failed.attemptId
            && ((item.toolName === failed.toolName && canonicalInput(item.input) === canonicalInput(failed.input))
              || verifiedAlternative(failed, item))
            && failed.resultOrder !== undefined && item.startOrder !== undefined && failed.resultOrder < item.startOrder) {
            recovered.add(failed.id);
          }
        }
      }
      // A narrower successful search cannot resolve an incomplete broad search.
      // Native metadata must not fall back to a looser historical heuristic.
      if (!item.toolInput) continue;
    }
    if (item.toolInput) {
      // "other" means this was not a schema rejection. It must not disable
      // evidence of a later successful identical runtime operation. Schema
      // rejections and native recovery metadata retain their stricter links.
      if (!item.toolRecovery && item.attemptId && !["context_rotate", "run_command"].includes(item.toolName)
        && typeof item.input === "object" && !Array.isArray(item.input)
        && !unparsedToolInput(item) && !isCanceledTool(item) && !isInterruptedTool(item)) {
        const key = canonicalInput([item.executionId, item.nodeId, item.attemptId, item.toolName, recoveryInput(item)]);
        if (item.status === "error" && item.toolInput.status === "other") {
          runtimePending.set(key, [...(runtimePending.get(key) || []), item]);
        } else if (item.status === "success" && item.toolInput.status === "success") {
          runtimePending.set(key, (runtimePending.get(key) || []).filter(failed => {
            if (failed.resultOrder === undefined || item.startOrder === undefined || failed.resultOrder >= item.startOrder) return true;
            recovered.add(failed.id);
            return false;
          }));
        }
      }
      if (item.status === "error" && ["invalid", "schema_required"].includes(item.toolInput.status)) nativePending.push(item);
      if (item.status === "success" && item.toolInput.status === "success" && item.attemptId) {
        for (const failed of nativePending) {
          if (failed.toolCallId && item.toolInput.corrects?.some(link => link.callId === failed.toolCallId && link.step === failed.toolInput?.step)
            && item.executionId === failed.executionId && item.nodeId === failed.nodeId
            && item.attemptId === failed.attemptId && item.toolName === failed.toolName
            && failed.resultOrder !== undefined && item.startOrder !== undefined && failed.resultOrder < item.startOrder) {
            recovered.add(failed.id);
          }
        }
      }
      // Native schema diagnostics deliberately decline ambiguous links. A
      // legacy same-input heuristic must not override that decision.
      continue;
    }
    // Older parser failures used {} as a display fallback. It cannot identify
    // an operation or prove recovery from a later successful empty call.
    if (unparsedToolInput(item)) continue;
    // Long paged inputs are JSON previews stored as strings. Matching their
    // shared prefix cannot establish that the complete operations were equal.
    if (typeof item.input !== "object" || Array.isArray(item.input)) continue;
    // Command recovery uses its settled receipt and full execution scope; do
    // not let the older generic fallback override an unconfirmed command.
    if (item.toolName === "run_command") continue;
    // State transitions need their native commit receipt, even with identical inputs.
    if (item.toolName === "context_rotate") continue;
    const key = canonicalInput([item.executionId, item.nodeId, item.toolName, recoveryInput(item)]);
    if (item.status === "error") pending.set(key, [...(pending.get(key) || []), item]);
    if (item.status === "success") {
      const unresolved = (pending.get(key) || []).filter((failed) => {
        if (failed.resultOrder !== undefined && item.startOrder !== undefined && failed.resultOrder < item.startOrder) {
          recovered.add(failed.id);
          return false;
        }
        return true;
      });
      pending.set(key, unresolved);
    }
  }
  return recovered;
}

/** Native node acceptance resolves a failed attempt, not each individual tool in it. */
export function historicalAttemptItemIds(items: TimelineItem[], execution?: ExecutionSnapshot | null): Set<string> {
  const historical = new Set<string>();
  if (!execution) return historical;
  for (const node of execution.nodes) {
    if (node.state !== "SUCCEEDED" || node.recovery?.status !== "recovered") continue;
    const failed = new Set(node.recovery.failedAttemptIds);
    for (const item of items) if (item.executionId === execution.executionId && item.nodeId === node.nodeId
      && item.attemptId && failed.has(item.attemptId)) historical.add(item.id);
  }
  return historical;
}

/** Keep every tool at its original process position; unfinished results remain unknown on restoration. */
export function presentRound(items: TimelineItem[], active = false, execution?: ExecutionSnapshot | null) {
  const settled = active ? items : items.map((item): TimelineItem => item.type === "tool" && item.status === "running"
    ? { ...item, status: "unknown", output: item.output || "本轮已结束，未收到这次操作的结果。" }
    : item.type === "assistant" ? { ...item, streaming: false,
      deliveryState: item.deliveryState === "pending" ? "rejected" : item.deliveryState } : item);
  const primaryExecutions = new Set(settled.filter((item) => item.nodeId === "primary").map((item) => item.executionId));
  const memberNodes = new Set(execution ? executionAgents(execution).filter(owner => owner.kind === "child").flatMap(owner => owner.nodes.map(node => node.nodeId)) : []);
  // A member's final answer is its task result, not the whole conversation's delivery.
  // The saved roster also proves ownership when no primary message was stored.
  const belongsToMember = (item: TimelineItem) => Boolean(item.nodeId && item.nodeId !== "primary"
    && (primaryExecutions.has(item.executionId)
      || (!item.executionId || item.executionId === execution?.executionId) && memberNodes.has(item.nodeId)));
  let answerId = settled.filter((item) => item.type === "assistant" && item.phase === "final_answer"
    && !belongsToMember(item)).at(-1)?.id;
  for (let i = settled.length - 1; !answerId && i >= 0; i--) {
    const item = settled[i];
    if (belongsToMember(item)) continue;
    if (item.type === "assistant" && item.phase !== "commentary") { answerId = item.id; break; }
    // A message before a subsequent tool call is a progress note, not the final answer.
    if (item.type === "tool") break;
  }
  const recovered = recoveredToolIds(settled);
  const historical = historicalAttemptItemIds(settled, execution);
  // Tool failures belong beside their surrounding commentary and retries, not below the whole process.
  // Pending approvals and run-level failures still need a separate, visible action surface.
  const attention = (item: TimelineItem) => item.type !== "tool"
    && !historical.has(item.id)
    && !(item.type === "assistant" && belongsToMember(item)) && needsAttention(item);
  // Questions remain at their original process position. The inline process
  // renderer keeps them visible when surrounding work records are collapsed.
  const visible = (item: TimelineItem) => item.id === answerId || attention(item);
  const preparations = new Set<string>();
  const repeatPreparation = (item: TimelineItem) => {
    if (item.type !== "progress" || item.status !== "complete" || !item.executionId || !item.nodeId
      || !/^准备(?:工作区|辅助任务 [a-f0-9]+)：.*（已处理 \d+ 个文件）完成$/.test(item.content)) return false;
    const key = JSON.stringify([item.executionId, item.nodeId, item.content]);
    if (preparations.has(key)) return true;
    preparations.add(key);
    return false;
  };
  return {
    process: settled.filter((item) => !visible(item) && !repeatPreparation(item)),
    outcome: settled.filter(visible),
    recovered,
    historical,
  };
}

function inputText(item: ToolTimelineItem, keys: string[]): string {
  if (!item.input || typeof item.input !== "object") return "";
  const input = item.input as Record<string, unknown>;
  for (const key of keys) {
    const value = input[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (key === "command" && Array.isArray(value) && value.every((part) => typeof part === "string")) return value.join(" ");
  }
  return "";
}

function toolKind(item: ToolTimelineItem): string {
  return item.toolName.replace(/^.*[.:/]/, "");
}

/** Only successful Skill loads count; mentions, discovery and failed calls do not. */
export function usedSkills(items: TimelineItem[]): string[] {
  const names = new Set<string>();
  for (const item of items) {
    if (item.type !== "tool" || item.status !== "success" || toolKind(item) !== "load_skill") continue;
    let name = inputText(item, ["name"]);
    if (!name) {
      try {
        const receipt: unknown = JSON.parse(item.output);
        if (receipt && typeof receipt === "object" && "name" in receipt && typeof receipt.name === "string") name = receipt.name.trim();
      } catch { /* Older results may contain plain text instead of a receipt. */ }
    }
    if (name) names.add(name);
  }
  return [...names];
}

export function toolActivity(item: ToolTimelineItem): { label: string; target: string } {
  if (isAgentCommunicationTool(item)) return { label: agentMessageView(item).action, target: "" };
  const path = inputText(item, ["path", "file_path", "filePath", "filename", "file", "directory"]);
  const kind = toolKind(item);
  if (kind === "poll_task_results") return { label: inputText(item, ["patch_id"]) ? "查看子代理修改" : "查看子代理结果", target: path };
  const delegationLabels: Record<string, string> = {
    wait_agents: "等待子任务", spawn_agents: "创建子任务", poll_agents: "查看子任务进度",
    spawn_builders: "创建修改子任务", wait_dependencies: "等待子代理结果", consult_agents: "请求子代理咨询",
    select_builder_candidate: "选择子代理修改", cancel_task: "取消子代理任务", submit_candidate: "提交本轮结果",
    memory_note_update: "保存交接笔记", context_rotate: "整理上下文",
    run_acceptance_checks: "检查构建与验收",
  };
  if (delegationLabels[kind]) return { label: delegationLabels[kind], target: "" };
  if (["read_file", "read_files"].includes(kind)) return { label: "读取文件", target: path };
  if (kind === "display_image") return { label: "展示图片", target: path };
  if (kind === "view_image") return { label: "读取图片", target: path };
  if (kind === "record_task_checks") return { label: "记录检查结果", target: "" };
  if (["list_files", "list_directory", "file_tree"].includes(kind)) return { label: "浏览目录", target: path };
  if (["grep_files", "search_files", "search_code"].includes(kind)) return { label: "搜索代码", target: inputText(item, ["pattern", "query", "search"]) };
  if (["write_file", "edit_file", "apply_patch", "patch_file"].includes(kind)) return { label: "修改文件", target: path };
  if (["run_command", "exec_command", "shell", "bash", "terminal"].includes(kind)) return { label: "运行命令", target: inputText(item, ["command", "cmd", "script"]) };
  if (kind === "start_service") return { label: "启动开发服务", target: inputText(item, ["ready_url", "command"]) };
  if (kind === "service_status") return { label: "检查服务状态", target: inputText(item, ["service_id"]) };
  if (kind === "service_logs") return { label: "读取服务日志", target: inputText(item, ["service_id"]) };
  if (kind === "stop_service") return { label: "停止开发服务", target: inputText(item, ["service_id"]) };
  if (kind === "web_search") return { label: "搜索网页", target: inputText(item, ["query", "q"]) };
  if (kind === "web_fetch") return { label: "读取网页", target: inputText(item, ["url"]) };
  if (kind === "discover_tools") return { label: "查找可用工具", target: "" };
  if (kind === "request_verification") return { label: "请求登记的验证检查", target: "" };
  if (kind === "load_skill") return { label: "加载 Skill", target: inputText(item, ["name"]) };
  return { label: item.toolName, target: path };
}

/** Counts come only from recorded successful operations, never from a generated plan. */
export function activitySummary(items: TimelineItem[]): string {
  const tools = items.filter((item): item is ToolTimelineItem => item.type === "tool" && item.status === "success");
  const counts = new Map<string, { count: number; targets: Set<string>; allTargeted: boolean }>();
  for (const item of tools) {
    const { label, target } = toolActivity(item);
    const group = counts.get(label) || { count: 0, targets: new Set<string>(), allTargeted: true };
    group.count += 1;
    group.allTargeted &&= Boolean(target);
    if (target) group.targets.add(target);
    counts.set(label, group);
  }
  return [...counts].map(([label, group]) => {
    if (label === "读取文件" && group.allTargeted) return `已读取 ${group.targets.size} 个文件`;
    if (label === "修改文件" && group.allTargeted) return `已修改 ${group.targets.size} 个文件`;
    if (label === "浏览目录" && group.allTargeted) return `已浏览 ${group.targets.size} 个目录`;
    return `${label} ${group.count} 次`;
  }).join(" · ") || "过程说明";
}
