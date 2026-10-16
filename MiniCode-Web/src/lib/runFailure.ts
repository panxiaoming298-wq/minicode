import type { ExecutionSnapshot, RuntimeTraceView, TimelineItem } from "../types";
import { traceForRun } from "./runtime";
import { providerFailure } from "./stopReason";
import { historicalAttemptItemIds } from "./conversation";
import { invalidVerifierReport, isRuntimeStateCheck } from "./verification";

export interface FailureExplanation {
  kind: "provider" | "runtime" | "parameters" | "budget" | "context" | "steps" | "verification" | "canceled" | "unknown";
  title: string;
  message: string;
  action: string;
}

export interface RunFailure extends FailureExplanation {
  runId: string | null;
  consequence: string;
  verificationPending: boolean;
  reviewPending?: boolean;
  rawReasons: string[];
}

/** Classify only recorded failures, never prose or isolated keywords in tool output. */
export function explainFailure(reason: string): FailureExplanation | null {
  const text = reason.trim();
  if (text.startsWith("{")) {
    try {
      const receipt = JSON.parse(text);
      if (receipt.error === "Tool unavailable in the current scope; no operation was executed."
        && Array.isArray(receipt.unavailable_names) && receipt.unavailable_names.length > 0) {
        const skill = receipt.unavailable_names.length === 1 && receipt.unavailable_names[0] === "load_skill"
          && receipt.unavailable_reasons?.load_skill === "effect_restricted";
        return { kind: "runtime", title: skill ? "工作流恢复被工具范围限制拦截" : "工具在当前执行范围内不可用",
          message: skill ? "恢复任务时再次加载 Skill 被拦截，尚未执行任何操作；已有工作和复核意见仍保留。"
            : "本轮请求的工具不在允许的执行范围内，相关操作没有执行。",
          action: skill ? "修正工作流恢复逻辑后，从已有进度继续处理复核意见。" : "查看具体工具与限制，采用当前允许的工具后继续。" };
      }
    } catch { /* Ordinary prose and malformed objects remain unclassified. */ }
  }
  if (text.startsWith("Review recovery pending:")) {
    const exhausted = /budget exhausted|no workspace progress|repeated a previous candidate/.test(text);
    return { kind: "verification", title: "复核仍有待处理项",
      message: exhausted ? "本轮修复与再次复核已达到自动处理上限，仍有问题未解决。"
        : "复核还缺少必要条件，当前尚不能确认任务完成。",
      action: "已完成的修改和复核意见均已保留。查看待处理项，补齐条件后可从当前进度继续。" };
  }
  if (text.startsWith("Verification evidence is incomplete.")) {
    let notConfigured = false;
    try {
      const receipt = JSON.parse(text.slice(text.indexOf("{"))).receipt;
      notConfigured = receipt?.status === "not_configured" && Array.isArray(receipt.checks) && receipt.checks.length === 0;
    } catch { /* Older or truncated records still identify an evidence gap. */ }
    return { kind: "verification", title: notConfigured ? "尚未配置验收检查" : "验收证据不完整",
      message: notConfigured ? "独立复核没有取得可执行的验收检查，本轮尚未完成。" : "独立复核缺少有效的检查结果，本轮尚不能确认完成。",
      action: "补齐验收检查及其执行结果后继续任务；已完成的代码和原始错误记录仍会保留。" };
  }
  const provider = providerFailure(text);
  if (provider) return { kind: "provider", ...provider, action: "" };
  if (/^Model API timeout:/.test(text)) {
    return { kind: "provider", title: "模型服务响应超时", message: "本轮模型请求未在等待时间内返回。", action: "检查连接后重试；是否产生用量以服务商返回的记录为准。" };
  }
  if (/^Network error \(connection failed or dropped\):/.test(text)) {
    return { kind: "provider", title: "模型服务连接失败或中断", message: "模型请求未能完成，当前连接已断开。", action: "检查服务地址与网络后重试；未确认的调用用量需要先核对。" };
  }
  if (/^Context budget exceeded before the model call\b/.test(text)) {
    return { kind: "context", title: "单次上下文容量不足", message: "当前请求超过可用上下文容量，尚未发送给模型。", action: "缩小单次读取范围或整理上下文，也可核对配置的上下文上限；增加任务总预算不会扩大模型容量。" };
  }
  if (/^Runtime model usage requires reconciliation:/.test(text)) {
    return usageReconciliationFailure;
  }
  if (/^(?:The provider response is not provably complete\b|Model response transport was incomplete\.)/.test(text)) {
    return { kind: "provider", title: "模型回复未完整接收", message: "回复的完整性未获确认，本轮未接受它作为已完成结果。", action: "检查模型服务与连接，从已保存的进度继续；操作结果不明时先核对，避免重复执行。" };
  }
  if (/^The primary ended without submit_candidate;/.test(text)) {
    return { kind: "runtime", title: "主任务尚未提交候选", message: "主任务结束时没有提交可供验收的候选，因此未获准交付。", action: "从保留的进度继续，完成候选提交与验收后再交付。" };
  }
  if (/^Worker failed with an ambiguous non-safe tool call\b/.test(text)) {
    return uncertainOperationFailure;
  }
  if (/^Model output was truncated \(stop_reason=(?:length|max_tokens)\)\./.test(text)) {
    return { kind: "provider", title: "模型单次回复被截断", message: "模型输出被截断，经过恢复尝试仍未完成本轮工作；已保存的改动保留。", action: "若手动上限较小，可改用自动模式或提高上限，再基于已有改动继续。任务总预算仍需充足；本轮不限额度也不会取消模型的输出和上下文限制。" };
  }
  if (/^(?:Model API error\b|Provider availability failure\b)/i.test(text)) {
    if (/context.window|context.length|context.*capacity|maximum context/i.test(text)) {
      return { kind: "context", title: "模型上下文容量不足", message: "当前请求包含的内容超过模型可接收的范围。", action: "整理上下文或缩小单次读取范围后继续；增加任务总预算不会扩大上下文容量。" };
    }
    if (/\b429\b|rate.?limit|too many requests/i.test(text)) {
      return { kind: "provider", title: "模型服务请求受限", message: "服务商限制了当前请求频率，本轮调用未成功。", action: "稍后重试，或在设置中切换可用的模型服务。" };
    }
    if (/\b401\b|invalid.?api.?key|authentication|unauthorized/i.test(text)) {
      return { kind: "provider", title: "模型服务身份验证失败", message: "服务商未接受当前调用凭据。", action: "在设置中检查当前服务商的地址和密钥，再测试连接。" };
    }
    if (/timed? ?out|timeout/i.test(text)) {
      return { kind: "provider", title: "模型服务响应超时", message: "本轮模型请求未在等待时间内返回。", action: "检查连接后重试；是否产生用量以服务商返回的记录为准。" };
    }
    return { kind: "provider", title: "模型服务调用失败", message: "模型请求未成功，现有记录不足以进一步确定服务商侧原因。", action: "展开原始错误，核对服务商返回的信息后重试。" };
  }
  if (/^Worker failed \([\w.]+\):/.test(text)) {
    return { kind: "runtime", title: "MiniCode 内部运行异常", message: "执行器遇到异常，本轮任务已中断。", action: "查看已保存的改动后可重试；若再次出现，请保留运行编号和原始错误以便排查。" };
  }
  if (/^(?:Local workspace error|Builder candidate preparation failed) \([\w.]+\):/.test(text)
    || /^Required Builder workspace preparation failed;/.test(text)) {
    return { kind: "runtime", title: "MiniCode 工作区准备或访问失败", message: "本地工作区操作未完成，后续执行或交付因此受阻。", action: "查看原始工作区错误后重试；子任务尚未生成补丁时，无需尝试接受或恢复补丁。" };
  }
  if (/^Input validation error in [\w.:-]+:/.test(text)) {
    return { kind: "parameters", title: "模型调用工具的参数不符合要求", message: "模型提交的工具参数未通过校验。", action: "继续任务时需要按工具要求修正参数；具体字段可在原始错误中查看。" };
  }
  if (invalidVerifierReport(text)) {
    return { kind: "parameters", title: "核验报告格式不符合要求", message: "复核报告格式不完整，尚无有效的核验结论。", action: "从已有进度继续，补全复核报告；无需仅因报告格式错误修改代码。" };
  }
  if (/^dependency wait \S+ blocked: timeout$/.test(text)) {
    return { kind: "runtime", title: "等待子任务超时", message: "主任务等待依赖结果超过了单独设置的等待时限，后续执行因此停止。", action: "保留已生成的文件后继续；本轮不限额时，也需检查依赖等待是否设置了单独超时。" };
  }
  if (/^Runtime model budget stopped execution: run canceled during model request$/i.test(text)) {
    return { kind: "canceled", title: "模型请求期间任务被取消", message: "运行在模型请求期间被取消；这条记录本身不能说明最初停止原因。", action: "查看主任务的失败记录，处理最初原因后从已有进度继续。" };
  }
  if (/^Runtime model budget stopped execution: context window budget exceeded\b/i.test(text)) {
    return { kind: "context", title: "单次上下文容量不足", message: "请求内容与预留输出无法同时容纳在当前上下文窗口中，执行已停止。", action: "核对本地上下文上限与单次输出上限，或整理上下文后继续；本轮不限额度不会取消单次上下文限制。" };
  }
  if (/^(?:Runtime model budget stopped execution:|model request cannot fit remaining|cannot reserve .*request|deadline exhausted)/i.test(text)) {
    return /\bdeadline\b|\btime[- ]+budget\b/i.test(text)
      ? { kind: "budget", title: "本轮任务已到截止时间", message: "任务达到设置的时间限制，尚未完成。", action: "查看已保存的进度，调整本轮时限后继续。" }
      : { kind: "budget", title: "本轮任务预算不足", message: "剩余任务预算不足以执行下一步或完成收尾检查。", action: "查看用量与预留记录，调整本轮任务预算后继续。" };
  }
  if (/^Reached the maximum tool step limit for this turn\./.test(text)) {
    return { kind: "steps", title: "本轮任务达到执行步数上限", message: "执行在步数上限处停止，任务尚未完成。", action: "保留已完成的工作，检查本轮限制后继续任务。" };
  }
  return null;
}

