import type { ExecutionNode, ExecutionProfile, ExecutionSnapshot, RunEvent, RunInfo, RunInput, RunStatus, RuntimeTraceView, TimelineItem } from "../types";
import { agentOwner } from "./agentActivity";

export const profileLabels: Record<ExecutionProfile, string> = { standard: "Standard", deep: "Deep", ultra: "Ultra" };

export function advisoryFailureLabel(node: Pick<ExecutionNode, "state" | "criticality" | "diagnosis">): string {
  if (node.state !== "FAILED_TERMINAL" || node.criticality !== "advisory" || node.diagnosis?.fallbackAllowed !== true) return "";
  return node.diagnosis.acknowledged === true ? "参考咨询失败 · 主 Agent 已接管" : "参考咨询失败 · 等待主 Agent 接管";
}
export function childRecoveryLabel(node: ExecutionNode, state = node.state): string {
  if (node.recovery?.status === "recovered" && state === "SUCCEEDED") return "重试后已完成";
  if (node.recovery?.status === "retrying" && ["READY", "CLAIMED", "RUNNING", "VERIFYING", "FAILED_RETRYABLE"].includes(state)) return "正在恢复";
  return "";
}
export const nodeStateLabels: Record<string, string> = {
  PENDING: "等待前置任务", READY: "等待开始", CLAIMED: "准备上下文", RUNNING: "执行中",
  VERIFYING: "检查结果", WAITING_INPUT: "需要补充", WAITING_APPROVAL: "等待确认", WAITING_CHILDREN: "等待辅助结果",
  SUCCEEDED: "已完成", FAILED_RETRYABLE: "准备重试", FAILED_TERMINAL: "未完成",
  CANCELED: "已停止", SUPERSEDED: "已调整",
  UNKNOWN: "结果未确认",
};

export function mergeRunInput(items: RunInput[], input: RunInput): RunInput[] {
  const previous = items.find((item) => item.inputId === input.inputId);
  if (previous && (input.version || 0) < (previous.version || 0)) return items;
  // HTTP acknowledgement may arrive after a newer SSE application event.
  if (previous && input.status === "accepted" && previous.status !== "accepted") return items;
  if (previous && ["completed", "canceled"].includes(previous.status) && ["starting", "applied"].includes(input.status)) return items;
  if (previous?.updatedAt && input.updatedAt && input.updatedAt < previous.updatedAt) return items;
  return previous ? items.map((item) => item.inputId === input.inputId ? { ...item, ...input } : item) : [...items, input];
}

export function updateRunActivity(run: RunInfo, event: RunEvent): RunInfo {
  const modelKey = `${event.executionId || ""}:${event.nodeId || ""}:${event.attemptId || ""}`;
  return { ...run, updatedAt: event.timestamp,
    ...(event.kind === "status" ? { reviewPending: event.status === "failed" && event.reviewPending === true } : {}),
    ...(event.kind === "error" ? { reviewPending: false } : {}),
    ...(event.kind === "question_requested" && event.requestId && event.question ? {
      pendingQuestions: [...(run.pendingQuestions || []).filter(question => question.requestId !== event.requestId), event],
    } : {}),
    ...(event.kind === "question_resolved" ? {
      pendingQuestions: (run.pendingQuestions || []).filter(question => question.requestId !== event.requestId),
    } : {}),
    ...(event.kind === "input_capabilities" ? { acceptingSteering: event.acceptingSteering === true } : {}),
    ...(event.fileChanges ? { fileChanges: event.fileChanges } : {}),
    ...(event.execution ? { execution: event.execution } : {}),
    ...(event.runInput ? { inputs: mergeRunInput(run.inputs || [], { ...event.runInput, updatedAt: event.timestamp }) } : {}),
    ...(event.kind === "model_status" ? { waitingModels: event.modelState === "waiting" ? [...new Set([...(run.waitingModels || []), modelKey])] : run.waitingModels?.filter((key) => key !== modelKey) } : {}),
  };
}

