import type { RunEvent, StoredMessage, TimelineItem } from "../types";

function eventId(event: RunEvent): string {
  return `event-${event.sequence}`;
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
  return messages.flatMap((message, index): TimelineItem[] => {
    const content = typeof message.content === "string" ? message.content : "";
    if (message.role === "user" && content) {
      return [{ id: `history-user-${index}`, type: "user", content, timestamp: 0 }];
    }
    if (message.role === "assistant" && content) {
      return [{ id: `history-assistant-${index}`, type: "assistant", content, timestamp: 0 }];
    }
    return [];
  });
}

export function reduceRunEvent(items: TimelineItem[], event: RunEvent): TimelineItem[] {
  if (
    event.kind === "status"
    && (event.status === "completed" || event.status === "failed" || event.status === "stopped")
  ) {
    const settled = event.status === "completed" ? "complete" : event.status;
    return settleTransientItems(items, settled);
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

  if (event.kind === "progress" || event.kind === "thinking_status") {
    const content = event.content || "";
    if (!content) return items;
    const last = items.at(-1);
    if (last?.type === "progress" && last.content === content && last.status === "running") return items;
    return [
      ...settleProgressItems(items),
      { id: eventId(event), type: "progress", content, timestamp: event.timestamp, status: "running" },
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

  if (event.kind === "error") {
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
