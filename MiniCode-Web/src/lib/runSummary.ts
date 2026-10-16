import type { RunInfo, RunStatus, RuntimeTraceView, SessionInspect, TimelineItem } from "../types";
import { traceForRun } from "./runtime";
import { diagnoseRunFailure, type RunFailure } from "./runFailure";
import { isAgentCommunicationTool } from "./agentMessages";
import { isRuntimeStateCheck } from "./verification";
import { imageProducerKey } from "./runImages";

export type ToolTimelineItem = Extract<TimelineItem, { type: "tool" }>;
export type StreamEntry = TimelineItem | { type: "tool-group"; id: string; tools: ToolTimelineItem[] };

export function groupToolCalls(items: TimelineItem[], separate: ReadonlySet<string> = new Set()): StreamEntry[] {
  const imageProducers = new Set(items.filter(item => item.type === "images").map(imageProducerKey).filter(Boolean));
  const result: StreamEntry[] = [];
  let tools: ToolTimelineItem[] = [];
  const flush = () => {
    if (tools.length > 1) result.push({ type: "tool-group", id: `group-${tools[0].id}`, tools });
    else if (tools.length) result.push(tools[0]);
    tools = [];
  };
  for (const item of items) {
    if (item.type === "tool" && item.status === "success" && !item.toolRecovery && !isAgentCommunicationTool(item)
      && !["wait_agents", "wait_dependencies"].includes(item.toolName)
      && !separate.has(item.id) && !imageProducers.has(imageProducerKey(item))) {
      if (tools.length && (tools[0].executionId !== item.executionId || tools[0].nodeId !== item.nodeId || tools[0].attemptId !== item.attemptId)) flush();
      tools.push(item);
    }
    else { flush(); result.push(item); }
  }
  flush();
  return result;
}

export interface RunSummary {
  status: "completed" | "failed" | "stopped";
  delivery: "passed" | "failed" | "unknown";
  changes: string[];
  records: RuntimeTraceView["delivery"]["verificationRecords"];
  reasons: string[];
  hasTrace: boolean;
  elapsedSeconds: number | null;
  runId: string | null;
  failure: RunFailure | null;
  reviewCloseout?: string;
  hasAcceptanceRequirements?: boolean;
  hasResourceChanges?: boolean;
}

export function verificationSummary(records: RunSummary["records"]): string {
  const final = records.filter(record => record.scope === "final" && !isRuntimeStateCheck(record));
  const available = final.filter(record => record.projectCheck?.execution !== "not-started");
  const passed = available.filter(record => record.passed);
  const checks = passed.filter(record => record.deterministic);
  const reviews = passed.filter(record => !record.deterministic);
  const failedChecks = available.filter(record => !record.passed && record.deterministic);
  const failedReviews = available.filter(record => !record.passed && !record.deterministic);
  const notStarted = final.length - available.length;
  const labels = [
    checks.length ? `${checks.length} 项检查通过` : "",
    reviews.length ? `${reviews.length} 项 AI 复核通过` : "",
    failedChecks.length ? `${failedChecks.length} 项检查未通过` : "",
    failedReviews.length ? `${failedReviews.length} 项 AI 复核发现问题` : "",
    notStarted ? `${notStarted} 项检查未开始` : "",
  ].filter(Boolean);
  if (labels.length) return labels.join(" · ");
  return records.some((record) => record.verifier === "integrated-runtime-state" && record.passed)
    ? "运行状态已确认" : "未记录通过的检查";
}

export function buildRunSummary(status: RunStatus, run: RunInfo | null, inspect: Pick<SessionInspect, "runtimeRuns"> | null, diagnostics: string[] = []): RunSummary | null {
  if (status !== "completed" && status !== "failed" && status !== "stopped") return null;
  const trace = traceForRun(inspect?.runtimeRuns || [], run?.runId);
  const records = trace?.delivery.verificationRecords.filter((record) => record.scope === "final") || [];
  const taskChecks = records.filter(record => !isRuntimeStateCheck(record) && record.projectCheck?.execution !== "not-started");
  const changes = [...new Set(trace?.plan.nodes.flatMap(node => node.result?.changedResources || []).filter(Boolean) || [])];
  const elapsed = run ? (run.updatedAt - run.createdAt) / 1000 : null;
  if (status === "failed" && (run?.reviewPending || trace?.reviewPending)) diagnostics = [...diagnostics, "Review recovery pending: unresolved review findings"];
  const failure = status === "stopped" || status === "failed" || trace && !trace.delivery.completed
    ? diagnoseRunFailure(trace, run?.execution || null, diagnostics, status === "stopped") : null;
  return {
    status,
    delivery: trace
      ? !trace.delivery.completed || records.some((record) => !record.passed) ? "failed"
        : taskChecks.length ? "passed" : "unknown"
      : "unknown",
    // Workspace Git status can contain unrelated pre-existing edits. Only use this run's resource records.
    changes,
    hasResourceChanges: changes.length > 0,
    records,
    reasons: failure?.rawReasons || [...new Set(trace ? [...trace.delivery.reasons, ...trace.scheduler.reasons] : [])],
    runId: run?.runId || trace?.webRunId || trace?.runId || null,
    failure,
    reviewCloseout: status === "failed" ? run?.reviewCloseout || trace?.reviewCloseout : undefined,
    hasTrace: Boolean(trace),
    hasAcceptanceRequirements: Boolean(trace && (trace.acceptanceCriteria?.length || trace.expectedOutputs?.length || trace.taskFile)),
    elapsedSeconds: elapsed !== null && Number.isFinite(elapsed) && elapsed >= 0 ? elapsed : null,
  };
}
