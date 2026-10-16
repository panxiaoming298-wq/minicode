import type { ExecutionNode, VerificationView } from "../types";

export interface ReviewStatus {
  state: "waiting" | "not-started" | "running" | "passed" | "findings" | "interrupted" | "invalid" | "unconfirmed" | "unavailable";
  label: string;
  description: string;
  tone: "neutral" | "pending" | "success" | "danger";
}

/** Completion metadata verifies runtime state, not the task's code or content. */
export function isRuntimeStateCheck(record: Pick<VerificationView, "verifier">): boolean {
  return record.verifier === "integrated-runtime-state" || record.verifier === "human-steering";
}

/** Older runtimes classified an invalid report as VerificationFailed. Keep that distinct from a finding. */
export function invalidVerifierReport(message: string): boolean {
  return /^Verifier (?:output must contain exactly one terminal decision marker|decision marker must be the exact final non-empty line|decision summary must preserve the full report's terminal decision)$/.test(message.trim());
}

/** Describe the recorded review lifecycle, never infer a verdict from assistant prose. */
export function reviewStatus(node: ExecutionNode, active: boolean, stopped = false, nodes: ExecutionNode[] = []): ReviewStatus | null {
  if (node.role !== "verifier") return null;
  const state = node.state;
  if (state === "SUCCEEDED") return { state: "passed", label: "复核通过", tone: "success",
    description: "AI 已完成本次复核并给出通过意见。实际检查的范围与证据请查看交付验收。" };
  if (state === "SUPERSEDED") return { state: "unconfirmed", label: "复核已调整", tone: "neutral",
    description: "这项复核已被新的任务安排替代，以当前任务的验收记录为准。" };
  if (node.attemptCount === 0 && ["PENDING", "READY", "CANCELED"].includes(state)) {
    if (active && state !== "CANCELED") return { state: "waiting", label: state === "PENDING" ? "等待前置任务" : "等待复核", tone: "neutral",
      description: "尚未开始复核；前置任务完成后，核验 Agent 会对照原始需求检查成果。" };
    const blocked = node.dependencies.some(id => {
      const dependency = nodes.find(item => item.nodeId === id);
      return dependency && dependency.state !== "SUCCEEDED";
    });
    return { state: "not-started", label: "复核未开始", tone: "neutral",
      description: stopped || state === "CANCELED" ? "此项复核已停止，尚未开始执行，没有复核结论。"
        : blocked ? "前置任务未完成，复核尚未开始。继续任务时需先处理前置错误。"
          : "本轮没有复核启动记录，尚无复核结论。" };
  }
  if (state.startsWith("FAILED")) {
    if (state === "FAILED_TERMINAL" && node.criticality === "advisory" && node.diagnosis?.fallbackAllowed) {
      const acknowledged = node.diagnosis.acknowledged === true;
      return { state: "interrupted", tone: acknowledged ? "neutral" : "pending",
        label: acknowledged ? "参考咨询失败 · 主 Agent 已接管" : "参考咨询失败 · 等待主 Agent 接管",
        description: acknowledged ? "这项参考复核未完成，主 Agent 已接管后续处理。最终核验结果请查看交付验收。"
          : "这项参考复核未完成，等待主 Agent 处理。参考咨询的状态不代表最终核验结论。" };
    }
    const message = node.failure?.message || node.summary;
    const kind = node.failure?.kind || node.diagnosis?.failureKind;
    if (active && state === "FAILED_RETRYABLE") return { state: "running", label: "复核准备重试", tone: "pending",
      description: "上次复核未完成，正在准备重试，尚无最终结论。" };
    if (node.reviewStatus === "unavailable" || kind === "ReviewUnavailable") return { state: "unavailable", label: "独立复核未完成",
      tone: node.reviewStatus === "unavailable" ? "neutral" : "pending",
      description: node.reviewStatus === "unavailable"
        ? "本次可选复核未形成结论。交付仍需满足必要检查，不能据此标记独立复核通过。"
        : "复核所需的证据、执行条件或预算不足，尚无有效结论。必需复核完成前无法交付。" };
    if (invalidVerifierReport(message)) return { state: "invalid", label: "复核报告未被接受", tone: "pending",
      description: "复核报告格式不完整，尚无有效结论。继续任务时应补全报告，无需仅因格式错误修改代码。" };
    if (kind === "VerificationFailed") return { state: "findings", label: "复核发现问题", tone: "pending",
      description: active && nodes.some(item => item.role === "primary" && item.state === "RUNNING")
        ? "复核意见已交回主 Agent 处理，修复后需要再次复核。"
        : "核验 Agent 报告了未满足的要求。请查看具体问题和依据，修复后继续验收。" };
    if (!kind) return { state: "unconfirmed", label: "复核结果未确认", tone: "pending",
      description: "这次复核未完成，但记录没有说明是发现问题还是执行异常。请查看原始报告后继续。" };
    return { state: "interrupted", label: "复核中断", tone: "pending",
      description: kind === "ToolTimeout" ? "复核中的工具调用超时，未形成有效结论。处理超时原因后可继续任务。"
        : "复核执行未完成，尚无有效结论。请查看停止原因后继续任务。" };
  }
  if (state === "CANCELED" || stopped) return { state: "interrupted", label: "复核已停止", tone: "pending",
    description: "复核开始后已停止，尚未确认结果。可从已有进度继续任务。" };
  if (active && ["PENDING", "READY"].includes(state)) return { state: "waiting", label: "等待复核", tone: "neutral",
    description: "正在等待本次复核开始，之前的尝试不代表当前版本已通过。" };
  if (active && ["RUNNING", "VERIFYING", "CLAIMED"].includes(state)) return { state: "running", label: "正在复核", tone: "neutral",
    description: "正在对照原始需求、当前代码和已有检查记录进行只读复核。" };
  if (active && ["WAITING_INPUT", "WAITING_APPROVAL", "WAITING_CHILDREN"].includes(state)) return { state: "waiting", tone: "pending",
    label: state === "WAITING_APPROVAL" ? "复核等待确认" : state === "WAITING_INPUT" ? "复核需要补充" : "复核等待辅助结果",
    description: "复核正在等待所需信息或确认，尚无最终结论。" };
  return { state: "unconfirmed", label: "复核结果未确认", tone: "pending",
    description: "现有记录不足以确认复核是否完成，不能据此判断成果通过或不通过。" };
}

export function verificationLabel(record: VerificationView): string {
  if (!record.deterministic) return "AI 复核";
  if (record.projectCheck) return "项目运行检查";
  return ({ "expected-outputs": "产物文件检查", "integrated-runtime-state": "交付状态检查", "human-steering": "输入处理状态" } as Record<string, string>)[record.verifier]
    || record.verifier.replace(/^(?:test|lint|type|build)-command:/, "");
}

export function recordedEvidence(record: VerificationView) {
  return record.evidence.filter(item => item.evidenceType !== "worker-assertion");
}

/** An AI opinion and a real check receipt are different kinds of support for a criterion. */
export function criterionAssessment(id: string, records: VerificationView[]) {
  const matches = records.filter(record => record.scope === "final" && !isRuntimeStateCheck(record) && record.coveredCriteriaIds?.includes(id));
  const available = matches.filter(record => record.projectCheck?.execution !== "not-started");
  const failed = available.filter(record => !record.passed);
  const checked = available.some(record => record.passed && record.deterministic && recordedEvidence(record).some(item => item.contentHash));
  const reviewed = available.some(record => record.passed && !record.deterministic);
  const state = failed.length ? "failed" : checked ? "passed" : reviewed ? "reviewed" : "unverified";
  return { state, records: matches,
    label: state === "failed" ? (failed.some(record => record.deterministic) ? "检查未通过" : "AI 复核发现问题")
      : state === "passed" ? "检查通过" : state === "reviewed" ? "AI 复核认为满足" : "尚未验证",
    description: state === "failed" ? "请查看未通过的检查及其依据。"
      : state === "passed" ? "已有声明覆盖此项的实际检查证据，具体范围见检查记录。"
        : state === "reviewed" ? "目前仅有 AI 判断，尚无对应的实际检查证据。"
          : "尚无可用的最终检查证据，不能据此判定此项不合格。" };
}
