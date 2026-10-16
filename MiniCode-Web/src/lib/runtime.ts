import type { RunStatus, RuntimeTraceView, RuntimeUsageView, VerificationView } from "../types";

export function formatStopReason(reason?: string): string {
  return ({
    await_user: "等待你的回答",
    budget_exhausted: "预算不足或已到截止时间，执行已停止",
    consultation_budget_limited: "咨询额度不足，收尾预算仍受保护；必要工作不能跳过",
    canceled: "用户已停止执行",
    cancelled: "用户已停止执行",
    blocked: "执行受阻，需要处理原因后重试",
    provider_error: "模型请求失败，执行已暂停",
    tool_timeout: "工具执行超时",
    max_steps: "已达到最大执行步数",
    done: "执行结束，交付结果以最终验收为准",
    completed: "执行结束，交付结果以最终验收为准",
    builder_submitted: "子任务候选已提交，等待补丁合并与最终验收",
    candidate_submitted: "主任务候选已提交，等待最终验收与交付",
    consultation_submitted: "咨询报告已提交，交付结果以最终验收为准",
    verification_failed: "验收未通过",
    empty_response: "模型没有返回可用内容",
  } as Record<string, string>)[reason || ""] || reason || "已满足停止条件";
}

export function formatCount(value?: number | null): string {
  return value == null || !Number.isFinite(value) ? "未知" : value.toLocaleString("zh-CN");
}

export function formatCost(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "未知";
  if (value > 0 && value < 0.000001) return "< $0.000001";
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 6 })}`;
}

export function formatCacheRate(value?: number | null): string {
  return value == null || !Number.isFinite(value) || value < 0 || value > 1
    ? "未知" : `${(value * 100).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}%`;
}

export function formatDuration(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "未知";
  if (value > 0 && value < 0.01) return "< 0.01 s";
  return `${value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })} s`;
}

export function usageTokens(usage?: RuntimeUsageView | null): string {
  if (!usage) return "未知";
  return `${formatCount(usage.tokens)}${!usage.tokensKnown && usage.tokens != null ? "（已统计）" : ""}`;
}

export function usageCost(usage?: RuntimeUsageView | null): string {
  return usage?.costKnown ? formatCost(usage.cost) : "未知";
}

export function budgetRemaining(limit?: number | null, used?: number | null, reserved?: number | null): number | null {
  if (limit == null || used == null || reserved == null) return null;
  return Math.max(0, limit - used - reserved);
}

export function closureReserveLabel(tokens: number | null, cost: number | null): string {
  const parts = [];
  if (tokens != null) parts.push(`${formatCount(tokens)} Token`);
  if (cost != null) parts.push(formatCost(cost));
  return parts.join(" / ") || "未设置上限";
}

export function criterionCoverage(id: string, records: VerificationView[]) {
  // Match CompletionGate: only passed final-scope evidence can cover a criterion.
  const matches = records.filter((record) => record.scope === "final" && record.coveredCriteriaIds?.includes(id));
  return {
    passed: matches.some((record) => record.passed),
    verifiers: matches.filter((record) => record.passed).map((record) => record.verifier),
  };
}

export function traceForRun(traces: RuntimeTraceView[], runId?: string | null): RuntimeTraceView | null {
  if (!runId) return traces.at(-1) || null;
  return traces.slice().reverse().find((trace) => trace.webRunId === runId || trace.runId === runId) || null;
}

export function runtimeStatus(status: RunStatus, trace: RuntimeTraceView | null): RunStatus {
  if (["queued", "running", "stopping"].includes(status) || !trace) return status;
  if (status === "stopped" || trace.scheduler.canceled) return "stopped";
  return trace.delivery.completed ? "completed" : "failed";
}
