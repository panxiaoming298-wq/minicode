import type { RunEvent, StoredMessage, TimelineItem } from "../types";

function eventId(event: RunEvent): string {
  return `event-${event.sequence}`;
}

export function limitLiveTimeline(
  items: TimelineItem[],
  maxItems = 512,
): TimelineItem[] {
  const liveIndexes = items
    .map((item, index) => (/^event-\d+$/.test(item.id) ? index : -1))
    .filter((index) => index >= 0);
  const excess = liveIndexes.length - Math.max(1, maxItems);
  if (excess <= 0) return items;

  const discarded = new Set(liveIndexes.slice(0, excess));
  const result: TimelineItem[] = [];
  let noticeInserted = false;
  items.forEach((item, index) => {
    if (discarded.has(index)) {
      if (!noticeInserted) {
        result.push({
          id: "event-retention-notice",
          type: "progress",
          content: "较早的实时运行细节已折叠；完整对话结果仍保存在会话记录中。",
          timestamp: item.timestamp,
          status: "complete",
          source: "runtime",
        });
        noticeInserted = true;
      }
      return;
    }
    result.push(item);
  });
  return result;
}

function settledProgressContent(
  content: string,
  status: "complete" | "failed" | "stopped",
): string {
  const subject = content.replace(/^正在/, "").replace(/[。…]+$/, "");
  if (status === "failed") return `${subject}失败`;
  if (status === "stopped") return `${subject}已停止`;
  return `${subject}完成`;
}

function settleProgressItems(
  items: TimelineItem[],
  status: "complete" | "failed" | "stopped" = "complete",
): TimelineItem[] {
  return items.map((item) => {
    if (item.type === "progress" && item.status === "running") {
      return {
        ...item,
        content: settledProgressContent(item.content, status),
        status,
      };
    }
    return item;
  });
}

function settleTransientItems(
  items: TimelineItem[],
  status: "complete" | "failed" | "stopped",
): TimelineItem[] {
  return settleProgressItems(items, status).map((item) => {
    if (item.type === "assistant" && item.streaming) return { ...item, streaming: false };
    if (item.type === "tool" && item.status === "running") {
      return { ...item, status: status === "complete" ? "success" : "error" };
    }
    return item;
  });
}

