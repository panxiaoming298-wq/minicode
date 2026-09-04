import {
  ArrowDown,
  Check,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  LoaderCircle,
  Menu,
  TerminalSquare,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { ApprovalChoice, RunStatus, TimelineItem } from "../types";

interface RunStreamProps {
  items: TimelineItem[];
  runStatus: RunStatus;
  streamKey: string;
  loading: boolean;
  hasSession: boolean;
  standalone: boolean;
  contextLabel: string;
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

function friendlyToolError(output: string): string {
  const normalized = output.trim();
  if (!normalized) return "工具没有返回错误详情。";
  if (normalized.includes("WEB_SEARCH_UNAVAILABLE") || normalized.includes("No search results found")) {
    return "公共搜索入口没有返回可用结果，可能被限流或要求人机验证。";
  }
  if (normalized.includes("WEB_FETCH_TLS_ERROR") || /UNEXPECTED_EOF|SSL.*EOF/i.test(normalized)) {
    return "目标站点提前断开了安全连接；可以重试或换一个来源。";
  }
  if (normalized.includes("WEB_FETCH_NETWORK_ERROR")) {
    return "当前无法连接目标网址；请检查网络或换一个来源。";
  }
  const firstUsefulLine = normalized.split(/\r?\n/).find((line) => line.trim()) || normalized;
  return firstUsefulLine.slice(0, 180);
}

function resolvedApprovalText(item: Extract<TimelineItem, { type: "approval" }>): string {
  const decision = item.resolved || "";
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

function ToolItem({ item }: { item: Extract<TimelineItem, { type: "tool" }> }) {
  const detailsRef = useRef<HTMLDetailsElement>(null);
  const errorSummary = item.status === "error" ? friendlyToolError(item.output || "") : "";

  useEffect(() => {
    if (item.status === "error" && detailsRef.current) detailsRef.current.open = true;
  }, [item.status]);

  return (
    <details ref={detailsRef} className={`tool-item tool-${item.status}`}>
      <summary>
        <span className="tool-icon">
          {item.status === "running" ? (
            <LoaderCircle size={15} className="spin" />
          ) : item.status === "error" ? (
            <X size={15} />
          ) : (
            <Check size={15} />
          )}
        </span>
        <span className="tool-label">
          <strong>{item.toolName}</strong>
          <small>
            {item.status === "running"
              ? "正在执行"
              : item.status === "error"
                ? "执行失败"
                : item.durationMs
                  ? `${item.durationMs} ms`
                  : "已完成"}
          </small>
          {errorSummary && <span className="tool-error-summary">{errorSummary}</span>}
        </span>
        <ChevronRight size={15} className="tool-chevron" />
      </summary>
      <div className="tool-detail">
        <div>
          <span>输入</span>
          <pre>{JSON.stringify(item.input ?? {}, null, 2)}</pre>
        </div>
        {item.output && (
          <div>
            <span>{item.status === "error" ? "错误详情" : "结果"}</span>
            <pre>{item.output}</pre>
          </div>
        )}
      </div>
    </details>
  );
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
    <section className="approval-card" aria-label="需要审批">
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
  onPromptSuggestion,
}: {
  standalone: boolean;
  onPromptSuggestion: (prompt: string) => void;
}) {
  const suggestions = standalone
    ? [
        "帮我梳理一个想法，并给出可执行步骤",
        "解释一个技术概念，并用例子说明",
        "比较两个方案的优缺点和适用场景",
      ]
    : [
        "检查这个项目如何启动，并修复影响启动的问题",
        "阅读当前改动，找出可能的回归风险",
        "为这个项目补充缺失的测试和运行文档",
      ];
  return (
    <div className="empty-stream">
      <div className="empty-symbol" aria-hidden="true"><TerminalSquare size={24} /></div>
      <h1>{standalone ? "开始独立对话" : "把项目工作交给 MiniCode"}</h1>
      <p>
        {standalone
          ? "当前对话不绑定项目，不会读取项目文件。需要处理代码时，请在左侧项目下新建对话。"
          : "MiniCode 会读取当前项目并使用工具执行，每次修改和关键命令都会留下可检查的记录。"}
      </p>
      <div className="suggestion-list">
        {suggestions.map((suggestion) => (
          <button key={suggestion} onClick={() => onPromptSuggestion(suggestion)}>{suggestion}</button>
        ))}
      </div>
    </div>
  );
}

export function RunStream({
  items,
  runStatus,
  streamKey,
  loading,
  hasSession,
  standalone,
  contextLabel,
  onPromptSuggestion,
  onApproval,
  onOpenRail,
}: RunStreamProps) {
  const endRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const followingRef = useRef(true);
  const streamKeyRef = useRef(streamKey);
  const [pendingItems, setPendingItems] = useState(0);
  const active = runStatus === "running" || runStatus === "queued" || runStatus === "stopping";

  useEffect(() => {
    const switchedStream = streamKeyRef.current !== streamKey;
    streamKeyRef.current = streamKey;
    if (switchedStream || items.length === 0) followingRef.current = true;
    if (followingRef.current) {
      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      endRef.current?.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "end" });
      setPendingItems(0);
      return;
    }
    setPendingItems((current) => current + 1);
  }, [items, streamKey]);

  function followLatest() {
    followingRef.current = true;
    setPendingItems(0);
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    endRef.current?.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "end" });
  }

  return (
    <main className="run-column" id="main-content">
      <header className="run-header">
        <button className="icon-button mobile-only" onClick={onOpenRail} aria-label="打开任务栏">
          <Menu size={19} />
        </button>
        <div>
          <strong>{hasSession ? "任务记录" : "新任务"}</strong>
          <span className="run-context" title={contextLabel}>{contextLabel}</span>
          <span className={`run-state state-${runStatus}`} aria-live="polite"><i />{statusText(runStatus)}</span>
        </div>
      </header>
      <div
        className="stream-scroll"
        ref={scrollRef}
        onScroll={() => {
          const element = scrollRef.current;
          if (!element) return;
          const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight <= 48;
          followingRef.current = atBottom;
          if (atBottom) setPendingItems(0);
        }}
      >
        <div className="stream-content">
          {loading ? (
            <div className="stream-loading" aria-live="polite" aria-label="正在恢复会话">
              <span />
              <span />
              <span />
            </div>
          ) : items.length === 0 ? (
            <EmptyStream standalone={standalone} onPromptSuggestion={onPromptSuggestion} />
          ) : (
            items.map((item) => {
              if (item.type === "user") {
                return <div className="user-message" key={item.id}>{item.content}</div>;
              }
              if (item.type === "assistant") {
                return (
                  <article className={`assistant-message ${item.streaming ? "is-streaming" : ""}`} key={item.id}>
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        a: ({ children, ...props }) => <a {...props} target="_blank" rel="noreferrer">{children}</a>,
                      }}
                    >
                      {item.content}
                    </ReactMarkdown>
                  </article>
                );
              }
              if (item.type === "tool") return <ToolItem item={item} key={item.id} />;
              if (item.type === "approval") {
                return <ApprovalItem item={item} onApproval={onApproval} key={item.id} />;
              }
              if (item.type === "progress") {
                return (
                  <div className={`progress-item progress-${item.status}`} key={item.id}>
                    {item.status === "running" ? (
                      <LoaderCircle size={14} className={active ? "spin" : ""} />
                    ) : item.status === "complete" ? (
                      <Check size={14} />
                    ) : item.status === "failed" ? (
                      <CircleAlert size={14} />
                    ) : (
                      <X size={14} />
                    )}
                    <span>{item.content}</span>
                  </div>
                );
              }
              return (
                <div className="error-item" key={item.id}>
                  <CircleAlert size={16} />
                  <span>{item.content}</span>
                </div>
              );
            })
          )}
          <div ref={endRef} />
        </div>
        {pendingItems > 0 && (
          <button className="new-items-button" onClick={followLatest}>
            <ArrowDown size={14} />
            {pendingItems} 条新进展
          </button>
        )}
      </div>
    </main>
  );
}