const usageReconciliationFailure: FailureExplanation = {
  kind: "runtime", title: "模型用量尚未核对", message: "先前模型请求的用量尚未确认，后续调用已暂停。",
  action: "查看用量记录并完成核对后继续；不要把未知用量当作零消耗。",
};
const uncertainOperationFailure: FailureExplanation = {
  kind: "runtime", title: "工具操作结果尚未确认", message: "工具可能已产生改动，MiniCode 已停止自动重放。",
  action: "先核对文件、命令或外部操作的实际结果，再决定如何继续，避免重复执行。",
};

function explainRecordedFailure(reason: string, failureKind: string): FailureExplanation | null {
  // These kinds carry evidence that prose alone cannot establish.
  if (failureKind === "ModelUsageUnreconciled") return usageReconciliationFailure;
  if (failureKind === "UncertainSideEffect") return uncertainOperationFailure;
  const recorded = explainFailure(reason);
  if (recorded) return recorded;
  const typed: Record<string, FailureExplanation> = {
    ProtocolError: { kind: "runtime", title: "执行协议未满足要求", message: "执行结果缺少有效的协议记录，本轮无法确认完成。", action: "查看原始协议错误，从保留的进度继续；若重复出现，保留运行编号用于排查。" },
    WorkspaceBusy: { kind: "runtime", title: "工作区正在被其他任务使用", message: "当前操作未取得工作区写入权限。", action: "待占用工作区的任务结束后重试，避免多个任务同时修改相同文件。" },
    WorkerTimedOut: { kind: "runtime", title: "执行步骤超时", message: "执行步骤超过了等待时限，本轮尚未完成。", action: "查看该步骤与已保存的进度，处理超时原因后继续。" },
    PolicyDenied: { kind: "runtime", title: "操作未获授权", message: "当前权限策略没有允许这项操作。", action: "查看被拒绝的操作，按任务需要调整授权或采用已获准的操作后继续。" },
    ApprovalExpired: { kind: "runtime", title: "操作审批已过期", message: "先前审批已失效，相关操作未获准继续执行。", action: "核对当前操作内容后重新审批，再继续任务。" },
    BudgetExhausted: { kind: "budget", title: "本轮任务预算不足", message: "任务预算或时限已耗尽，本轮尚未完成。", action: "查看预算与时限记录，调整本轮限制后继续。" },
    ModelOutputTruncated: { kind: "provider", title: "模型单次回复被截断", message: "模型回复被截断，本轮尚未完成。", action: "查看单次输出上限与剩余任务预算，从已保存的进度继续。" },
  };
  return Object.hasOwn(typed, failureKind) ? typed[failureKind] : null;
}