/** Count received questions, including answered ones, within the current turn. */
export function questionProgress(run: Pick<RunInfo, "runId" | "pendingQuestions">,
  question: Pick<RunEvent, "turnId" | "executionId" | "requestId" | "questionGroupId" | "questionIndex" | "questionCount">, items: TimelineItem[]) {
  if (question.questionGroupId && question.questionIndex && question.questionCount)
    return { current: question.questionIndex, total: question.questionCount };
  const belongs = (turnId?: string, executionId?: string, webRunId?: string) => question.turnId
    ? turnId === question.turnId
    : question.executionId ? executionId === question.executionId : webRunId === run.runId;
  const ids = new Set<string>();
  // The timeline is restored from durable events and is not limited to the
  // small live-event buffer. Resolving a question must not shrink the total.
  for (const item of items) {
    if (item.type === "progress" && item.questionId && !item.questionGroupId
      && belongs(item.questionTurnId, item.executionId, item.questionRunId)) ids.add(item.questionId);
  }
  for (const pending of run.pendingQuestions || []) {
    if (pending.requestId && pending.question && !pending.questionGroupId
      && belongs(pending.turnId, pending.executionId, pending.webRunId || run.runId)) ids.add(pending.requestId);
  }
  if (question.requestId) ids.add(question.requestId);
  return { current: Math.max(1, [...ids].indexOf(question.requestId || "") + 1), total: Math.max(1, ids.size) };
}

/** Attach durable diagnostics only to the same plan, node state and attempt. */
export function executionWithTrace(execution: ExecutionSnapshot, trace: RuntimeTraceView | null): ExecutionSnapshot {
  if (!trace || ![trace.runId, trace.webRunId].includes(execution.executionId) || trace.plan.planVersion !== execution.planVersion) return execution;
  return { ...execution,
    agentTasks: execution.agentTasks || trace.agentCollaboration?.agents,
    agentWaits: execution.agentWaits || trace.agentCollaboration?.waits,
    nodes: execution.nodes.map(node => {
    const saved = trace.plan.nodes.find(item => item.nodeId === node.nodeId && item.state === node.state && item.attemptCount === node.attemptCount);
    return saved ? { ...node, failure: saved.result?.failure || node.failure,
      reviewPolicy: saved.reviewPolicy || node.reviewPolicy, reviewStatus: saved.reviewStatus || node.reviewStatus } : node;
  }) };
}

export function executionForRun(run: RunInfo | null, trace: RuntimeTraceView | null): ExecutionSnapshot | null {
  if (run?.execution) {
    const current = executionWithTrace(run.execution, trace);
    const snapshot = run.status === "running" && run.pendingQuestions?.length
      ? { ...current, phase: "waiting_input", detail: "回答问题后，任务会从当前进度继续。" } : current;
    if (!trace || trace.runId !== snapshot.executionId) return snapshot;
    return { ...snapshot,
      collaboration: snapshot.collaboration || trace.profile.collaboration,
      agentTeam: snapshot.agentTeam || trace.agentCollaboration?.team || undefined,
      agentTasks: snapshot.agentTasks || trace.agentCollaboration?.agents,
      agentWaits: snapshot.agentWaits || trace.agentCollaboration?.waits,
    };
  }
  if (!trace || (run && !["completed", "failed", "stopped"].includes(run.status))) return null;
  const profile = trace.profile.requestedMode;
  if (!(profile in profileLabels)) return null;
  return {
    executionId: trace.runId, planVersion: trace.plan.planVersion, profile: profile as ExecutionProfile,
    collaboration: trace.profile.collaboration,
    agentTeam: trace.agentCollaboration?.team || undefined,
    agentTasks: trace.agentCollaboration?.agents,
    agentWaits: trace.agentCollaboration?.waits,
    agentMessages: trace.agentCollaboration?.messages.map(message => ({
      message_id: message.messageId, sender_id: message.senderId, recipient_id: message.recipientId,
      kind: message.kind, state: message.state, sequence: message.sequence, reply_to: message.replyTo,
    })),
    maxActiveWorkers: trace.profile.maxActiveWorkers,
    route: trace.profile.selectedRoute, phase: trace.delivery.completed ? "completed" : "failed",
    routeReasons: trace.topology.reasons,
    nodes: trace.plan.nodes.map((node) => ({ ...node, summary: node.result?.outputSummary || "", failure: node.result?.failure })),
  };
}

