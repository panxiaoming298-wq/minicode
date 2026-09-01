import {
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
  hasSession: boolean;
  standalone: boolean;
  contextLabel: string;
  onPromptSuggestion: (prompt: string) => void;
  onApproval: (requestId: string, decision: string, feedback?: string) => Promise<void>;
  onOpenRail: () => void;
}

const approvalLabels: Record<string, string> = {
  allow_once: "允许一次",
  allow_turn: "本轮允许",
  allow_all_turn: "允许本轮所有修改",
  allow_always: "始终允许",
  deny_once: "拒绝一次",
  deny_with_feedback: "拒绝并说明",
  deny_always: "始终拒绝",
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

function ToolItem({ item }: { item: Extract<TimelineItem, { type: "tool" }> }) {
  return (
    <details className={`tool-item tool-${item.status}`}>
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
            <span>结果</span>
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
        <span>审批已处理：{approvalLabels[item.resolved] || item.resolved}</span>
      </div>
    );
  }

  const needsFeedback = item.request.choices.some((choice) => choice.decision === "deny_with_feedback");
  return (
    <section className="approval-card" aria-label="需要审批">
      <div className="approval-heading">
        <CircleAlert size={19} />
        <div>
          <strong>等待你确认</strong>
          <p>{item.request.summary}</p>
        </div>
      </div>
      <ul>
        {item.request.details.map((detail) => <li key={detail}>{detail}</li>)}
      </ul>
      {needsFeedback && (
        <label className="approval-feedback">
          <span>如果拒绝，可以告诉 MiniCode 原因</span>
          <textarea value={feedback} onChange={(event) => setFeedback(event.target.value)} rows={2} />
        </label>
      )}
      <div className="approval-actions">
        {item.request.choices.map((choice, index) => (
          <button
            key={`${choice.decision}-${choice.label}`}
            className={index === 0 ? "button button-primary" : "button button-secondary"}
            onClick={() => void decide(choice)}
            disabled={busy}
          >
            {approvalLabels[choice.decision] || choice.label}
          </button>
        ))}
      </div>
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
  hasSession,
  standalone,
  contextLabel,
  onPromptSuggestion,
  onApproval,
  onOpenRail,
}: RunStreamProps) {
  const endRef = useRef<HTMLDivElement>(null);
  const active = runStatus === "running" || runStatus === "queued" || runStatus === "stopping";

  useEffect(() => {
    if (active) endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [items, active]);

  return (
    <main className="run-column" id="main-content">
      <header className="run-header">
        <button className="icon-button mobile-only" onClick={onOpenRail} aria-label="打开任务栏">
          <Menu size={19} />
        </button>
        <div>
          <strong>{hasSession ? "任务记录" : "新任务"}</strong>
          <span className="run-context" title={contextLabel}>{contextLabel}</span>
          <span className={`run-state state-${runStatus}`}><i />{statusText(runStatus)}</span>
        </div>
      </header>
      <div className="stream-scroll">
        <div className="stream-content">
          {items.length === 0 ? (
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
      </div>
    </main>
  );
}
