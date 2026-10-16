import { ChevronRight, CircleAlert, LoaderCircle, MessageCircle } from "lucide-react";
import type { ExecutionSnapshot } from "../types";
import type { ToolTimelineItem } from "../lib/runSummary";
import { agentMessageView } from "../lib/agentMessages";
import "./AgentMessageItem.css";

export function AgentMessageItem({ item, execution, recovered = false }: { item: ToolTimelineItem; execution?: ExecutionSnapshot | null; recovered?: boolean }) {
  const message = agentMessageView(item, execution, recovered);
  return <details className={`agent-message-item is-${message.state}`} data-message-id={message.messageId}>
    <summary>
      {message.state === "sending" ? <LoaderCircle size={16} className="spin" aria-hidden="true" />
        : ["failed", "rejected", "unknown", "warning"].includes(message.state) ? <CircleAlert size={16} aria-hidden="true" />
          : <MessageCircle size={16} aria-hidden="true" />}
      <span>{message.title}</span>
      <ChevronRight size={14} className="agent-message-chevron" aria-hidden="true" />
    </summary>
    <div className="agent-message-detail">
      <div className="agent-message-meta"><span>{message.recipient}</span><span>{message.kind}</span></div>
      {message.replyLabel && <p className="agent-message-delivery">{message.replyLabel}</p>}
      <p className="agent-message-body">{message.content || "未保存消息正文。"}</p>
      {message.report && <details className="agent-message-report">
        <summary>完整报告<ChevronRight size={13} aria-hidden="true" /></summary>
        <p className="agent-message-body">{message.report}</p>
      </details>}
      <p className="agent-message-delivery" role="status">{message.detail}</p>
      {message.reason && <p className="agent-message-reason">{message.reason}</p>}
      {message.receipt && <details className="agent-message-report">
        <summary>执行回执<ChevronRight size={13} aria-hidden="true" /></summary>
        <p className="agent-message-body">{message.receipt}</p>
      </details>}
    </div>
  </details>;
}