export function historyToTimeline(messages: StoredMessage[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  const pendingByUseId = new Map<string, number>();
  messages.forEach((message, index) => {
    const content = typeof message.content === "string" ? message.content : "";
    if (message.role === "user" && content) {
      items.push({ id: `history-user-${index}`, type: "user", content, timestamp: 0 });
      return;
    }
    if (message.role === "assistant" && content) {
      if (content.startsWith("Runtime completion gate rejected delivery:")) {
        items.push({ id: `history-error-${index}`, type: "error", content, timestamp: 0 });
        return;
      }
      items.push({ id: `history-assistant-${index}`, type: "assistant", content, timestamp: 0 });
      return;
    }
    if (message.role === "assistant_tool_call") {
      const itemIndex = items.length;
      items.push({
        id: `history-tool-${index}`,
        type: "tool",
        toolName: message.toolName || "tool",
        input: message.input,
        output: "",
        timestamp: 0,
        status: "running",
      });
      if (message.toolUseId) pendingByUseId.set(message.toolUseId, itemIndex);
      return;
    }
    if (message.role === "tool_result") {
      let target = message.toolUseId ? pendingByUseId.get(message.toolUseId) ?? -1 : -1;
      if (target < 0) {
        for (let cursor = items.length - 1; cursor >= 0; cursor -= 1) {
          const candidate = items[cursor];
          if (
            candidate.type === "tool"
            && candidate.status === "running"
            && (!message.toolName || candidate.toolName === message.toolName)
          ) {
            target = cursor;
            break;
          }
        }
      }
      const output = content || message.output || "";
      if (target >= 0) {
        const candidate = items[target];
        if (candidate.type === "tool") {
          items[target] = {
            ...candidate,
            output,
            status: message.isError ? "error" : "success",
          };
        }
      } else {
        items.push({
          id: `history-tool-result-${index}`,
          type: "tool",
          toolName: message.toolName || "tool",
          input: null,
          output,
          timestamp: 0,
          status: message.isError ? "error" : "success",
        });
      }
      if (message.toolUseId) pendingByUseId.delete(message.toolUseId);
    }
  });
  return items;
}

function runtimeProgressContent(event: RunEvent): string {
  const runtime = event.runtime;
  const category = runtime?.category || "runtime";
  const phaseLabel = {
    explore: "分析",
    execute: "执行",
    verify: "核验",
  }[runtime?.phase || ""] || runtime?.phase || "";
  const verificationLabel = {
    light: "基础",
    normal: "常规",
    strict: "严格",
  }[runtime?.verification_focus || ""] || runtime?.verification_focus || "";
  const label = {
    phase: "运行阶段",
    compaction: "上下文整理",
    guard: "结果检查",
    widening: "执行扩展",
    recovery: "恢复处理",
    stop: "停止条件",
  }[category] || "运行时状态";
  const phase = phaseLabel ? ` · ${phaseLabel}` : "";
  const detail = category === "phase"
    ? verificationLabel ? `验证重点：${verificationLabel}` : ""
    : category === "stop"
      ? runtime?.stop_reason || event.stopReason || "已满足停止条件"
      : runtime?.widening_reason || runtime?.evidence_summary || runtime?.message || event.content || "";
  return detail ? `${label}${phase} — ${detail}` : `${label}${phase}`;
}

export function reduceRunEvent(items: TimelineItem[], event: RunEvent): TimelineItem[] {
  if (
    event.kind === "status"
    && (event.status === "completed" || event.status === "failed" || event.status === "stopped")
  ) {
    const settled = event.status === "completed" ? "complete" : event.status;
    return settleTransientItems(items, settled);
  }

  if (
    event.kind === "thinking_status"
    && items.some((item) => item.type === "progress" && item.source === "thinking")
  ) {
    return items;
  }

  const currentItems = event.kind === "progress" || event.kind === "thinking_status"
    ? items
    : settleProgressItems(items);

  if (event.kind === "assistant_stream") {
    const existingIndex = currentItems.findIndex((item) => item.type === "assistant" && item.streaming);
    if (existingIndex >= 0) {
      return currentItems.map((item, index) =>
        index === existingIndex && item.type === "assistant"
          ? { ...item, content: item.content + (event.content || "") }
          : item,
      );
    }
    return [
      ...currentItems,
      {
        id: eventId(event),
        type: "assistant",
        content: event.content || "",
        timestamp: event.timestamp,
        streaming: true,
      },
    ];
  }

  if (event.kind === "assistant") {
    const streamingIndex = currentItems.findIndex((item) => item.type === "assistant" && item.streaming);
    const nextItem: TimelineItem = {
      id: eventId(event),
      type: "assistant",
      content: event.content || "",
      timestamp: event.timestamp,
    };
    if (streamingIndex >= 0) {
      return currentItems.map((item, index) => (index === streamingIndex ? nextItem : item));
    }
    return [...currentItems, nextItem];
  }

  if (event.kind === "tool_start") {
    return [
      ...currentItems,
      {
        id: eventId(event),
        type: "tool",
        toolName: event.toolName || "tool",
        input: event.input,
        output: "",
        timestamp: event.timestamp,
        status: "running",
        step: event.step,
      },
    ];
  }

  if (event.kind === "tool_result") {
    let target = -1;
    for (let index = currentItems.length - 1; index >= 0; index -= 1) {
      const item = currentItems[index];
      if (item.type === "tool" && item.status === "running" && item.toolName === event.toolName) {
        target = index;
        break;
      }
    }
    if (target < 0) return currentItems;
    return currentItems.map((item, index) =>
      index === target && item.type === "tool"
        ? {
            ...item,
            output: event.output || "",
            status: event.isError ? "error" : "success",
            durationMs: event.durationMs,
          }
        : item,
    );
  }

  if (event.kind === "progress" || event.kind === "thinking_status" || event.kind === "runtime") {
    const content = event.kind === "runtime" ? runtimeProgressContent(event) : event.content || "";
    if (!content) return items;
    const last = items.at(-1);
    if (
      event.kind === "progress"
      && /^Runtime phase:/i.test(content)
      && last?.type === "progress"
      && last.status === "running"
      && last.content.startsWith("运行阶段")
    ) return items;
    if (last?.type === "progress" && last.content === content && last.status === "running") return items;
    return [
      ...settleProgressItems(items),
      {
        id: eventId(event),
        type: "progress",
        content,
        timestamp: event.timestamp,
        status: "running",
        source: event.kind === "thinking_status"
          ? "thinking"
          : event.kind === "runtime"
            ? "runtime"
            : "progress",
      },
    ];
  }

  if (event.kind === "approval_requested" && event.requestId && event.request) {
    return [
      ...currentItems,
      {
        id: eventId(event),
        type: "approval",
        requestId: event.requestId,
        request: event.request,
        timestamp: event.timestamp,
      },
    ];
  }

  if (event.kind === "approval_resolved" && event.requestId) {
    return currentItems.map((item) =>
      item.type === "approval" && item.requestId === event.requestId
        ? { ...item, resolved: event.decision || "resolved" }
        : item,
    );
  }

  if (event.kind === "error" || event.kind === "event_gap") {
    return [
      ...currentItems,
      {
        id: eventId(event),
        type: "error",
        content: event.message || event.content || "任务运行失败。",
        timestamp: event.timestamp,
      },
    ];
  }

  return currentItems;
}
