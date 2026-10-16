import type { ExecutionNode, ExecutionSnapshot, RunStatus, RuntimeTraceView, TimelineItem } from "../types";
import { agentProcessEntries, settledAgentState, type AgentActivityOwner, type AgentProcessEntry } from "./agentActivity";
import { historicalAttemptItemIds, presentRound, type ConversationRound } from "./conversation";
import { executionForRun, executionWithTrace } from "./execution";
import { traceForRun } from "./runtime";

export interface AgentRoundSource {
  round: ConversationRound;
  execution?: ExecutionSnapshot | null;
  status: RunStatus;
}

export type AgentPanelTask = Extract<AgentProcessEntry, { type: "agent-task" }> & {
  execution: ExecutionSnapshot;
  active: boolean;
  status: RunStatus;
  recovered: Set<string>;
};

export interface AgentConversation {
  key: string;
  owner: AgentActivityOwner;
  tasks: AgentPanelTask[];
}

export function agentConversationKey(execution: ExecutionSnapshot, owner: AgentActivityOwner): string {
  const origin = execution.agentTasks?.find(task => task.sessionId === owner.key)?.sessionOriginRunId;
  return JSON.stringify([origin || execution.executionId, owner.key]);
}

export function executionForRound(round: ConversationRound, execution: ExecutionSnapshot | null | undefined, traces: RuntimeTraceView[]) {
  if (execution) return executionWithTrace(execution, traceForRun(traces, execution.executionId));
  const ids = [...new Set(round.items.map(item => item.executionId).filter(Boolean))];
  return ids.length === 1 ? executionForRun(null, traces.find(trace => trace.runId === ids[0] || trace.webRunId === ids[0]) || null) : null;
}

/** The panel reads the same loaded rounds as the main conversation, including live updates. */
export function agentConversations(sources: AgentRoundSource[], traces: RuntimeTraceView[]): AgentConversation[] {
  const conversations = new Map<string, AgentConversation>();
  for (const source of sources) {
    const execution = executionForRound(source.round, source.execution, traces);
    if (!execution) continue;
    const active = ["running", "queued", "stopping"].includes(source.status);
    const view = presentRound(source.round.items, active, execution);
    const normalized = new Map([...view.process, ...view.outcome].map(item => [item.id, item]));
    const items = source.round.items.map(item => normalized.get(item.id) || item);
    for (const entry of agentProcessEntries(items, execution)) {
      if (entry.type !== "agent-task") continue;
      const key = agentConversationKey(execution, entry.owner);
      const conversation = conversations.get(key) || { key, owner: entry.owner, tasks: [] };
      const taskActive = active && !["SUCCEEDED", "CANCELED", "SUPERSEDED", "FAILED_TERMINAL", "UNKNOWN"].includes(entry.node.state);
      const task = { ...entry, execution, active: taskActive, status: source.status, recovered: view.recovered };
      const previous = conversation.tasks.findIndex(item => item.node.nodeId === entry.node.nodeId);
      if (previous < 0) conversation.tasks.push(task);
      else if (entry.items.length || !conversation.tasks[previous].items.length) conversation.tasks[previous] = task;
      conversations.set(key, conversation);
    }
  }
  return [...conversations.values()].map(conversation => {
    // Persisted maps can be sorted by hashed task IDs; use the recorded round order.
    conversation.tasks.sort((a, b) => {
      const first = a.execution.agentTasks?.find(task => task.taskId === a.node.nodeId)?.generation;
      const second = b.execution.agentTasks?.find(task => task.taskId === b.node.nodeId)?.generation;
      return first !== undefined && second !== undefined ? first - second : 0;
    });
    return conversation;
  });
}

export function agentTaskState(task: AgentPanelTask): string {
  return settledAgentState(task.node.state, task.active, task.status === "stopped");
}

export function agentContinuationLabel(task: AgentPanelTask): string {
  const metadata = task.execution.agentTasks?.find(item => item.taskId === task.node.nodeId);
  if (metadata?.sessionState === "retired") return "已结束使用";
  if (task.node.failure?.kind === "ProviderBalanceUnavailable") return "余额不足 · 等待继续";
  if (metadata?.sessionState === "interrupted") return metadata.continuationAvailable ? "已中断 · 可继续任务" : "本轮已中断";
  const implementation = agentImplementationLabel(task.node, task.execution);
  if (implementation) return implementation;
  if (agentTaskState(task) === "SUCCEEDED" && metadata?.continuationAvailable) return "本轮完成 · 可继续接任务";
  if (metadata?.recoveredFromRunId && task.active) return "已恢复原子代理 · 继续执行";
  return "";
}

/** A writer returning its result does not establish that its checks passed. */
export function agentImplementationLabel(node: ExecutionNode, execution?: ExecutionSnapshot | null): string {
  if (node.state !== "SUCCEEDED" || node.role !== "shared_writer") return "";
  const task = execution?.agentTasks?.find(task => task.taskId === node.nodeId);
  if (task?.localCheckStatus === "failed") return "实现已提交 · 检查未通过";
  if (task?.localCheckStatus === "passed") return "实现已提交 · 局部检查通过";
  // Legacy receipts have no ordered check status. Do not infer later recovery
  // from a key-sorted array or let an old PASS hide a newer failed check.
  if (task?.checks?.some(check => check.state === "completed" && !check.passed)) return "实现已提交 · 检查未通过";
  return "实现已提交 · 待验证";
}

export function agentConversationState(conversation: AgentConversation): string {
  const states = conversation.tasks.map(agentTaskState);
  return states.find(state => ["RUNNING", "CLAIMED", "VERIFYING", "WAITING_APPROVAL", "WAITING_INPUT", "WAITING_CHILDREN", "READY", "PENDING", "FAILED_RETRYABLE"].includes(state))
    || states.at(-1) || "UNKNOWN";
}

export function agentTaskObjective(objective: string): string {
  return objective.replace(/^Independently verify (?:the integrated result for this original request|the delivered candidate for):\s*/, "核验最终结果是否满足原任务：");
}

export function agentTaskTranscript(task: AgentPanelTask) {
  // Approval controls stay in the main conversation, where the attention banner can reach them.
  const approvals = task.items.filter((item): item is Extract<TimelineItem, { type: "approval" }> => item.type === "approval" && !item.resolved);
  const items = task.items.filter(item => item.type !== "approval" || item.resolved);
  const historical = historicalAttemptItemIds(items, task.execution);
  const history = items.filter(item => historical.has(item.id));
  // This is the child's own conversation: its final answer is the outcome here.
  const view = presentRound(items.filter(item => !historical.has(item.id)), task.active);
  const answer = view.outcome.find(item => item.type === "assistant" && item.phase !== "commentary" && !["pending", "rejected"].includes(item.deliveryState || ""));
  return { ...view, history,
    approvals, summary: !answer ? task.node.summary : "" };
}