export function progressView(status: RunStatus, execution: ExecutionSnapshot | null, items: TimelineItem[], reconnecting: boolean,
  waitingModels: string[] = [], pendingQuestions: RunEvent[] = []) {
  const active = ["queued", "running", "stopping"].includes(status);
  const approval = active ? items.find((item) => item.type === "approval" && !item.resolved) : undefined;
  const needsInput = active && pendingQuestions.length > 0
    || execution?.phase === "waiting_input" || execution?.nodes.some((node) => node.state === "WAITING_INPUT");
  const needsApproval = execution?.phase === "waiting_approval" || execution?.nodes.some((node) => node.state === "WAITING_APPROVAL");
  const balanceStopped = status === "failed" && execution?.nodes.some(node => node.failure?.kind === "ProviderBalanceUnavailable");
  const phase = execution?.phase || "preparing";
  let label = ({ preparing: "准备任务", exploring: "并行探索", consulting: "咨询辅助 Agent", executing: "执行任务", synthesizing: "汇总发现",
    verifying: "核验结果", integrating: "合入已验收的候选", delivering: "整理交付", retrying: "调整后重试", repairing: "根据验证结果返修", waiting_input: "需要你的补充", waiting_approval: "等待你的确认" } as Record<string, string>)[phase] || "等待执行进展";
  if (status === "completed") label = "任务已完成";
  else if (status === "stopped") label = "任务已停止";
  else if (status === "failed") label = balanceStopped ? "余额不足，任务已暂停" : needsInput ? "需要你的补充" : "任务未完成";
  else if (status === "stopping") label = "正在停止";
  else if (approval || needsApproval) label = "等待你的确认";
  else if (reconnecting) label = "正在恢复连接";
  else if (needsInput) label = "需要你的补充";
  else if (status === "queued") label = "排队中";
  else if (!execution && status === "running") label = "正在执行";
  const tool = active ? items.slice().reverse().find((item) => item.type === "tool" && item.status === "running") : undefined;
  const detail = approval || needsApproval ? "处理确认后，相关任务才能继续"
    : balanceStopped ? "已有进度保留，补充余额后发送继续，可核对并续接任务"
    : active && reconnecting ? "后台任务可能仍在继续，正在同步最新状态"
    : needsInput ? "补充所需信息后，相关任务才能继续"
    : tool?.type === "tool" ? `正在调用 ${tool.toolName}${waitingModels.length ? ` · ${waitingModels.length} 项等待模型响应` : ""}`
    : active && waitingModels.length ? `等待模型响应${waitingModels.length > 1 ? ` · ${waitingModels.length} 项` : ""}`
    : active && execution?.detail ? execution.detail
    : active && phase === "repairing" ? `第 ${execution?.repairCount || 1} 轮返修${execution?.repairLimit ? ` / 上限 ${execution.repairLimit} 轮` : ""}，完成后重新验收`
    : active && phase === "integrating" ? "候选已通过验收，正在检查冲突并写入原工作区"
    : active ? "有新进展时会自动更新" : "";
  return { active, label, detail, approval, needsInput, needsApproval };
}

export function confirmationView(execution: ExecutionSnapshot | null | undefined, items: TimelineItem[], status: RunStatus) {
  if (status !== "running" || items.some((item) => item.type === "approval" && !item.resolved)
    || execution?.nodes.some((node) => ["WAITING_INPUT", "WAITING_APPROVAL"].includes(node.state))) return null;
  const answer = [...items].reverse().find((item) => item.type === "assistant" && item.phase === "final_answer"
    && agentOwner(item, execution).kind !== "child");
  if (answer?.type !== "assistant" || answer.streaming || answer.deliveryState === "rejected") return null;
  if (answer.deliveryState === "accepted") return { label: "答复已确认，正在保存记录", startedAt: execution?.phaseStartedAt };
  if (answer.deliveryState !== "pending") return null;
  const phase = execution?.phase;
  if (phase && !["verifying", "integrating", "delivering"].includes(phase)) return null;
  const detail = execution?.detail || (phase === "integrating" ? "正在合入已检查的改动"
    : phase === "delivering" ? "正在保存任务记录" : "正在确认运行状态与检查结果");
  return { label: `答复已生成，${detail}`, startedAt: execution?.phaseStartedAt };
}