function completionConsequence(reason: string): boolean {
  if (/^dependency wait \S+ blocked: failed_dependency$/.test(reason.trim())) return true;
  return /^(?:Runtime completion gate rejected delivery\b|Required consultation failed; no further primary model calls or edits are permitted in this run\.|workers\/nodes are still active:|terminal node failure\/cancellation remains|not every current Plan node succeeded|one or more final verification records failed|candidate repair stopped: node failures or unfinished work require separate resolution|no runnable nodes remain)/i.test(reason.trim());
}

export function diagnoseRunFailure(
  trace: RuntimeTraceView | null,
  execution: ExecutionSnapshot | null = null,
  fallbackReasons: string[] = [],
  stopped = false,
): RunFailure {
  const nodes = trace ? trace.plan.nodes.map(node => ({ ...node, message: node.result?.failure?.message || "", failureKind: node.result?.failure?.kind || "" }))
    : (execution?.nodes || []).map(node => ({ ...node, message: node.failure?.message || node.summary, failureKind: node.failure?.kind || node.diagnosis?.failureKind || "" }));
  const failures = nodes.filter(node => node.state.startsWith("FAILED")
    && !(node.criticality === "advisory" && node.diagnosis?.acknowledged));
  const failureIds = new Set(failures.map(node => node.nodeId));
  const checks = [
    ...(trace?.delivery.verificationRecords || []),
    ...(trace?.plan.nodes.filter(node => failureIds.has(node.nodeId))
      .flatMap(node => node.result?.verificationRecords || []) || []),
  ].filter(record => !isRuntimeStateCheck(record));
  const failedChecks = checks.filter(record => !record.passed);
  const checkReasons = failedChecks.flatMap(record => record.projectCheck
    ? [record.projectCheck.failure, ...record.projectCheck.browserErrors].filter(Boolean) : [record.details]);
  // Actual node failures precede the completion gate's downstream consequences.
  const rawReasons = [...new Set([
    ...failures.map(node => node.message), ...checkReasons,
    ...(trace?.delivery.reasons || []), ...(trace?.scheduler.reasons || []), ...fallbackReasons,
  ].filter(reason => reason.trim()))];
  const failedIds = new Set(failures.map(node => node.nodeId));
  function hasFailedDependency(id: string, visited = new Set<string>()): boolean {
    if (visited.has(id)) return false;
    visited.add(id);
    return (nodes.find(node => node.nodeId === id)?.dependencies || [])
      .some(dependency => failedIds.has(dependency) || hasFailedDependency(dependency, visited));
  }
  const rootNodes = failures.filter(node => !hasFailedDependency(node.nodeId));
  const rootReasons = [...new Set(rootNodes
    .map(node => node.message).filter(reason => reason.trim() && !completionConsequence(reason)))];
  // Do not replace an unclassified upstream error with a more recognizable downstream one.
  const causes = rootReasons.length ? rootReasons : rawReasons.filter(reason => !completionConsequence(reason));
  const consultationLimit = causes.length === 1 && rootNodes.some(node =>
    node.message === causes[0] && node.failureKind === "ConsultationBudgetLimited");
  const causeNodes = rootNodes.filter(node => node.message.trim() && !completionConsequence(node.message));
  const causeKinds = [...new Set(causeNodes.filter(node => node.message === causes[0]).map(node => node.failureKind).filter(Boolean))];
  const toolTimeouts = causeNodes.length > 0 && causeNodes.every(node => node.failureKind === "ToolTimeout");
  const recoveryPending = rawReasons.find(reason => reason.startsWith("Review recovery pending:"));
  const known: FailureExplanation | null = recoveryPending ? explainFailure(recoveryPending) : toolTimeouts
    ? { kind: "runtime", title: "工具执行超时", message: "一个或多个执行步骤的工具调用超过时限，相关任务已停止。", action: "查看超时调用；代码搜索可缩小目录或指定文件，处理后从已有进度继续。" }
    : consultationLimit
    ? { kind: "budget", title: "辅助任务触及内部咨询额度", message: "辅助任务的模型请求被 MiniCode 咨询预算拦截，后续执行因此停止。", action: "查看咨询额度与任务预算记录；如果本轮已选不限额，应检查预算是否正确传递给辅助任务。" }
    : causes.length === 1 ? explainRecordedFailure(causes[0], causeKinds.length === 1 ? causeKinds[0] : "") : null;
  const project = failedChecks.find(record => record.projectCheck)?.projectCheck;
  const failedStage = project?.stages.find(stage => stage.exitCode !== 0)?.stage;
  const projectFailure: FailureExplanation | null = project?.execution === "not-started"
    ? { kind: "runtime", title: "项目检查未开始", message: project.failure || "项目检查尚未启动，没有实际检查结论。", action: "处理检查的启动条件后，从已有进度继续验收。" }
    : project ? {
    kind: "verification",
    title: failedStage === "build" ? "项目构建未通过" : failedStage === "dependencies" ? "项目依赖安装未完成"
      : failedStage === "browser" || project.browserErrors.length ? "浏览器检查未通过" : "项目检查未完成",
    message: (project.browserErrors[0] || project.failure || "原生项目检查未通过。").slice(0, 600),
    action: "展开项目检查明细查看命令输出、浏览器错误和截图，处理后再继续。",
  } : null;
  const patchDeliveryBlocked = rootReasons.length === 0 && rawReasons.some(reason =>
    /^(?:Runtime completion gate rejected delivery: )?Builder has no accepted, applied and checked patch: builder-[\w-]+(?:;|$)/.test(reason.trim()));
  // Cancellation is a recorded lifecycle outcome. Do not infer it from quoted
  // prose, a canceled helper, or the completion gate's generic failure summary.
  // A stopped web run also covers cancellation before its first Plan/trace.
  const fallbackFailure = fallbackReasons.some(reason => reason.trim() && !completionConsequence(reason)
    && !/^Runtime was canceled$/i.test(reason.trim()) && explainFailure(reason)?.kind !== "canceled");
  const canceled = failures.length === 0 && failedChecks.every(record => record.projectCheck?.execution === "not-started")
    && !fallbackFailure
    && !causes.some(reason => { const cause = explainFailure(reason); return cause && cause.kind !== "canceled"; })
    && (stopped || trace?.scheduler.canceled || nodes.some(node => node.role === "primary" && node.state === "CANCELED"));
  const verifiers = nodes.filter(node => node.role === "verifier");
  const reviewPending = (canceled || failures.some(node => node.role !== "verifier"))
    && verifiers.length > 0 && verifiers.every(node => (node.state === "PENDING" || node.state === "CANCELED") && node.attemptCount === 0);
  const verificationPending = canceled ? checks.every(record => record.projectCheck?.execution === "not-started")
    : reviewPending && checks.length === 0;
  const reviewFinding = causeNodes.length === 1 && ["verifier", "reviewer"].includes(causeNodes[0].role) && causeNodes[0].failureKind === "VerificationFailed";
  const explanation: FailureExplanation = canceled
    ? { kind: "canceled", title: "任务已停止", message: "任务尚未完成，可从已有进度继续。", action: "" }
    : known || (rootReasons.length > 1
    ? { kind: "unknown", title: "多个执行步骤失败", message: "多个执行步骤分别报告了错误，当前记录不足以归为同一个原因。", action: "查看原始停止原因和运行详情，逐项处理后继续。" }
    : reviewFinding
    ? { kind: "verification", title: "独立复核发现问题", message: causeNodes[0].message || "核验 Agent 报告了未满足的要求。", action: "查看复核报告的具体问题与依据，修复后从已有进度继续验收。" }
    : failedChecks.length && (rootReasons.length === 0 || rootNodes.every(node => node.role === "verifier")
      || rootReasons.every(reason => /registered check|project check|verification/i.test(reason)))
    ? projectFailure || { kind: "verification", title: "本轮最终检查未通过", message: "已执行的验收检查发现未满足的要求。", action: "查看验证明细，修复未通过的检查项后继续。" }
    : patchDeliveryBlocked
    ? { kind: "verification", title: "子任务补丁尚未通过交付检查", message: "子任务执行结束，但补丁的接受、应用或验证记录尚未满足交付要求。", action: "查看运行详情中的补丁状态与检查记录，补齐缺失环节后继续。" }
    : { kind: "unknown", title: "任务未完成，原因记录不完整", message: "现有记录不足以确认最初的失败原因。", action: "展开原始记录核对；若继续后再次失败，请保留新的运行编号和错误。" });
  return {
    ...explanation,
    runId: trace?.webRunId || trace?.runId || execution?.executionId || null,
    consequence: canceled ? "" : recoveryPending ? "验收仍待完成；具体复核意见及各次检查记录如下。" : reviewPending ? "本轮在独立复核开始前已停止，尚无复核结论。"
      : failures.length ? "执行步骤失败，本轮未通过交付检查。" : "",
    verificationPending,
    reviewPending,
    rawReasons,
  };
}

/** Never borrow the latest trace for an older or unidentifiable conversation round. */
export function failureForRound(items: TimelineItem[], traces: RuntimeTraceView[], execution: ExecutionSnapshot | null = null, stopped = false): RunFailure {
  const ids = [...new Set(items.flatMap(item => item.executionId ? [item.executionId] : []))];
  const id = ids.length === 1 ? ids[0] : null;
  const matchedExecution = execution && (ids.length === 0 || id === execution.executionId) ? execution : null;
  const traceId = id || matchedExecution?.executionId;
  const trace = traceId ? traceForRun(traces, traceId) : null;
  const historical = historicalAttemptItemIds(items, matchedExecution);
  const reasons = items.flatMap(item => item.type === "error" && !historical.has(item.id) ? [item.content] : []);
  if (trace?.reviewPending) reasons.push("Review recovery pending: unresolved review findings");
  if (!reasons.length && items.some(item => item.type === "assistant" && item.messageId?.startsWith("review-closeout:"))) {
    reasons.push("Review recovery pending: unresolved review findings");
  }
  return diagnoseRunFailure(trace, matchedExecution, reasons, stopped);
}
