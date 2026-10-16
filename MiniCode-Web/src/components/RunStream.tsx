import { toolAvailabilityFailure } from "../lib/toolAvailability";
import { commandFailureLabel, commandFailureSummary } from "../lib/commandResult";
import { commandRecoveryPresentation, correctedCommandResults } from "../lib/commandRecovery";
import { CommandOutput } from "./CommandOutput";
import {
  BookOpen,
  Bug,
  Check,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  Copy,
  GitCompareArrows,
  GitFork,
  Hammer,
  Lightbulb,
  LoaderCircle,
  Menu,
  PanelLeft,
  ScanLine,
  Square,
  Telescope,
  TerminalSquare,
} from "lucide-react";
import { createContext, memo, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type { ConversationViewState } from "../lib/conversationCache";
import type { TurnMessagesPage } from "../types";
import { createPortal } from "react-dom";
import { parseMessage } from "../lib/attachments";
import { SkillText } from "./SkillText";
import type { FileReference } from "../lib/fileReferences";
import { AssistantMarkdown, FileNavigationContext } from "./AssistantMarkdown";
import { AttachmentList } from "./ComposerAttachments";

import type { ApprovalChoice, ExecutionProfile, ExecutionSnapshot, RunInfo, RunStatus, TimelineItem, TurnsPage, SessionTurn, SessionSummary, TurnFileChanges, RuntimeTraceView } from "../types";
import { historyToTimeline } from "../lib/timeline";
import { contextRotationPresentation, hasContextRotationCommit } from "../lib/contextRotationRecovery";
import { isContextRotationRequest } from "../lib/composerCommands";
import { agentWaitPresentation } from "../lib/agentWait";
import "./Worktrees.css";
import { groupToolCalls, type RunSummary, type ToolTimelineItem } from "../lib/runSummary";
import { formatDuration, traceForRun } from "../lib/runtime";
import { diagnoseRunFailure, failureForRound } from "../lib/runFailure";
import { FailureNotice } from "./FailureDetails";
import { RunSummaryCard } from "./RunSummaryCard";
import { RunStopLine, StopActions, StopLabel, type RunRecoveryAction } from "./RunStopLine";
import { ProjectCheckCard } from "./ProjectCheckCard";
import { RunImages } from "./RunImages";
import { imageProcessSections, turnMessagesWithImages } from "../lib/runImages";
import { taskChecksForSteps, type StepCheck } from "../lib/taskChecks";
import { TaskCheckSummary, TaskCheckDetails } from "./TaskChecks";
import { projectChecksForRounds, type RoundProjectCheck } from "../lib/projectChecks";
import { RunProgress } from "./RunProgress";
import { RunActivity } from "./RunActivity";
import { AgentBadge } from "./AgentActivity";
import { AgentPanel } from "./AgentPanel";
import { AgentMessageItem } from "./AgentMessageItem";
import { isAgentCommunicationTool } from "../lib/agentMessages";
import { agentConversationKey, agentConversations, agentImplementationLabel, agentTaskObjective, executionForRound } from "../lib/agentPanel";
import { agentOwner, agentProcessSections, ownerLabel, settledAgentState, type AgentProcessEntry } from "../lib/agentActivity";
import { reviewStatus } from "../lib/verification";
import { RunChangesBadge, RunChangesCard, RunChangesReview, type ChangeReviewSource } from "./RunChanges";
import { childRecoveryLabel, advisoryFailureLabel, confirmationView, nodeStateLabels, profileLabels, readableElapsed } from "../lib/execution";
import { activitySummary, deferredToolPresentation, historicalAttemptItemIds, isCanceledTool, isInterruptedTool, latestTurnItems, presentRound, conversationRounds, needsAttention, toolActivity, toolFailureCount, toolFailureLabel, toolIssueSummary, turnElapsedMs, unparsedToolInput, usedSkills, type ConversationRound } from "../lib/conversation";
import { TurnActions } from "./TurnActions";
import { ChatActions } from "./ChatActions";
import { ConversationNavigator, type ConversationAnchor } from "./ConversationNavigator";
import { DevelopmentServices } from "./DevelopmentServices";
import { PageSentinel } from "./PageSentinel";
import { QuestionRecord } from "./QuestionRecord";
import { QuestionSession } from "./QuestionSession";

interface RunStreamProps {
  imageInputStatus?: "supported" | "unsupported" | "unknown";
  restoredView?: ConversationViewState;
  captureViewRef?: { current: (() => ConversationViewState | undefined) | null };
  onViewChange?: (view: ConversationViewState) => void;
  onProcessPage?: (turnId: string, page: TurnMessagesPage, detail?: boolean) => void;
  onLoadProcessPage?: (turn: SessionTurn, after: number) => Promise<void>;
  workspace?: string;
  savedTurnId?: string;
  history?: TurnsPage | null;
  historyError?: string;
  historyLoading?: boolean;
  onLoadEarlier?: () => void;
  onFork?: (turn: SessionTurn) => void;
  session?: SessionSummary;
  onSourceSession?: (id: string) => void;
  items: TimelineItem[];
  runStatus: RunStatus;
  run: RunInfo | null;
  execution: ExecutionSnapshot | null;
  actualProfile?: ExecutionProfile;
  reconnecting: boolean;
  onCompose: () => void;
  onAnswerQuestion?: (requestId: string) => void;
  activeQuestionId?: string;
  questionOpenRequest?: number;
  streamKey: string;
  loading: boolean;
  hasSession: boolean;
  standalone: boolean;
  contextLabel: string;
  taskTitle?: string;
  railCollapsed: boolean;
  runSummary: RunSummary | null;
  runtimeRuns?: RuntimeTraceView[];
  summaryLoading: boolean;
  onOpenChanges: () => void;
  onOpenRuntime: () => void;
  onOpenTerminal?: () => void;
  onOpenFile?: (reference: FileReference) => void;
  recoveryAction?: RunRecoveryAction;
  onOpenSettings?: () => void;
  agentPanelTarget: HTMLElement | null;
  onOpenAgentPanel: () => void;
  onCloseAgentPanel: () => void;
  onPromptSuggestion: (prompt: string) => void;
  onApproval: (requestId: string, decision: string, feedback?: string) => Promise<void>;
  onOpenRail: () => void;
}

const approvalLabels: Record<string, string> = {
  allow_once: "仅允许这一次",
  allow_turn: "本轮允许这个文件",
  allow_all_turn: "本轮允许所有修改",
  allow_always: "以后自动允许此操作",
  deny_once: "暂不允许",
  deny_with_feedback: "拒绝并告诉它原因",
  deny_always: "以后自动拒绝此操作",
};

function statusText(status: RunStatus): string {
  return {
    idle: "就绪",
    queued: "排队中",
    running: "正在执行",
    stopping: "正在停止",
    completed: "已完成",
    failed: "执行失败",
    stopped: "已停止",
  }[status];
}

function friendlyToolError(output: string, toolName: string): string {
  const availability = toolAvailabilityFailure(output, toolName);
  if (availability) return availability.summary;
  const normalized = output.trim();
  if (!normalized) return "这次操作未成功，工具未提供错误详情。";
  if (/^Canceled: (?:parent Runtime canceled this tool call|Tool canceled (?:before|while awaiting) permission approval|Tool canceled while awaiting permission|file edit canceled before writing)/.test(normalized)) {
    return "任务已停止，这项操作已取消；无需为这条取消记录重试。";
  }
  if (/^Input validation error in memory_note_update:/.test(normalized)
      && /characters exceeds|entries exceeds/.test(normalized)) {
    return "交接笔记过长或条目过多，需要精简后重试；这与账户额度无关。";
  }
  if (/^(?:confirmed_findings|ruled_out)\[\d+\]\.source_event_ids/.test(normalized)) {
    return "交接笔记引用的证据编号不正确，需要核对原始工具结果后重试。";
  }
  if (/^Context protection:/.test(normalized)) {
    if (/This scope cannot record notes or rotate/.test(normalized)) {
      return "审查上下文接近容量上限，这项读取未执行；审查 Agent 需使用已有证据，或明确报告尚缺的证据。";
    }
    return "单次上下文接近容量上限，需先保存交接笔记并整理上下文，再继续这项操作。";
  }
  if (/^CandidateWriteRefused:/.test(normalized) && /outside this candidate's live write scope/.test(normalized)) {
    return "该文件超出此辅助任务的修改范围，需要由主任务重新安排。";
  }
  if (/Input validation error|invalid (arguments|parameters)|validation failed/i.test(normalized)) {
    return "这次操作的参数不符合要求，需要调整后重试。";
  }
  if (/Unknown or unavailable tool|tool.*not (found|available)/i.test(normalized)) {
    return "当前工具不可用，需要检查可用工具或改用其他方式。";
  }
  if (/PermissionError|permission denied|access denied|EACCES/i.test(normalized)) {
    return "这次操作缺少必要权限，请核对访问范围和授权。";
  }
  if (/FileNotFoundError|ENOENT|no such file|^Path does not exist:/i.test(normalized)) {
    return "没有找到目标文件或目录，需要核对路径。";
  }
  if (["grep_files", "find_references"].includes(toolName)
      && /^Search (?:snapshot timed out;|timed out after [\d.]+s;|incomplete: timeout(?:\r?$))/m.test(normalized)) {
    return "代码搜索已超时并停止，返回结果不完整；可缩小搜索目录或指定文件后重试。";
  }
  if (/^WEB_SEARCH_CONFIG_ERROR\b/.test(normalized)) {
    return "搜索服务配置不完整，请检查所选搜索源及其 API 密钥。";
  }
  if (/^WEB_SEARCH_UNAVAILABLE\b/.test(normalized) || normalized.includes("No search results found")) {
    return "搜索已结束，但没有取得可用结果；详情中列出了各搜索源的失败原因。";
  }
  if (normalized.includes("WEB_FETCH_TLS_ERROR") || /UNEXPECTED_EOF|SSL.*EOF/i.test(normalized)) {
    return "目标站点提前断开了安全连接；可以重试或换一个来源。";
  }
  if (normalized.includes("WEB_FETCH_NETWORK_ERROR")) {
    return "当前无法连接目标网址；请检查网络或换一个来源。";
  }
  if (/TimeoutError|timed? ?out/i.test(normalized)) {
    return "这次操作超时，尚未确认结果；请先核对执行状态再决定是否重试。";
  }
  const firstUsefulLine = normalized.split(/\r?\n/).find((line) => line.trim()) || normalized;
  return /[\u4e00-\u9fff]/.test(firstUsefulLine) ? firstUsefulLine.slice(0, 180) : "这次操作未成功，可展开详情核对原因。";
}

function resolvedApprovalText(item: Extract<TimelineItem, { type: "approval" }>): string {
  const decision = item.resolved || "";
  if (decision === "history_only") return "历史审批记录；可继续读取工作过程查看审批结果。";
  if (decision === "allow_once") {
    if (item.request.operationType === "network_read") {
      return "已允许这次网页访问，MiniCode 正在继续。";
    }
    if (item.request.operationType === "edit") {
      return "已允许这次修改，MiniCode 正在继续。";
    }
    return "已允许这项操作，MiniCode 正在继续。";
  }
  if (decision.startsWith("allow")) {
    return `已完成授权：${approvalLabels[decision] || decision}。`;
  }
  if (decision.startsWith("deny")) {
    return "已拒绝这项操作，MiniCode 不会执行它。";
  }
  return `审批已处理：${approvalLabels[decision] || decision}`;
}

const ToolExpansion = createContext<{ prefix: string; details: string[]; incomplete?: boolean; changed?: () => void }>({ prefix: "", details: [] });
const StepChecks = createContext<ReadonlyMap<string, StepCheck[]>>(new Map());

function ToolItem({ item, recovered = false, correction, active = false, execution }: { item: Extract<TimelineItem, { type: "tool" }>; recovered?: boolean; correction?: ToolTimelineItem; active?: boolean; execution?: ExecutionSnapshot | null }) {
  const expansion = useContext(ToolExpansion);
  const stepChecks = useContext(StepChecks);
  const checks = stepChecks.get(item.id) || [];
  const pendingResult = item.status === "unknown" && expansion.incomplete;
  const detailId = `${expansion.prefix}/${item.id}`;
  const [open, setOpen] = useState(() => expansion.details.includes(detailId));
  if (stepChecks.has(item.id) && !checks.length) return null;
  if (isAgentCommunicationTool(item)) return <AgentMessageItem item={item} execution={execution} recovered={recovered} />;
  const { label, target } = toolActivity(item);
  const schemaLoading = item.toolInput?.status === "schema_required" || (item.output || "").startsWith("ToolSchemaRequired:");
  const inputProblem = item.toolInput?.status === "invalid";
  const availability = item.status === "error" ? toolAvailabilityFailure(item.output || "", item.toolName) : undefined;
  const firstIssue = item.toolInput?.issues[0];
  const argumentError = item.toolInput?.argumentError;
  const unparsedInput = unparsedToolInput(item);
  const recovery = item.toolRecovery;
  const commandRecovery = correction && recovered ? commandRecoveryPresentation(item, correction) : undefined;
  const autoRecovered = item.status === "success" && recovery?.status === "recovered" && Boolean(recovery.attempts?.length);
  const retrying = item.status === "running" && recovery?.status === "retrying";
  const deferral = recovered ? undefined : deferredToolPresentation(item, active);
  const agentHandling = item.status === "error" && recovery?.status === "agent_action" && active && !recovered && !availability && !deferral;
  const canceled = isCanceledTool(item);
  const interrupted = isInterruptedTool(item);
  const foldStatus = item.status === "error" && !recovered && !canceled && !interrupted && !agentHandling && !deferral;
  const rotation = contextRotationPresentation(item);
  const wait = agentWaitPresentation(item, execution, active);
  const statusLabel = interrupted ? "新消息中断，未执行" : canceled ? "已取消" : deferral?.label || wait?.label || rotation?.label || (autoRecovered ? "重试后已恢复" : recovered ? recovery?.kind === "verification_alternative" ? "已改用登记验证" : commandRecovery?.label || (schemaLoading ? "加载后已执行" : inputProblem ? "已纠正" : "已恢复") : retrying
    ? `退避重试 ${recovery.attempt}/${recovery.maxAttempts}` : agentHandling ? recovery.kind === "command_timeout" ? "超时，Agent 处理中" : "Agent 处理中" : item.status === "running"
      ? "正在执行"
      : item.status === "unknown"
        ? pendingResult ? "结果尚未加载" : "结果未确认"
        : item.status === "error"
          ? unparsedInput ? "参数格式错误，未执行" : item.toolInput?.status === "schema_required" ? "待补充工具说明" : inputProblem ? active ? "参数待纠正" : "参数错误，未执行" : availability?.label || commandFailureLabel(item) || "执行失败"
          : item.durationMs != null
            ? `${item.durationMs} ms`
             : "已完成");
  const errorSummary = interrupted ? "本次操作在执行前被新的用户消息中断。" : deferral?.summary || wait?.summary || rotation?.summary || (autoRecovered ? "重试后已恢复，之前的失败详情已保留。"
    : recovered ? recovery?.kind === "verification_alternative" ? "已通过登记检查取得验证结果；原命令未执行，原始权限限制记录保留在详情中。" : commandRecovery ? commandRecovery.summary : schemaLoading ? "加载完整工具定义后，同一操作已由后续调用执行成功；原暂缓记录保留。" : inputProblem ? "已纠正并执行成功，原始错误保留在详情中。"
      : item.toolName === "context_rotate" ? "本次上下文整理后续已成功，原始失败记录保留在详情中。"
        : "同一操作后续已成功，原始失败记录保留在详情中。"
    : retrying ? `遇到暂时性故障，等待 ${((recovery.retryDelayMs || 0) / 1000).toFixed(1)} 秒后重试；可随时停止任务。`
    : agentHandling ? recovery.kind === "command_timeout" ? "命令已超时并结束，已保留输出，Agent 正在检查并调整，任务继续。"
      : `${recovery.attempts?.length ? "自动重试未恢复，" : ""}已将错误返回 Agent 调整，任务继续。`
    : unparsedInput ? `模型返回的参数未能解析，工具没有执行。${argumentError?.line != null ? `错误位于第 ${argumentError.line} 行、第 ${argumentError.column} 列。` : ""}`
    : item.toolInput?.status === "schema_required" ? "本次未执行：需要先加载工具参数说明，再重新调用。"
    : item.status === "error" && firstIssue ? `参数 ${firstIssue.path.join(".") || "输入"}：需要 ${firstIssue.expected}，收到 ${firstIssue.actual}。`
    : item.status === "error" ? availability?.summary || commandFailureSummary(item) || friendlyToolError(item.output || "", item.toolName)
       : item.status === "unknown" && !pendingResult ? "未收到这次操作的结果，请核对实际状态后再重试。" : "");

  const rawDetails = open && <>
    <p className="tool-technical-name">{item.toolName}</p>
    {unparsedInput ? <div><span>原始参数（解析失败）</span><pre>{argumentError?.rawArguments ?? "原始参数无法解析；此记录未保存可显示的原文。"}</pre>
      {argumentError?.truncated && <p>此处显示参数开头，完整原文保存在运行记录中。</p>}</div>
      : <div><span>输入</span><pre>{JSON.stringify(item.input ?? {}, null, 2)}</pre></div>}
    {item.toolName === "run_command" && !interrupted && !deferral && !unparsedInput && !inputProblem && !availability && !canceled
      ? <CommandOutput item={item} />
      : item.output && <div><span>{interrupted ? "中断详情" : deferral ? "暂缓详情" : item.status === "error" ? "错误详情" : "结果"}</span><pre>{item.output}</pre></div>}
  </>;
  return (
    <details open={open} data-detail-id={detailId} data-tool-call-id={item.toolCallId} data-corrected-by={correction?.toolCallId} onToggle={event => { setOpen(event.currentTarget.open); expansion.changed?.(); }} className={`tool-item tool-${pendingResult ? "pending-result" : item.status} ${canceled ? "is-canceled" : ""} ${interrupted ? "is-interrupted" : ""} ${deferral ? "is-deferred" : ""} ${recovered || autoRecovered ? "is-recovered" : ""} ${agentHandling ? "is-recovering" : ""}`}>
      <summary>
        <span className="tool-icon">
          {wait ? wait.waiting ? <LoaderCircle size={15} className="spin" /> : wait.satisfied ? <Check size={15} /> : <CircleAlert size={15} /> : canceled || interrupted || deferral ? <Square size={15} /> : recovered || autoRecovered ? <Check size={15} /> : item.status === "running" || agentHandling ? (
            <LoaderCircle size={15} className="spin" />
          ) : item.status === "unknown" ? (
            pendingResult ? <span aria-hidden="true">…</span> : <CircleAlert size={15} />
          ) : item.status === "error" ? (
            <span className="tool-error-dot" aria-hidden="true" />
          ) : (
            <Check size={15} />
          )}
        </span>
        <span className="tool-label">
          <strong>{label}{target && <code title={target}>{target}</code>}</strong>
          {!foldStatus && <small>{statusLabel}</small>}
          {errorSummary && item.status !== "error" && <span className="tool-error-summary">{errorSummary}</span>}
          <TaskCheckSummary checks={checks} />
        </span>
        <ChevronRight size={15} className="tool-chevron" />
      </summary>
      {open && <div className="tool-detail">
        {errorSummary && item.status === "error" && <p className="tool-error-summary">
          {foldStatus && <><strong>{statusLabel}</strong><br /></>}
          {errorSummary}
          {expansion.incomplete && !active && !recovered && !canceled && !interrupted && <><br />后续记录尚未加载完整，纠正状态待确认。</>}
        </p>}
        {agentOwner(item, execution).kind === "unknown" && <AgentBadge source={item} execution={execution} />}
        <TaskCheckDetails checks={checks} />
        {recovered && correction && <details className="tool-retry-history">
          <summary>查看后续成功命令</summary>
          <div><span>输入</span><pre>{JSON.stringify(correction.input, null, 2)}</pre></div>
          <CommandOutput item={correction} />
        </details>}
        {recovery?.attempts?.length ? <details className="tool-retry-history">
          <summary>查看恢复过程（{recovery.attempts.length} 次失败记录）</summary>
          {recovery.attempts.map(attempt => <div key={attempt.attempt}>
            <span>第 {attempt.attempt} 次尝试{attempt.delayMs != null ? ` · 退避 ${(attempt.delayMs / 1000).toFixed(1)} 秒` : ""}</span>
            <pre>{attempt.output}</pre>
          </div>)}
        </details> : null}
        {item.toolInput?.recoveryElapsedMs != null && <p>本次纠正耗时 {formatDuration(item.toolInput.recoveryElapsedMs / 1000)}，原始调用记录已保留。</p>}
        {checks.length ? <details className="tool-check-raw"><summary>工具输入与输出</summary>{rawDetails}</details> : rawDetails}
      </div>}
    </details>
  );
}

function ToolGroup({ tools, execution }: { tools: ToolTimelineItem[]; execution?: ExecutionSnapshot | null }) {
  const expansion = useContext(ToolExpansion);
  const detailId = `${expansion.prefix}/group-${tools[0].id}`;
  const [open, setOpen] = useState(() => expansion.details.includes(detailId));
  const allTimed = tools.every((tool) => tool.durationMs != null && Number.isFinite(tool.durationMs));
  const totalSeconds = tools.reduce((total, tool) => total + (tool.durationMs || 0), 0) / 1000;
  return <details open={open} data-detail-id={detailId} className="tool-group" onToggle={event => { setOpen(event.currentTarget.open); expansion.changed?.(); }}>
    <summary>
      <Check size={15} />
      <span><strong>{activitySummary(tools)}</strong></span>
      <ChevronRight size={15} className="tool-chevron" />
    </summary>
    {open && <div className="tool-group-content">
      {allTimed && <p className="tool-group-timing">工具累计耗时 {formatDuration(totalSeconds)}</p>}
      {tools.map((item) => <ToolItem key={item.id} item={item} execution={execution} />)}
    </div>}
  </details>;
}

function ApprovalItem({
  item,
  onApproval,
}: {
  item: Extract<TimelineItem, { type: "approval" }>;
  onApproval: (requestId: string, decision: string, feedback?: string) => Promise<void>;
}) {
  const [feedback, setFeedback] = useState("");
  const [feedbackMode, setFeedbackMode] = useState(false);
  const [busy, setBusy] = useState(false);

  async function decide(choice: ApprovalChoice) {
    setBusy(true);
    try {
      await onApproval(item.requestId, choice.decision, feedback.trim() || undefined);
    } finally {
      setBusy(false);
    }
  }

  if (item.resolved) {
    return (
      <div className="approval-card is-resolved">
        <CircleCheck size={18} />
        <span>{resolvedApprovalText(item)}</span>
      </div>
    );
  }

  const choices = item.request.choices || [];
  const primaryChoices = choices.filter((choice) => ["allow_once", "deny_once"].includes(choice.decision));
  const advancedChoices = choices.filter((choice) => !["allow_once", "deny_once"].includes(choice.decision));
  const visiblePrimaryChoices = primaryChoices.length > 0 ? primaryChoices : choices;
  const technicalDetails = item.request.technicalDetails?.length
    ? item.request.technicalDetails
    : item.request.details.map((value, index) => ({ label: `详情 ${index + 1}`, value }));

  function choiceButton(choice: ApprovalChoice, compact = false) {
    const asksForFeedback = choice.decision === "deny_with_feedback";
    return (
      <button
        key={`${choice.decision}-${choice.label}`}
        className={choice.decision === "allow_once" && !compact ? "button button-primary" : "button button-secondary"}
        onClick={() => {
          if (asksForFeedback && !feedbackMode) {
            setFeedbackMode(true);
            return;
          }
          void decide(choice);
        }}
        disabled={busy}
      >
        {choice.label || approvalLabels[choice.decision] || choice.decision}
      </button>
    );
  }

  return (
    <section id={`approval-${item.id}`} tabIndex={-1} className="approval-card" aria-label="需要审批">
      <div className="approval-heading">
        <CircleAlert size={19} />
        <div>
          <strong>{item.request.title || "需要你确认"}</strong>
          <p>{item.request.action || item.request.summary}</p>
        </div>
      </div>
      {(item.request.impact || item.request.reason || item.request.target) && (
        <dl className="approval-facts">
          {item.request.impact && <div><dt>会影响什么</dt><dd>{item.request.impact}</dd></div>}
          {item.request.reason && <div><dt>为什么会弹窗</dt><dd>{item.request.reason}</dd></div>}
          {item.request.target && <div><dt>{item.request.targetLabel || "运行位置"}</dt><dd>{item.request.target}</dd></div>}
        </dl>
      )}
      {technicalDetails.length > 0 && (
        <details className="approval-technical">
          <summary>查看命令与技术详情</summary>
          <dl>
            {technicalDetails.map((detail, index) => (
              <div key={`${detail.label}-${index}`}>
                <dt>{detail.label}</dt>
                <dd><pre>{detail.value}</pre></dd>
              </div>
            ))}
          </dl>
        </details>
      )}
      {item.request.timeoutPolicy && <p className="approval-timeout">{item.request.timeoutPolicy}</p>}
      {feedbackMode && (
        <label className="approval-feedback">
          <span>告诉 MiniCode 应该怎么调整</span>
          <textarea
            autoFocus
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            placeholder="例如：不要使用 PowerShell，改用只读的时间工具"
            rows={2}
          />
          <div className="approval-feedback-actions">
            <button className="button button-secondary" onClick={() => setFeedbackMode(false)} disabled={busy}>取消</button>
            <button
              className="button button-primary"
              onClick={() => {
                const choice = choices.find((candidate) => candidate.decision === "deny_with_feedback");
                if (choice) void decide(choice);
              }}
              disabled={busy || !feedback.trim()}
            >
              拒绝并发送原因
            </button>
          </div>
        </label>
      )}
      <div className="approval-actions">
        {visiblePrimaryChoices.map((choice) => choiceButton(choice))}
      </div>
      {primaryChoices.length > 0 && advancedChoices.length > 0 && (
        <details className="approval-scope-options">
          <summary>更多授权范围</summary>
          <div>{advancedChoices.filter((choice) => choice.decision !== "deny_with_feedback" || !feedbackMode).map((choice) => choiceButton(choice, true))}</div>
        </details>
      )}
    </section>
  );
}

function EmptyStream({
  standalone,
  contextLabel,
  onPromptSuggestion,
}: {
  standalone: boolean;
  contextLabel: string;
  onPromptSuggestion: (prompt: string) => void;
}) {
  const suggestions = standalone
    ? [
        { icon: Lightbulb, title: "梳理一个想法", detail: "把灵感变成可执行的步骤", prompt: "帮我梳理一个想法，并给出可执行步骤", tone: "explore" },
        { icon: BookOpen, title: "理解一个概念", detail: "用清晰的解释和例子入门", prompt: "解释一个技术概念，并用例子说明", tone: "build" },
        { icon: GitCompareArrows, title: "比较不同方案", detail: "找到适合当前目标的选择", prompt: "比较两个方案的优缺点和适用场景", tone: "review" },
        { icon: Hammer, title: "制定行动计划", detail: "拆解目标，明确下一步", prompt: "帮我把一个目标拆成具体步骤，并明确第一步该做什么", tone: "fix" },
      ]
    : [
        { icon: Telescope, title: "探索代码", detail: "了解结构与运行方式", prompt: "探索这个项目的代码结构，说明主要模块和本地运行方式", tone: "explore" },
        { icon: Hammer, title: "构建新功能", detail: "把想法变成可用的功能", prompt: "我想为这个项目构建一个新功能，请先了解项目结构，再和我一起明确需求", tone: "build" },
        { icon: ScanLine, title: "审查改动", detail: "检查问题与回归风险", prompt: "阅读当前改动，找出可能的回归风险", tone: "review" },
        { icon: Bug, title: "修复问题", detail: "定位原因，验证修复", prompt: "检查这个项目如何启动，并修复影响启动的问题", tone: "fix" },
      ];
  return (
    <div className="empty-stream">
      <div className="empty-symbol" aria-hidden="true"><TerminalSquare size={38} strokeWidth={1.4} /></div>
      <h1>{standalone ? "有什么想一起完成的？" : <>你想在 <span>{contextLabel}</span> 中构建什么？</>}</h1>
      <p>
        {standalone
          ? "聊想法、问问题，或从左侧选择一个项目开始构建。"
          : "描述你的目标，一起探索代码、完成修改并验证结果。"}
      </p>
      <div className="suggestion-list">
        {suggestions.map(({ icon: Icon, title, detail, prompt, tone }) => (
          <button className={`suggestion-card suggestion-${tone}`} key={title} onClick={() => onPromptSuggestion(prompt)}>
            <Icon size={21} strokeWidth={1.6} aria-hidden="true" />
            <strong>{title}</strong>
            <span>{detail}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

const AssistantMessage = memo(function AssistantMessage({ item, commentary = false }: { item: Pick<Extract<TimelineItem, { type: "assistant" }>, "content" | "streaming" | "deliveryState">; commentary?: boolean }) {
  return <article className={`assistant-message ${commentary ? "is-commentary" : "is-answer"} ${item.streaming ? "is-streaming" : ""}`}>
    {item.deliveryState === "pending" && <div className="answer-state" role="status">
      {item.streaming ? "正在生成答复 · 待验证" : "答复草稿 · 待验证"}
    </div>}
    {item.deliveryState === "rejected" && <div className="answer-state is-unconfirmed" role="status">这份草稿未确认为最终答复</div>}
    <AssistantMarkdown content={item.content} />
  </article>;
}, (previous, next) => previous.commentary === next.commentary && previous.item.content === next.item.content
  && previous.item.streaming === next.item.streaming && previous.item.deliveryState === next.item.deliveryState);

function UserMessage({ content, anchorId }: { content: string; anchorId?: string }) {
  const message = parseMessage(content);
  const [copyStatus, setCopyStatus] = useState("");
  useEffect(() => {
    if (!copyStatus) return;
    const timer = window.setTimeout(() => setCopyStatus(""), 2500);
    return () => window.clearTimeout(timer);
  }, [copyStatus]);
  return <div className="user-message" id={anchorId} tabIndex={-1} role="group" aria-label="用户输入">
    <div className={`user-message-content ${message.text ? "has-copy" : ""}`}><SkillText text={message.text} /><AttachmentList files={message.attachments} /></div>
    {message.text && <button className="user-message-copy" type="button" aria-label={copyStatus === "已复制" ? "已复制输入" : "复制输入"}
      title={copyStatus || "复制输入"} onClick={async () => {
        try { await navigator.clipboard.writeText(message.text); setCopyStatus("已复制"); }
        catch { setCopyStatus("复制失败，请选中文字复制"); }
      }}>{copyStatus === "已复制" ? <Check size={16} /> : <Copy size={16} />}</button>}
    {copyStatus && <span className="user-message-copy-status" role="status">{copyStatus}</span>}
  </div>;
}

function promptAnchorId(source: string, round: ConversationRound): string {
  return `prompt-${encodeURIComponent(source)}-${encodeURIComponent(round.id)}`;
}

function conversationAnchor(id: string, round: ConversationRound): ConversationAnchor {
  const answer = round.items.filter((item): item is Extract<TimelineItem, { type: "assistant" }> => item.type === "assistant" && item.phase !== "commentary").at(-1);
  return { id, prompt: isContextRotationRequest(round.user?.content || "") ? "整理上下文" : parseMessage(round.user?.content || "").text, preview: answer?.content
    .replace(/(^|\n)[ \t]*(?:#{1,6}|>)\s+/g, "$1").replace(/\s+/g, " ").slice(0, 200) || "" };
}

function ConversationItems({ items, active, process = false, expandErrors = false, scopedAgent = false, recovered, commandCorrections, execution, onApproval }: {
  items: TimelineItem[]; active: boolean; process?: boolean; expandErrors?: boolean; scopedAgent?: boolean; recovered?: Set<string>; commandCorrections?: ReadonlyMap<string, ToolTimelineItem>; execution?: ExecutionSnapshot | null; onApproval: RunStreamProps["onApproval"];
}) {
  const stepChecks = useContext(StepChecks);
  const corrections = commandCorrections ?? correctedCommandResults(items);
  const entries = groupToolCalls(items, new Set(stepChecks.keys()));
  return <div className="conversation-items">{entries.map((item) => {
    if (item.type === "tool-group") return <ToolGroup key={item.id} tools={item.tools} execution={execution} />;
    if (item.type === "user") return <UserMessage key={item.id} content={item.content} />;
    if (item.type === "assistant") {
      const commentary = process || item.phase === "commentary";
      return <AssistantMessage key={item.id} item={item} commentary={commentary} />;
    }
    if (item.type === "tool") return <ToolItem item={item} recovered={recovered?.has(item.id)} correction={corrections.get(item.id)} active={active} key={item.id} execution={execution} />;
    if (item.type === "images") return <RunImages key={item.id} images={item.images} active={active} />;
    if (item.type === "approval") return <div className="agent-attributed-message" key={item.id}>
      {!scopedAgent && agentOwner(item, execution).kind === "child" && <AgentBadge source={item} execution={execution} />}
      <ApprovalItem item={item} onApproval={onApproval} />
    </div>;
    if (item.type === "progress") {
      if (item.questionId && item.question) return <QuestionRecord key={item.id} item={item} items={items} />;
      // Runtime phase is already shown in the sticky status line.
      if (item.source === "runtime" && item.content.startsWith("运行阶段") && !needsAttention(item)) return null;
      return <div className={`progress-item progress-${item.stopReason === "blocked" ? "warning" : item.status}`} key={item.id}>
        {item.status === "running" && active ? <LoaderCircle size={14} className="spin" />
          : item.status === "complete" ? <Check size={14} /> : item.status === "stopped" ? <Square size={14} /> : <CircleAlert size={14} />}
        <span>{!scopedAgent && agentOwner(item, execution).kind === "child" && <AgentBadge source={item} execution={execution} />} {item.content}</span>
      </div>;
    }
    return process ? <details className="summary-details" key={item.id} open={expandErrors}>
      <summary>运行错误记录</summary><pre className="failure-raw-text">{item.content}</pre>
    </details> : <FailureNotice key={item.id} failure={diagnoseRunFailure(null, null, [item.content])} />;
  })}</div>;
}

function DelegatedTask({ entry, active, status, recovered, historical, onOpen, execution }: {
  entry: Extract<AgentProcessEntry, { type: "agent-task" }>; active: boolean; status: RunStatus;
  recovered: Set<string>; historical: Set<string>; onOpen: (trigger: HTMLButtonElement) => void;
  execution?: ExecutionSnapshot | null;
}) {
  const { owner, node, items } = entry;
  const state = settledAgentState(node.state, active, status === "stopped");
  const working = active && ["CLAIMED", "RUNNING", "VERIFYING"].includes(state);
  const unfinished = active && !["SUCCEEDED", "CANCELED", "SUPERSEDED", "FAILED_TERMINAL", "UNKNOWN"].includes(state);
  const objective = agentTaskObjective(node.objective);
  const review = reviewStatus(node, active, status === "stopped", execution?.nodes);
  const label = review?.label || agentImplementationLabel(node, execution) || childRecoveryLabel(node, state) || advisoryFailureLabel(node) || nodeStateLabels[state] || "状态未知";
  const failures = toolFailureCount(items, recovered, historical);
  return <button type="button" className={`delegated-task delegated-${state.toLowerCase()}`} data-node-id={node.nodeId} data-review-tone={review?.tone}
    title={review ? `${review.description}\n${objective}` : objective} aria-label={`查看${ownerLabel(owner)}：${label}`} aria-controls="agent-panel-host" onClick={event => onOpen(event.currentTarget)}>
      <GitFork size={16} className="delegated-icon" aria-hidden="true" />
      <span className="delegated-label"><strong>{owner.label}</strong><span>{owner.role}</span></span>
      <span className="delegated-status">{working && <LoaderCircle size={12} className="spin" aria-hidden="true" />}{label}</span>
      <ChevronRight size={14} className="delegated-chevron" aria-hidden="true" />
      {failures > 0 && <span className="delegated-issue" title={toolIssueSummary(items, recovered)}>{toolFailureLabel(failures, unfinished)}</span>}
  </button>;
}

function ConversationTurn({ round, active, continuing = false, status, execution, elapsedMs, reconnecting = false, onApproval, onOpenAgent, anchorId, forkTurn, forkUnavailable, onFork, fileChanges, onReview, undoEligible = false, runtimeRuns = [], summary, summaryCard, projectCheck, sessionId, recoveryAction, onOpenRuntime, initiallyOpen, onProcessPage, onLoadProcessPage, onExpanded, details = [] }: {
  continuing?: boolean;
  details?: string[];
  initiallyOpen?: boolean; onExpanded?: () => void;
  onProcessPage?: RunStreamProps["onProcessPage"];
  onLoadProcessPage?: RunStreamProps["onLoadProcessPage"];
  round: ConversationRound; active: boolean; status: RunStatus; execution?: ExecutionSnapshot | null; elapsedMs?: number; reconnecting?: boolean; onApproval: RunStreamProps["onApproval"];
  anchorId: string; forkTurn?: SessionTurn; forkUnavailable?: boolean; onFork?: RunStreamProps["onFork"];
  fileChanges?: TurnFileChanges | null; onReview?: (path?: string) => void;
  undoEligible?: boolean;
  runtimeRuns?: RuntimeTraceView[]; summary?: RunSummary | null;
  summaryCard?: ReactNode;
  projectCheck?: RoundProjectCheck; sessionId?: string;
  recoveryAction?: RunRecoveryAction; onOpenRuntime?: () => void;
  onOpenAgent: (key: string, trigger: HTMLButtonElement) => void;
}) {
  // Restore each older turn from its own execution, never from the latest run's roster.
  execution = executionForRound(round, execution, runtimeRuns);
  const hasImages = round.items.some(item => item.type === "images");
  const hasQuestions = round.items.some(item => item.type === "progress" && item.questionId && item.question);
  const hasInlineSteps = hasImages || hasQuestions;
  const processActive = active || continuing;
  const manualRotation = isContextRotationRequest(round.user?.content || "");
  const { process, outcome, recovered, historical } = presentRound(round.items, processActive, execution);
  const roundRunIds = [...new Set(round.items.flatMap(item => item.executionId ? [item.executionId] : []))];
  const recordedTrace = traceForRun(runtimeRuns, summary?.runId || (roundRunIds.length === 1 ? roundRunIds[0] : ""));
  const reviewCloseout = !active && status === "failed" ? summary?.reviewCloseout || recordedTrace?.reviewCloseout : undefined;
  if (reviewCloseout && !outcome.some(item => item.type === "assistant" && item.messageId?.startsWith("review-closeout:"))) {
    outcome.push({ id: `review-closeout:${summary?.runId || recordedTrace?.runId}`, type: "assistant",
      messageId: `review-closeout:${summary?.runId || recordedTrace?.runId}`, phase: "final_answer",
      content: reviewCloseout, timestamp: round.items.at(-1)?.timestamp || 0 });
  }
  const commandCorrections = correctedCommandResults(round.items);
  const failure = !active ? summary?.failure || (status === "failed" || status === "stopped"
    ? failureForRound(round.items, runtimeRuns, execution, status === "stopped") : null) : null;
  const stopped = failure?.kind === "canceled";
  const interruptedByError = Boolean(failure && !stopped && (status !== "completed" || failure.kind !== "verification"));
  const [open, setOpen] = useState(manualRotation ? initiallyOpen ?? false : processActive || interruptedByError || (initiallyOpen ?? false));
  const previousProcess = useRef({ active: processActive, failed: interruptedByError });
  useEffect(() => {
    // Keep failed transcripts readable, including errors restored after the run settles.
    if (!manualRotation && (previousProcess.current.active !== processActive || interruptedByError && !previousProcess.current.failed)) {
      setOpen(processActive || interruptedByError);
    }
    previousProcess.current = { active: processActive, failed: interruptedByError };
  }, [processActive, interruptedByError, manualRotation]);
  const failureInSummary = Boolean(summary?.failure);
  const answer = outcome.filter((item): item is Extract<TimelineItem, { type: "assistant" }> => item.type === "assistant"
    && item.phase !== "commentary" && !item.streaming && !["pending", "rejected"].includes(item.deliveryState || "")).at(-1);
  const processIds = new Set(process.map(item => item.id));
  const presentedItems = new Map([...process, ...outcome].map(item => [item.id, item]));
  const terminalProgress = (item: TimelineItem) => item.type === "progress" && (item.status === "stopped" || item.status === "failed");
  const visibleProcess = round.items.map(item => presentedItems.get(item.id) || item).filter(item => (processIds.has(item.id)
    || failure && (item.type === "error" || terminalProgress(item)))
    && (item.type !== "progress" || item.source !== "runtime" || !item.content.startsWith("运行阶段")));
  const visibleOutcome = outcome.filter(item => item.type !== "images"
    && (!failure || item.type !== "error" && !terminalProgress(item)));
  const failedTools = toolFailureCount(process, recovered, historical);
  const incomplete = Boolean(forkTurn?.viewOnly && forkTurn.processAvailable && (!forkTurn.processMessages || forkTurn.nextProcessAfter != null));
  const unknownTools = incomplete ? 0 : process.filter(item => item.type === "tool" && item.status === "unknown" && !historical.has(item.id)).length;
  const imageSections = hasInlineSteps ? imageProcessSections(visibleProcess) : [];
  // The trailing section keeps snapshot-only tasks after the final inline card.
  const processSections = agentProcessSections(hasInlineSteps
    ? [...imageSections.map(section => section.type === "process" ? section.items : []), []]
    : [visibleProcess], execution);
  const processEntries = processSections.flat();
  const activity = activitySummary(process);
  const roundElapsed = elapsedMs ?? (summary?.elapsedSeconds != null ? summary.elapsedSeconds * 1000 : undefined);
  const duration = roundElapsed !== undefined && Number.isFinite(roundElapsed) && roundElapsed >= 0 ? readableElapsed(roundElapsed) : null;
  const processLabel = stopped ? `已停止${duration ? ` · 用时 ${duration}` : ""}` : processActive ? "工作过程" : duration ? `用时 ${duration}` : "查看工作过程";
  const issues = toolIssueSummary(process.filter(item => !historical.has(item.id)), recovered);
  const processDescription = [activity, issues, unknownTools ? `${unknownTools} 项结果未确认` : ""].filter(Boolean).join("，");
  const processIntroduction = <>
    {issues && <p className="tree-message">{issues}。此处统计工具调用记录，不代表项目缺陷数量；已确认恢复的调用不计入失败数。</p>}
    {!processActive && execution && <div className="process-context"><span>本轮 {profileLabels[execution.profile]}</span></div>}
  </>;
  const renderProcess = (entries: AgentProcessEntry[]) => entries.map(entry => entry.type === "agent-task"
      ? <DelegatedTask key={entry.id} entry={entry} active={processActive} status={status} recovered={recovered} historical={historical} execution={execution}
        onOpen={trigger => execution && onOpenAgent(agentConversationKey(execution, entry.owner), trigger)} />
      : <ConversationItems key={entry.id} items={entry.items} active={processActive} process expandErrors={interruptedByError} recovered={recovered} commandCorrections={commandCorrections} execution={execution} onApproval={onApproval} />);
  const processContent = <>{processIntroduction}{renderProcess(processEntries)}</>;
  const inlineProcess = <>
    {open && processIntroduction}
    {imageSections.map((section, index) => section.type === "process"
      ? processSections[index].length > 0 && <div key={section.id} className="inline-process-section" data-process-section={section.id}>{open ? renderProcess(processSections[index])
        : hasQuestions && <button type="button" className="round-process-toggle question-process-placeholder"
          onClick={() => { setOpen(true); onExpanded?.(); }}><ChevronRight size={13} />{activitySummary(section.items) || "查看这段工作过程"}</button>}</div>
      : section.type === "question" ? <QuestionRecord key={section.id} item={section.item} items={round.items} />
      : <div key={section.id} className="inline-image-step">
        {section.producer && <>
          {agentOwner(section.producer, execution).kind === "child" && <AgentBadge source={section.producer} execution={execution} />}
          <ToolItem item={section.producer} active={processActive} recovered={recovered.has(section.producer.id)} correction={commandCorrections.get(section.producer.id)} execution={execution} />
        </>}
        <RunImages images={section.item.images} active={processActive} />
      </div>)}
    {hasInlineSteps && open && renderProcess(processSections.at(-1)!)}
  </>;
  const processIdentity = forkTurn?.turnId || `live:${round.id}`;
  const processContentId = `${anchorId}-process`;
  const processHeading = <>
    {stopped ? <StopLabel elapsedMs={roundElapsed} /> : <span>{processLabel}</span>}
    <ChevronRight size={14} className="process-chevron" />
    {failedTools > 0 && <span className="process-issue is-error" title={issues}>{toolFailureLabel(failedTools, processActive)}</span>}
    {unknownTools > 0 && <span className="process-issue is-unknown">{unknownTools} 项结果未确认</span>}
  </>;
  const turnResult = <>
    <ConversationItems items={visibleOutcome} active={active} execution={execution} onApproval={onApproval} />
    {failure && !stopped && !failureInSummary && <FailureNotice failure={failure} />}
    {projectCheck && (!failure || stopped || !failureInSummary) && (failure
      ? <details className="round-evidence" open={interruptedByError}><summary>查看项目检查与截图</summary>{projectCheck.records.map(record =>
          <ProjectCheckCard key={record.verifier} record={record} runId={projectCheck.runId} sessionId={sessionId} />)}</details>
      : projectCheck.records.map(record => <ProjectCheckCard key={record.verifier} record={record} runId={projectCheck.runId} sessionId={sessionId} active={active} />))}
    {!active && fileChanges && onReview && (failure
      ? <details className="round-evidence" open={interruptedByError}><summary>查看本轮改动</summary><RunChangesCard changes={fileChanges} onReview={onReview} /></details>
      : <RunChangesCard changes={fileChanges} onReview={onReview} undoSessionId={undoEligible ? sessionId : undefined} />)}
    {active && <AnswerConfirmation execution={execution} items={round.items} status={status} reconnecting={reconnecting} />}
    <TurnActions answer={active ? undefined : answer?.content}
      skills={usedSkills(!active && forkTurn ? [...round.items, ...historyToTimeline(forkTurn.displayMessages)] : round.items)}
      forkTurn={active || failure && !answer ? undefined : forkTurn} forkUnavailable={forkUnavailable} onFork={onFork} />
    {summaryCard}
  </>;
  if (manualRotation) {
    const committed = round.items.some(item => item.type === "tool" && hasContextRotationCommit(item));
    const awaitingApproval = processActive && outcome.some(item => item.type === "approval" && !item.resolved);
    const awaitingAnswer = processActive && round.items.some(item => item.type === "progress" && item.questionId && item.question && !item.questionResolution);
    const rotationLabel = stopped || status === "stopped" ? "上下文整理已停止"
      : status === "stopping" ? "正在停止整理上下文"
      : awaitingApproval ? "整理上下文 · 等待确认"
      : awaitingAnswer ? "整理上下文 · 等待回答"
      : processActive ? status === "queued" ? "等待整理上下文" : "正在整理上下文"
      : status === "failed" ? "上下文整理失败" : committed ? "上下文已整理" : "上下文整理已结束";
    return <StepChecks.Provider value={taskChecksForSteps(round.items)}><ToolExpansion.Provider value={{ prefix: anchorId, details, incomplete, changed: onExpanded }}>
      <div className={`conversation-turn ${active ? "is-active" : "is-settled"}`}>
        <details data-process-turn={processIdentity} className={`round-process context-rotation-process ${active ? "is-active" : "is-settled"}`} open={open}>
          <summary id={anchorId} title={open ? "收起整理过程" : "展开整理过程"} aria-expanded={open} aria-controls={processContentId} onClick={event => {
            event.preventDefault(); setOpen(value => !value); onExpanded?.();
          }}>
            {processActive ? <LoaderCircle size={14} className="spin" aria-hidden="true" />
              : stopped || status === "stopped" ? <Square size={14} aria-hidden="true" />
              : status === "failed" ? <CircleAlert size={14} aria-hidden="true" />
              : committed ? <Check size={14} aria-hidden="true" /> : <BookOpen size={14} aria-hidden="true" />}
            <span>{rotationLabel}{duration ? ` · 用时 ${duration}` : ""}</span>
            <ChevronRight size={14} className="process-chevron" aria-hidden="true" />
            {failedTools > 0 && <span className="process-issue is-error" title={issues}>{toolFailureLabel(failedTools, processActive)}</span>}
            {unknownTools > 0 && <span className="process-issue is-unknown">{unknownTools} 项结果未确认</span>}
          </summary>
          {open && <div className="round-process-content" id={processContentId}>
            <details className="summary-details context-rotation-request"><summary>整理指令</summary><pre>{round.user!.content}</pre></details>
            {forkTurn?.viewOnly && forkTurn.processAvailable && sessionId
              ? <SavedTurnProcess sessionId={sessionId} turn={forkTurn} onPage={onProcessPage} onLoadPage={onLoadProcessPage}>{hasInlineSteps ? inlineProcess : processContent}</SavedTurnProcess>
              : hasInlineSteps ? inlineProcess : processContent}
            {stopped && <RunStopLine elapsedMs={roundElapsed} recoveryAction={recoveryAction} onOpenRuntime={onOpenRuntime} />}
            {turnResult}
          </div>}
        </details>
      </div>
    </ToolExpansion.Provider></StepChecks.Provider>;
  }
  return <StepChecks.Provider value={taskChecksForSteps(round.items)}><ToolExpansion.Provider value={{ prefix: anchorId, details, incomplete, changed: onExpanded }}><div className={`conversation-turn ${active ? "is-active" : "is-settled"}`}>
    {round.user && <UserMessage content={round.user.content} anchorId={anchorId} />}
    {hasInlineSteps ? <section className={`round-inline-process ${active ? "is-active" : "is-settled"}`} data-process-turn={processIdentity} data-expanded={open}>
      <div className="round-inline-heading">
        <button type="button" className="round-process-toggle" title={processDescription} aria-expanded={open} aria-controls={processContentId}
          aria-label={`${processLabel}，${processDescription}`} onClick={() => { setOpen(value => !value); onExpanded?.(); }}>{processHeading}</button>
        {stopped && <StopActions recoveryAction={recoveryAction} onOpenRuntime={onOpenRuntime} />}
      </div>
      <div className="round-inline-content" id={processContentId}>
        {forkTurn?.viewOnly && forkTurn.processAvailable && sessionId
          ? <SavedTurnProcess enabled={open} sessionId={sessionId} turn={forkTurn} onPage={onProcessPage} onLoadPage={onLoadProcessPage}>{inlineProcess}</SavedTurnProcess>
          : inlineProcess}
      </div>
    </section> : (processEntries.length > 0 || forkTurn?.processAvailable) && <details data-process-turn={processIdentity} className={`round-process ${active ? "is-active" : "is-settled"} ${stopped ? "is-stopped" : ""}`} open={open}>
      <summary title={processDescription} aria-label={`${processLabel}，${processDescription}`} onClick={(event) => {
        event.preventDefault();
        setOpen((value) => !value);
        onExpanded?.();
      }}>
        {processHeading}
        {stopped && <StopActions recoveryAction={recoveryAction} onOpenRuntime={onOpenRuntime} />}
      </summary>
      {open && <div className="round-process-content">
        {forkTurn?.viewOnly && forkTurn.processAvailable && sessionId
          ? <SavedTurnProcess sessionId={sessionId} turn={forkTurn} onPage={onProcessPage} onLoadPage={onLoadProcessPage}>{processContent}</SavedTurnProcess>
          : processContent}
      </div>}
    </details>}
    {stopped && processEntries.length === 0 && <RunStopLine elapsedMs={roundElapsed} recoveryAction={recoveryAction} onOpenRuntime={onOpenRuntime} />}
    {turnResult}
  </div></ToolExpansion.Provider></StepChecks.Provider>;
}

function SavedTurnProcess({ sessionId, turn, onPage, onLoadPage, children, enabled = true }: {
  sessionId: string; turn: SessionTurn; onPage?: RunStreamProps["onProcessPage"];
  onLoadPage?: RunStreamProps["onLoadProcessPage"]; children: ReactNode; enabled?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const busyRef = useRef(false);
  const load = async (after: number) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError("");
    const request = new AbortController(); controller.current = request;
    try {
      if (onLoadPage) await onLoadPage(turn, after);
      else {
        const page = await api.getTurnMessages(sessionId, turn.turnId, after, request.signal);
        if (!request.signal.aborted) onPage?.(turn.turnId, page);
      }
    } catch (cause) {
      if (!request.signal.aborted) setError(cause instanceof Error ? cause.message : "无法读取工作过程。");
    } finally { if (!request.signal.aborted) { busyRef.current = false; setBusy(false); } }
  };
  useEffect(() => {
    if (!enabled) setBusy(false);
    if (enabled && !turn.processMessages) void load(0);
    return () => {
      controller.current?.abort(); controller.current = null; busyRef.current = false;
    };
  }, [sessionId, turn.turnId, enabled]);
  return <>{enabled && <div className="saved-process-controls">
    <p>{turn.toolCallCount !== undefined ? `${turn.toolCallCount} 次工具调用 · ` : ""}
      {turn.processMessages ? `已读取 ${turn.processMessages.length} / ${turn.processTotal ?? turn.processMessageCount ?? 0} 条记录` : "工作记录按需读取"}</p>
    {detailError && <p role="alert">{detailError}</p>}
    {turn.processMessages?.filter(message => message.truncatedFields?.length).map(message => <button key={message.displayIndex} className="history-load" disabled={busy}
      onClick={async () => {
        if (busyRef.current) return;
        busyRef.current = true; setBusy(true); setDetailError("");
        const request = new AbortController(); controller.current = request;
        try {
          const result = await api.getTurnMessage(sessionId, turn.turnId, message.displayIndex!, request.signal);
          if (!request.signal.aborted) onPage?.(turn.turnId, { messages: [{ ...result.message, displayIndex: message.displayIndex }],
            total: turn.processTotal || 0, nextAfter: turn.nextProcessAfter ?? null }, true);
        } catch (cause) { if (!request.signal.aborted) setDetailError(cause instanceof Error ? cause.message : "完整记录读取失败。"); }
        finally { if (!request.signal.aborted) { busyRef.current = false; setBusy(false); } }
      }}>第 {message.displayIndex! + 1} 条长记录已缩略 · 加载完整记录</button>)}
  </div>}
    {children}
    {enabled && <><PageSentinel cursor={turn.processMessages ? turn.nextProcessAfter : null} busy={busy} error={error}
      onLoad={() => void load(turn.nextProcessAfter ?? 0)} />
    {busy && <p className="page-loading" role="status">正在读取工作过程…</p>}
    {error && <div className="page-error"><p role="alert">{error}</p>
      <button className="history-load" disabled={busy} onClick={() => void load(turn.nextProcessAfter ?? 0)}>重试读取工作过程</button></div>}</>}
  </>;
}

function AnswerConfirmation({ execution, items, status, reconnecting }: {
  execution?: ExecutionSnapshot | null; items: TimelineItem[]; status: RunStatus; reconnecting: boolean;
}) {
  const view = confirmationView(execution, items, status);
  const [now, setNow] = useState(Date.now());
  const startedAt = view?.startedAt;
  useEffect(() => {
    if (!startedAt) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);
  if (!view) return null;
  return <div className="answer-confirmation" aria-label="答复确认进度">
    <LoaderCircle size={14} className="spin" aria-hidden="true" />
    <span role="status">{reconnecting ? "正在恢复连接，同步最新确认状态" : view.label}</span>
    {!reconnecting && startedAt !== undefined && Number.isFinite(startedAt) && <span className="confirmation-elapsed">已用 {readableElapsed(now - startedAt)}</span>}
  </div>;
}

export function RunStream({
  imageInputStatus,
  restoredView, captureViewRef, onViewChange, onProcessPage, onLoadProcessPage,
  workspace,
  savedTurnId,
  history, historyError, historyLoading, onLoadEarlier, onFork, session, onSourceSession,
  items,
  runStatus,
  run,
  execution,
  actualProfile,
  reconnecting,
  onCompose,
  onAnswerQuestion,
  activeQuestionId,
  questionOpenRequest,
  streamKey,
  loading,
  hasSession,
  standalone,
  contextLabel,
  taskTitle,
  railCollapsed,
  runSummary,
  runtimeRuns = [],
  summaryLoading,
  onOpenChanges,
  onOpenRuntime,
  onOpenTerminal,
  onOpenFile,
  recoveryAction,
  onOpenSettings,
  agentPanelTarget,
  onOpenAgentPanel,
  onCloseAgentPanel,
  onPromptSuggestion,
  onApproval,
  onOpenRail,
}: RunStreamProps) {
  const endRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const followingRef = useRef(true);
  const streamKeyRef = useRef(streamKey);
  const pendingView = useRef<ConversationViewState | undefined>(restoredView);
  const viewFrame = useRef(0);
  const timelineRef = useRef(items);
  const [awayFromBottom, setAwayFromBottom] = useState(false);
  const [hasNewProgress, setHasNewProgress] = useState(false);
  const [review, setReview] = useState<{ stream: string; source: ChangeReviewSource } | null>(null);
  const [agentSelection, setAgentSelection] = useState<{ stream: string; key: string | null } | null>(null);
  const agentTrigger = useRef<HTMLButtonElement | null>(null);
  const earlierScrollRef = useRef<ConversationViewState | null>(null);
  const active = runStatus === "running" || runStatus === "queued" || runStatus === "stopping";
  const hasHistory = Boolean(history?.turns.length);
  const hasHistoricalImages = items.some(item => item.type === "user" && item.content.includes("<minicode-image-attachments-v1>"))
    || Boolean(history?.turns.some(turn => turn.prompt.includes("<minicode-image-attachments-v1>")));
  const currentItems = latestTurnItems(items);
  const hasLiveRound = active || currentItems.some((item) => item.timestamp > 0);
  const latestHistory = history?.turns.at(-1);
  const currentUser = currentItems.find((item) => item.type === "user");
  const savedTurn = history?.turns.find((turn) => turn.turnId === (savedTurnId || run?.fileChanges?.turnId))
    || (!run?.fileChanges?.turnId && !active && hasLiveRound && latestHistory?.prompt === currentUser?.content ? latestHistory : undefined);
  const liveChanges = run?.fileChanges || savedTurn?.fileChanges;
  const hasLiveChanges = active && Boolean(run?.fileChanges?.files.length);
  const summaryChanges = hasLiveRound ? liveChanges : latestHistory?.fileChanges;
  function reviewChanges(changes: TurnFileChanges, path?: string, live = false) {
    const sessionId = session?.sessionId || run?.sessionId;
    if (sessionId) setReview({ stream: streamKey, source: { sessionId, turnId: changes.turnId, path, ...(live && run ? { runId: run.runId } : {}) } });
  }
  const displayItems = hasHistory ? hasLiveRound ? currentItems : [] : items;
  const rounds = conversationRounds(displayItems);
  const currentRoundStart = rounds.findIndex(round => round.user?.id === currentUser?.id);
  const historyRounds = useMemo(() => history?.turns.filter((turn) => turn.status !== "running" && (!hasLiveRound || turn.turnId !== savedTurn?.turnId)).map((turn) => ({
    turn, rounds: conversationRounds(historyToTimeline(turnMessagesWithImages(turn))),
  })) || [], [history, hasLiveRound, savedTurn?.turnId]);
  // One presentation owns the latest outcome: a stop line or a failure notice.
  // Older rounds retain their own status and logs.
  const summaryRound = rounds.at(-1) || (!hasLiveRound ? historyRounds.at(-1)?.rounds.at(-1) : undefined);
  const historicalSummaryItems = historicalAttemptItemIds(summaryRound?.items || [], execution);
  const summaryFailure = runSummary?.failure && summaryRound
    ? diagnoseRunFailure(traceForRun(runtimeRuns, runSummary.runId), execution,
      [...summaryRound.items.flatMap(item => item.type === "error" && !historicalSummaryItems.has(item.id) ? [item.content] : []),
        ...runSummary.failure.rawReasons.filter(reason => reason.startsWith("Review recovery pending:"))], runSummary.status === "stopped")
    : runSummary?.failure;
  const settledSummary = runSummary && summaryFailure ? { ...runSummary, failure: summaryFailure, reasons: summaryFailure.rawReasons } : runSummary;
  const headerFailure = runStatus === "failed" ? summaryFailure
    || failureForRound(summaryRound?.items || [], runtimeRuns, execution) : null;
  const projectChecks = projectChecksForRounds([
    ...historyRounds.flatMap(({ turn, rounds: restored }) => restored.map(round => ({ key: promptAnchorId(turn.turnId, round), items: round.items }))),
    ...rounds.map((round, index) => ({ key: promptAnchorId("live", round), items: round.items,
      runId: hasLiveRound && index === rounds.length - 1 ? run?.runId : undefined })),
  ], runtimeRuns);
  const summaryHasInlineCheck = Boolean(runSummary?.runId && [...projectChecks.values()].some(check =>
    check.runId === runSummary.runId || check.webRunId === runSummary.runId));
  const summaryIsRotation = isContextRotationRequest(summaryRound?.user?.content || "");
  const summaryCard = !loading && settledSummary && !(settledSummary.failure?.kind === "canceled" && summaryRound)
    ? <RunSummaryCard summary={summaryChanges && !summaryChanges.partial ? { ...settledSummary, changes: [] } : settledSummary} loading={summaryLoading} standalone={standalone}
        sessionId={session?.sessionId || run?.sessionId}
        showProjectCheck={!summaryHasInlineCheck || Boolean(settledSummary.failure && settledSummary.failure.kind !== "canceled")}
        onOpenChanges={onOpenChanges} onOpenRuntime={onOpenRuntime} recoveryAction={recoveryAction} onOpenSettings={onOpenSettings} />
    : null;
  const navigationEntries = [
    ...historyRounds.flatMap(({ turn, rounds: restored }) => restored.filter(round => round.user)
      .map(round => conversationAnchor(promptAnchorId(turn.turnId, round), round))),
    ...rounds.filter(round => round.user).map(round => conversationAnchor(promptAnchorId("live", round), round)),
  ];
  const showNavigation = !loading && navigationEntries.length > 0;
  const forkUnavailable = Boolean(session?.worktreeId && session.worktreeStatus !== "ready");
  const panelAgents = agentConversations([
    ...historyRounds.flatMap(({ turn, rounds: restored }, turnIndex) => restored.map((round, index) => ({
      round, status: (turn.status === "failed" ? "failed" : turn.status === "stopped" ? "stopped" : "completed") as RunStatus,
      execution: !hasLiveRound && turnIndex === historyRounds.length - 1 && index === restored.length - 1 ? execution : null,
    }))),
    ...rounds.map((round, index) => ({ round, status: index === rounds.length - 1 ? runStatus : "completed" as RunStatus,
      execution: index === rounds.length - 1 ? execution : null })),
  ], runtimeRuns);

  function openAgent(key: string, trigger: HTMLButtonElement) {
    followingRef.current = false;
    agentTrigger.current = trigger;
    setAgentSelection({ stream: streamKey, key });
    onOpenAgentPanel();
  }

  function closeAgent() {
    onCloseAgentPanel();
    agentTrigger.current?.focus({ preventScroll: true });
  }

  function captureReadingView(): ConversationViewState | undefined {
      const element = scrollRef.current;
      if (!element || loading) return;
      const top = element.getBoundingClientRect().top;
      const anchors = Array.from(element.querySelectorAll<HTMLElement>(".user-message[id], .context-rotation-process > summary[id]"));
      const anchor = anchors.find(item => item.getBoundingClientRect().bottom >= top) || anchors.at(-1);
      return { anchorId: anchor?.id, offset: anchor ? anchor.getBoundingClientRect().top - top : 0,
        scrollTop: element.scrollTop, following: followingRef.current,
        expanded: Array.from(element.querySelectorAll<HTMLElement>(".round-process[open][data-process-turn], .round-inline-process[data-expanded='true'][data-process-turn]"))
          .map(item => item.dataset.processTurn!).filter(Boolean),
        details: Array.from(element.querySelectorAll<HTMLElement>("details[open][data-detail-id]"))
          .map(item => item.dataset.detailId!) };
  }

  useEffect(() => {
    if (!captureViewRef) return;
    captureViewRef.current = captureReadingView;
    return () => { captureViewRef.current = null; };
  }, [captureViewRef, streamKey, loading]);

  function saveReadingView() {
    window.cancelAnimationFrame(viewFrame.current);
    viewFrame.current = window.requestAnimationFrame(() => {
      const view = captureReadingView();
      if (view) onViewChange?.(view);
    });
  }

  function recordProcessInteraction() {
    followingRef.current = false;
    saveReadingView();
  }

  function loadEarlier() {
    if (loading || historyLoading || earlierScrollRef.current || !onLoadEarlier) return;
    earlierScrollRef.current = captureReadingView() || null;
    followingRef.current = false;
    onLoadEarlier();
  }

  useEffect(() => () => window.cancelAnimationFrame(viewFrame.current), [streamKey]);

  useLayoutEffect(() => {
    const switchedStream = streamKeyRef.current !== streamKey;
    streamKeyRef.current = streamKey;
    const previousItems = timelineRef.current;
    timelineRef.current = items;
    if (switchedStream) {
      pendingView.current = restoredView;
      followingRef.current = restoredView?.following ?? true;
      earlierScrollRef.current = null;
      setAwayFromBottom(false);
      setHasNewProgress(false);
    }
    if (loading) return;
    // Restored history and metadata refreshes are not newly received progress.
    const previousById = new Map(previousItems.map(item => [item.id, item]));
    const receivedProgress = !switchedStream && items !== previousItems && items.some(item => {
      if (item.timestamp <= 0 || item.type === "user") return false;
      const previous = previousById.get(item.id);
      if (!previous || previous.type !== item.type) return true;
      if ("content" in item && "content" in previous && item.content !== previous.content) return true;
      if ("status" in item && "status" in previous && item.status !== previous.status) return true;
      if (item.type === "tool" && previous.type === "tool") return item.output !== previous.output;
      if (item.type === "assistant" && previous.type === "assistant") return item.deliveryState !== previous.deliveryState;
      return item.type === "approval" && previous.type === "approval" && item.resolved !== previous.resolved;
    });
    if (receivedProgress && !followingRef.current) setHasNewProgress(true);
    const element = scrollRef.current;
    if (pendingView.current && element && history?.turns.length) {
      const view = pendingView.current;
      const anchor = view.anchorId ? document.getElementById(view.anchorId) : null;
      followingRef.current = view.following;
      element.scrollTop = view.following ? element.scrollHeight : anchor
        ? element.scrollTop + anchor.getBoundingClientRect().top - element.getBoundingClientRect().top - view.offset
        : view.scrollTop || 0;
      setAwayFromBottom(!view.following);
      pendingView.current = undefined;
      return;
    }
    if (earlierScrollRef.current && !historyLoading && element) {
      const view = earlierScrollRef.current;
      const anchor = view.anchorId ? document.getElementById(view.anchorId) : null;
      element.scrollTop = anchor
        ? element.scrollTop + anchor.getBoundingClientRect().top - element.getBoundingClientRect().top - view.offset
        : view.scrollTop || 0;
      earlierScrollRef.current = null;
      return;
    }
    if (earlierScrollRef.current) return;
    if (followingRef.current) {
      // Streaming tokens must not restart a smooth scroll animation or move the page itself.
      if (element) element.scrollTop = element.scrollHeight;
      setHasNewProgress(false);
      return;
    }
  }, [items, streamKey, loading, runSummary, summaryLoading, history, historyLoading]);

  function followLatest() {
    followingRef.current = true;
    setAwayFromBottom(false);
    setHasNewProgress(false);
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: reducedMotion ? "auto" : "smooth" });
  }

  function jumpToItem(id: string) {
    followingRef.current = false;
    const focusTarget = () => {
      const target = document.getElementById(id);
      target?.scrollIntoView({ block: "center", behavior: "auto" });
      target?.focus({ preventScroll: true });
    };
    const rotation = scrollRef.current?.querySelector<HTMLDetailsElement>(".conversation-turn.is-active > .context-rotation-process");
    if (!document.getElementById(id) && rotation && !rotation.open) {
      rotation.querySelector<HTMLElement>(":scope > summary")?.click();
      window.requestAnimationFrame(focusTarget);
    } else focusTarget();
  }

  return (
    <FileNavigationContext.Provider value={{ workspace, onOpenFile }}>
    <QuestionSession run={run} items={items} onAnswer={onAnswerQuestion} activeId={activeQuestionId} openRequest={questionOpenRequest}>
    <main className="run-column conversation-surface" id="main-content">
      <header className={`run-header ${!hasSession ? "is-new-task" : ""} ${session?.sessionId ? "has-chat-actions" : ""} ${session?.sessionId || onOpenTerminal ? "has-header-actions" : ""}`}>
        <button className="icon-button mobile-only" onClick={onOpenRail} aria-label="打开任务栏">
          <Menu size={19} />
        </button>
        {railCollapsed && <button className="icon-button desktop-only" onClick={onOpenRail} aria-label="展开侧栏" title="展开侧栏">
          <PanelLeft size={19} />
        </button>}
        <div>
          <strong className="run-title" title={taskTitle}>{hasSession ? taskTitle || "任务记录" : "新任务"}</strong>
          <span className="run-context" title={contextLabel}>{contextLabel}</span>
          <span className={`run-state state-${headerFailure?.kind === "verification" ? "stopped" : runStatus}`} aria-live="polite"><i />{runStatus === "running" && run?.pendingQuestions?.length ? "等待你的回答" : headerFailure?.kind === "verification" ? "验收待处理" : statusText(runStatus)}</span>
          {session?.worktreeId && <span className="worktree-badge" title={session.workspace}>Worktree · {session.worktreeStatus === "ready" ? "就绪" : `不可运行（${session.worktreeStatus || "缺失"}）`}</span>}
          {session?.forkedFromId && <span className="worktree-badge">分叉自 <button onClick={() => onSourceSession?.(session.forkedFromId!)}>{session.forkedFromId.slice(0, 12)}</button></span>}
        </div>
        {(session?.sessionId || onOpenTerminal) && <span className="run-header-actions">
          {session?.sessionId && <ChatActions key={session.sessionId} sessionId={session.sessionId} active={active} loading={loading} />}
          {onOpenTerminal && <button type="button" className="icon-button terminal-toggle" onClick={onOpenTerminal} aria-label="打开项目终端" title="打开项目终端">
            <TerminalSquare size={18} />
          </button>}
        </span>}
      </header>
      {!loading && active && <RunProgress run={run} status={runStatus} execution={execution} profile={actualProfile}
        items={items} reconnecting={reconnecting} onCompose={onCompose} onJump={jumpToItem} />}
      <div className={`conversation-viewport ${showNavigation ? "has-turn-navigation" : ""} ${!loading && active ? "has-run-activity" : ""} ${hasLiveChanges ? "has-file-changes" : ""}`}>
      <div
        className="stream-scroll"
        ref={scrollRef}
        onScroll={() => {
          const element = scrollRef.current;
          if (!element) return;
          const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight <= 48;
          followingRef.current = atBottom;
          setAwayFromBottom(!atBottom);
          if (atBottom) setHasNewProgress(false);
          if (earlierScrollRef.current) earlierScrollRef.current = captureReadingView() || earlierScrollRef.current;
          saveReadingView();
        }}
      >
        <div className={`stream-content ${!loading && items.length === 0 && !runSummary && !hasHistory ? "is-empty" : ""}`}>
          {!loading && !active && hasHistoricalImages && imageInputStatus !== "supported" && <p className="composer-image-note" role="status">
            {imageInputStatus === "unsupported" ? "当前模型不支持图片输入。" : "当前模型的图片输入能力尚未确认。"}
            历史图片仍可预览，继续对话时仅参考已有文字。
            <button onClick={onOpenSettings}>切换模型</button>
          </p>}
          {!loading && historyError && <p className="worktree-error" role="alert">{historyError}</p>}
          {!loading && history?.nextBefore != null && <>
            <PageSentinel key={streamKey} cursor={history.nextBefore} busy={Boolean(historyLoading)} error={historyError} edge="top" onLoad={loadEarlier} />
            {historyLoading && <p className="page-loading" role="status">正在读取更早轮次…</p>}
            {historyError && <button className="history-load" disabled={historyLoading} onClick={loadEarlier}>重试读取更早轮次</button>}
          </>}
          {!loading && historyRounds.map(({ turn, rounds: restored }, turnIndex) => <section className="history-round" key={turn.turnId} data-turn-id={turn.turnId}>
            {restored.map((round, index) => <ConversationTurn key={`${streamKey}-${round.id}`} round={round} active={false}
              initiallyOpen={restoredView?.expanded.includes(turn.turnId)} details={restoredView?.details} onExpanded={recordProcessInteraction} onProcessPage={onProcessPage} onLoadProcessPage={onLoadProcessPage}
              onOpenAgent={openAgent}
              runtimeRuns={runtimeRuns} summary={!hasLiveRound && turnIndex === historyRounds.length - 1 && index === restored.length - 1 ? settledSummary : null}
              summaryCard={summaryIsRotation && !hasLiveRound && turnIndex === historyRounds.length - 1 && index === restored.length - 1 ? summaryCard : undefined}
              recoveryAction={!hasLiveRound && turnIndex === historyRounds.length - 1 && index === restored.length - 1 ? recoveryAction : undefined}
              onOpenRuntime={!hasLiveRound && turnIndex === historyRounds.length - 1 && index === restored.length - 1 ? onOpenRuntime : undefined}
              sessionId={session?.sessionId || run?.sessionId} projectCheck={projectChecks.get(promptAnchorId(turn.turnId, round))}
              anchorId={promptAnchorId(turn.turnId, round)} forkTurn={index === restored.length - 1 ? turn : undefined}
              forkUnavailable={forkUnavailable} onFork={onFork}
              fileChanges={index === restored.length - 1 ? turn.fileChanges : undefined}
              undoEligible={!active && !hasLiveRound && turn.turnId === latestHistory?.turnId && turn.status === "completed"}
              onReview={path => turn.fileChanges && reviewChanges(turn.fileChanges, path)}
              status={turn.status === "failed" ? "failed" : turn.status === "stopped" ? "stopped" : "completed"}
              elapsedMs={restored.length === 1 ? turnElapsedMs(turn) : undefined}
              execution={!hasLiveRound && turnIndex === historyRounds.length - 1 && index === restored.length - 1 ? execution : null} onApproval={onApproval} />)}
          </section>)}
          {loading ? (
            <div className="stream-loading" aria-live="polite" aria-label="正在恢复会话">
              <span />
              <span />
              <span />
            </div>
          ) : items.length === 0 && !runSummary && !hasHistory ? (
            <EmptyStream standalone={standalone} contextLabel={contextLabel} onPromptSuggestion={onPromptSuggestion} />
          ) : (
            rounds.map((round, index) => <ConversationTurn key={`${streamKey}-${round.id}`} round={round}
              initiallyOpen={restoredView?.expanded.includes(`live:${round.id}`)} details={restoredView?.details} onExpanded={recordProcessInteraction}
              onOpenAgent={openAgent}
              runtimeRuns={runtimeRuns} summary={index === rounds.length - 1 ? settledSummary : null}
              summaryCard={summaryIsRotation && index === rounds.length - 1 ? summaryCard : undefined}
              recoveryAction={index === rounds.length - 1 ? recoveryAction : undefined}
              onOpenRuntime={index === rounds.length - 1 ? onOpenRuntime : undefined}
              sessionId={session?.sessionId || run?.sessionId} projectCheck={projectChecks.get(promptAnchorId("live", round))}
              anchorId={promptAnchorId("live", round)} forkTurn={index === rounds.length - 1 && hasLiveRound ? savedTurn : undefined}
              forkUnavailable={forkUnavailable} onFork={onFork}
              fileChanges={index === rounds.length - 1 ? liveChanges : undefined}
              undoEligible={!active && runStatus === "completed" && index === rounds.length - 1}
              onReview={path => liveChanges && reviewChanges(liveChanges, path, savedTurn?.fileChanges?.turnId !== liveChanges.turnId)}
              active={active && index === rounds.length - 1} continuing={active && index >= currentRoundStart && index < rounds.length - 1}
              status={(active && index >= currentRoundStart) || index === rounds.length - 1 ? runStatus : "completed"}
              reconnecting={reconnecting}
              elapsedMs={rounds.length === 1 ? turnElapsedMs(savedTurn) : undefined}
              execution={(active && index >= currentRoundStart) || index === rounds.length - 1 ? execution : null} onApproval={onApproval} />)
          )}
          {!summaryIsRotation && summaryCard}
          {!loading && hasSession && <DevelopmentServices key={streamKey} workspace={workspace}
            sessionId={session?.sessionId || run?.sessionId} refreshKey={`${run?.runId || ""}:${runStatus}`} />}
          <div ref={endRef} />
        </div>
      </div>
      {!loading && <RunActivity key={`${streamKey}:${run?.runId || ""}`} run={run} status={runStatus} execution={execution}
        changesBadge={hasLiveChanges && run?.fileChanges ? <RunChangesBadge changes={run.fileChanges} onReview={() => reviewChanges(run.fileChanges!, undefined, true)} /> : undefined}
        items={items} reconnecting={reconnecting} awayFromBottom={awayFromBottom} hasNewProgress={hasNewProgress}
        onFollowLatest={followLatest} onCompose={onCompose} onJump={jumpToItem} />}
      {showNavigation && <ConversationNavigator key={streamKey} entries={navigationEntries} scrollRef={scrollRef} onNavigate={id => {
        const scroll = scrollRef.current;
        const target = document.getElementById(id);
        if (!scroll || !target) return;
        followingRef.current = false;
        scroll.scrollTo({ top: scroll.scrollTop + target.getBoundingClientRect().top - scroll.getBoundingClientRect().top - 20, behavior: "instant" });
        target.focus({ preventScroll: true });
      }} />}
      </div>
      {!loading && review?.stream === streamKey && <RunChangesReview key={`${review.source.turnId}:${review.source.runId || "saved"}`} source={review.source} onClose={() => setReview(null)} />}
      {agentPanelTarget && createPortal(<AgentPanel key={streamKey} agents={loading ? [] : panelAgents}
        selected={agentSelection?.stream === streamKey ? agentSelection.key : null}
        onSelect={key => setAgentSelection({ stream: streamKey, key })} onClose={closeAgent}
        onApproval={id => { closeAgent(); jumpToItem(`approval-${id}`); }}
        renderItems={(task, taskItems, process) => <ConversationItems items={taskItems} active={task.active} process={process} scopedAgent
          recovered={task.recovered} execution={task.execution} onApproval={onApproval} />}
        renderAnswer={content => <AssistantMessage item={{ content }} />} />, agentPanelTarget)}
    </main>
    </QuestionSession></FileNavigationContext.Provider>
  );
}