export function phaseSteps(execution: ExecutionSnapshot): Array<{ id: string; label: string }> {
  if (execution.route === "primary-led") return [
    { id: "executing", label: "主 Agent 处理" },
    ...(execution.nodes.some((node) => ["explorer", "planner", "reviewer"].includes(node.role))
      ? [{ id: "consulting", label: "按需咨询" }] : []),
    { id: "verifying", label: execution.nodes.some((node) => node.role === "verifier") ? "独立核验" : "结果检查" },
    ...(execution.repairCount ? [{ id: "repairing", label: "验证返修" }] : []),
    ...(execution.workspaceDelivery === "candidate-first" ? [{ id: "integrating", label: "受控合入" }] : []),
    { id: "delivering", label: "交付" },
  ];
  if (execution.route === "ultra-read-fanout") return [
    { id: "preparing", label: "拆解任务" }, { id: "exploring", label: "并行探索" },
    { id: "executing", label: execution.nodes.some((n) => n.role === "writer") ? "汇总与修改" : "汇总发现" },
    { id: "verifying", label: "核验结果" }, { id: "delivering", label: "交付" },
  ];
  return [
    ...(execution.profile === "standard" ? [] : [{ id: "preparing", label: "分析任务" }]),
    { id: "executing", label: "处理任务" },
    { id: "verifying", label: execution.nodes.some((n) => n.role === "verifier") ? "独立核验" : "结果检查" },
    { id: "delivering", label: "交付" },
  ];
}

export function readableElapsed(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return seconds < 60 ? `${seconds} 秒` : seconds < 3600 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`;
}

export function routeExplanation(execution: ExecutionSnapshot): string {
  if (execution.agentTeam) return `${profileLabels[execution.profile]} · Agent Teams 已启用；主 Agent 负责统筹，实际分派情况见本轮 Agent 分工。`;
  if (execution.route === "primary-led" && execution.consultationMode === "durable-batch") {
    const capacity = execution.maxActiveWorkers;
    const scheduling = capacity === 1 ? "串行执行" : capacity && capacity > 1 ? `最多 ${capacity} 个同时执行` : "按并发上限执行";
    return `${profileLabels[execution.profile]} · 主 Agent 主导；咨询时暂停并保存进度，辅助任务${scheduling}，结果齐备后恢复。`;
  }
  if (execution.route === "primary-led") return `${profileLabels[execution.profile]} · 主 Agent 持续负责，辅助任务按需串行咨询。`;
  if (execution.profile !== "ultra" || execution.route !== "deep-single") return "";
  const reasons = execution.routeReasons.join(" ");
  const cause = reasons.includes("at least two independent subproblems") ? "没有足够独立的子任务"
    : reasons.includes("agent/token budget permits fan-out") ? "当前预算不足以支持并行任务"
    : reasons.includes("runtimeMaxActiveWorkers >= 2") ? "当前设置只允许一个任务同时执行"
    : reasons.includes("shared context dependency is low") ? "各部分依赖同一份上下文"
    : reasons.includes("no unisolated parallel writes") ? "并行修改可能产生冲突"
    : reasons.includes("results are independently verifiable") ? "拆分结果无法独立核验"
    : reasons.includes("gain exceeds coordination cost") || reasons.includes("parallel work has material gain") ? "预计并行收益不足以抵消协调成本"
    : "本轮采用单个主任务连续处理";
  return `Ultra · ${cause}，采用连续处理。`.replace("本轮采用单个主任务连续处理，采用连续处理", "本轮采用单个主任务连续处理");
}
