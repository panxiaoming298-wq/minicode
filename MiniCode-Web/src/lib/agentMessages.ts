import type { ExecutionSnapshot, TimelineItem } from "../types";
import type { ToolTimelineItem } from "./runSummary";
import { agentMessageLabel, agentOwner } from "./agentActivity";

export function isAgentCommunicationTool(item: Pick<ToolTimelineItem, "toolName">): boolean {
  return ["send_agent_message", "submit_consultation", "submit_builder_candidate", "accept_builder_patch"]
    .includes(item.toolName.replace(/^.*[.:/]/, ""));
}

interface AgentMessagePresentation {
  action: string; title: string; state: string; detail: string; messageId: string;
  recipient: string; kind: string; content: string; report: string; reason: string;
  receipt?: string;
  replyLabel?: string;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function field(value: Record<string, unknown>, key: string): string {
  return typeof value[key] === "string" ? value[key] : "";
}

export function consultationFormatRejected(item: ToolTimelineItem): boolean {
  return item.toolName === "submit_consultation" && item.status === "error"
    && /^(?:NotExecuted: (?:summary must fit|report:|summary:|verdict:|report must contain|submit_consultation accepts|paged consultation)|Input validation error in submit_consultation:)/.test(item.output);
}

/** A native handoff resolves format rejections in this activation, not review findings. */
export function correctedConsultationIds(items: TimelineItem[]): Set<string> {
  const pending = new Map<string, ToolTimelineItem[]>();
  const corrected = new Set<string>();
  for (const item of items) {
    if (item.type === "user") { pending.clear(); continue; }
    if (item.type !== "tool" || item.toolName !== "submit_consultation"
      || !item.executionId || !item.nodeId || !item.attemptId || !item.toolCallId) continue;
    const key = JSON.stringify([item.executionId, item.nodeId, item.attemptId]);
    if (consultationFormatRejected(item)) {
      pending.set(key, [...(pending.get(key) || []), item]);
    } else if (item.status === "success") {
      try {
        const receipt = JSON.parse(item.output);
        if (receipt.status !== "consultation_submitted" || receipt.delivered !== false
          || typeof receipt.handoff_id !== "string" || !receipt.handoff_id
          || !/^[a-f0-9]{64}$/.test(receipt.report_sha256 || "")) continue;
        for (const failed of pending.get(key) || []) {
          if (failed.toolCallId !== item.toolCallId && failed.resultOrder !== undefined
            && item.startOrder !== undefined && failed.resultOrder < item.startOrder) corrected.add(failed.id);
        }
        pending.delete(key);
      } catch { /* Text, a missing receipt or a rejected draft is not a successful handoff. */ }
    }
  }
  return corrected;
}

export function agentMessageView(item: ToolTimelineItem, execution?: ExecutionSnapshot | null, recovered = false): AgentMessagePresentation {
  const input = record(item.input);
  let receipt: Record<string, unknown> = {};
  try { receipt = record(JSON.parse(item.output)); } catch { /* Old or failed calls can have plain-text results. */ }
  if (item.nodeId && field(receipt, "sender_id") && receipt.sender_id !== item.nodeId
    || item.executionId && field(receipt, "run_id") && receipt.run_id !== item.executionId
    || field(input, "message_id") && field(receipt, "message_id") && input.message_id !== receipt.message_id) receipt = {};

  const toolName = item.toolName.replace(/^.*[.:/]/, "");
  if (toolName === "submit_consultation" || toolName === "submit_builder_candidate") {
    const builder = toolName === "submit_builder_candidate";
    const subject = builder ? "修改" : "报告";
    const action = builder ? "向父代理提交修改" : "向父代理提交咨询报告";
    const corrected = !builder && recovered && consultationFormatRejected(item);
    const state = corrected ? "corrected" : item.status === "running" ? "sending" : item.status === "error" ? "failed"
      : item.status === "unknown" ? "unknown" : "submitted";
    const content = field(input, "summary") || field(input, "report");
    const report = field(input, "report");
    const verdict = !builder && state === "submitted" && receipt.status === "consultation_submitted"
      && receipt.delivered === false && receipt.verdict === input.verdict ? field(receipt, "verdict") : "";
    const verdictLabel = ({ passed: "复核意见：通过", failed: "复核意见：发现问题", unavailable: "复核意见：无法完成" } as Record<string, string>)[verdict];
    return {
      action, state, recipient: "父代理", kind: builder ? "修改提交" : "咨询报告", content,
      report: report && report !== content ? report : "",
      messageId: item.toolCallId || field(receipt, "handoff_id") || item.id,
      title: corrected ? "咨询报告格式已纠正" : state === "sending" ? `正在${action}` : state === "failed" ? `${action}失败`
        : state === "unknown" ? `${action} · 结果未确认` : `已${action}`,
      // Both handoff receipts deliberately have delivered:false; submission is not final acceptance.
      detail: corrected ? "这次提交未执行；同一子代理随后已成功提交修正后的报告。原始错误保留，最终验收另行记录。"
        : state === "sending" ? `正在提交${subject}` : state === "failed" ? `${subject}未提交成功`
        : state === "unknown" ? `未收到${subject}提交结果`
          : builder ? "修改已提交供父代理审阅；应用和最终验收结果另行记录。"
            : `${verdictLabel ? verdictLabel + "。" : ""}咨询报告已提交；最终验收结果另行记录。`,
      reason: item.status === "error" ? item.output : "",
    };
  }

  if (toolName === "accept_builder_patch") {
    const recovery = input.recover === true;
    const action = recovery ? "请求子代理重新生成修改" : "接收子代理修改";
    // The native receipt is plain text. Its first field confirms application even if a later check failed.
    // Never infer application from a quoted receipt, a final task state, or a successful recovery request.
    const applied = !recovery && (receipt.applied === true || /^applied=True(?:\s|$)/.test(item.output));
    const state = item.status === "running" ? "sending" : item.status === "unknown" ? "unknown"
      : applied ? item.status === "error" ? "warning" : "applied"
        : item.status === "error" ? "failed" : recovery ? "sent" : "unknown";
    return {
      action, state, recipient: recovery ? "子代理" : "父代理", kind: recovery ? "重新生成" : "修改接收",
      content: field(input, "patch_id") ? `补丁：${field(input, "patch_id")}` : "补丁标识未记录。", report: "",
      messageId: item.toolCallId || item.id,
      title: state === "sending" ? `正在${action}` : state === "failed" ? `${action}失败`
        : state === "unknown" ? `${action} · 结果未确认`
          : state === "warning" ? "已接收子代理修改 · 检查未通过" : `已${action}`,
      detail: state === "sending" ? "正在处理请求" : state === "unknown" ? "未收到可确认的处理结果。"
        : state === "failed" ? "请求未成功，请展开执行回执查看原因。"
          : recovery ? "已请求重新生成；这次调用未应用旧补丁。"
            : state === "warning" ? "修改已应用到父代理的候选版本，但后续检查未通过；已应用的修改仍保留。"
              : "修改已应用到父代理的候选版本；最终验收结果另行记录。",
      reason: "", receipt: item.output,
    };
  }

  const sender = item.nodeId || field(receipt, "sender_id");
  const messageId = field(input, "message_id") || field(receipt, "message_id");
  const requestedRecipient = field(input, "recipient_id");
  const resolvedRecipient = field(receipt, "recipient_id");
  const ownsResolution = field(receipt, "recipient_handle") === requestedRecipient
    && (!item.executionId || execution?.executionId === item.executionId)
    && execution?.agentTasks?.some(task => task.sessionId === requestedRecipient && task.taskId === resolvedRecipient);
  const recipient = ownsResolution ? resolvedRecipient : requestedRecipient || resolvedRecipient;
  const parent = recipient === "parent" || recipient === "primary";
  const scopedExecution = !item.executionId || execution?.executionId === item.executionId ? execution : null;
  const recipientOwner = agentOwner({ nodeId: recipient }, scopedExecution);
  const child = !parent && (sender === "primary" || recipientOwner.kind === "child");
  const destination = parent ? "父代理" : child ? "子代理" : "代理";
  const action = `向${destination}发送消息`;
  // A failed invocation must not inherit a successful receipt from an earlier use of the same message ID.
  const saved = scopedExecution?.agentMessages?.find(message => message.message_id === messageId
    && message.sender_id === sender && message.recipient_id === (parent ? "primary" : recipient));
  const delivery = saved?.state || field(receipt, "state");
  const state = item.status === "running" ? "sending" : item.status === "unknown" ? "unknown"
    : item.status === "error" ? field(receipt, "state") === "rejected" ? "rejected" : "failed"
      : ["queued", "consumed", "rejected"].includes(delivery) ? delivery : "sent";
  const title = state === "sending" ? `正在${action}`
    : state === "failed" || state === "rejected" ? `${action}失败`
      : state === "unknown" ? `${action} · 结果未确认` : `已${action}`;
  const detail = ({
    sending: "正在发送", queued: "已发送，等待对方接收", consumed: "对方已接收，已进入上下文",
    sent: "已发送，接收状态未记录", rejected: "对方未接收", failed: "发送失败", unknown: "未收到发送结果",
  } as Record<string, string>)[state];
  const kind = field(input, "kind") || field(receipt, "kind");
  const replyTo = field(input, "reply_to") || field(receipt, "reply_to") || saved?.reply_to;
  const original = replyTo && scopedExecution?.agentMessages?.find(message => `${message.sender_id}:${message.message_id}` === replyTo);
  return {
    action, title, state, detail, messageId,
    recipient: parent ? "父代理" : recipient ? agentMessageLabel(recipient, scopedExecution) : "接收方未记录",
    kind: ({ progress: "进度", question: "问题", result: "结果", instruction: "指令" } as Record<string, string>)[kind] || "消息",
    content: field(input, "content") || field(receipt, "content"),
    report: "",
    reason: field(receipt, "reason") || (item.status === "error" ? item.output : ""),
    replyLabel: original ? `回复 ${agentMessageLabel(original.sender_id, scopedExecution)} 的${original.kind === "question" ? "问题" : "消息"}` : replyTo ? "回复此前消息" : undefined,
  };
}
