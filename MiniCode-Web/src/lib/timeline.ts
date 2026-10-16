import type { RunEvent, StoredMessage, TimelineItem, TimelineOwner } from "../types";
import { formatStopReason } from "./runtime";
import { explainFailure } from "./runFailure";
import { appendRunImages, positionRunImages } from "./runImages";

const preparationProgress = /^(准备(?:工作区|辅助任务 [a-f0-9]+)：[^（]+)（已处理 (\d+) 个文件）(完成)?$/;
// Legacy native notices had no step receipt. Match only the exact runtime wording.
const completedRotationNotice = /^已自动整理历史上下文，最新要求和新增消息已保留。(?:继续执行任务。|正在检查恢复后的上下文容量。|新增内容仍较多，正在继续恢复。)$/;

function timelineOwner(source: TimelineOwner): TimelineOwner {
  return {
    ...(source.executionId !== undefined ? { executionId: source.executionId } : {}),
    ...(source.nodeId !== undefined ? { nodeId: source.nodeId } : {}),
    ...(source.attemptId !== undefined ? { attemptId: source.attemptId } : {}),
  };
}

function sameOwner(item: TimelineOwner, source: TimelineOwner): boolean {
  return item.executionId === source.executionId
    && item.nodeId === source.nodeId
    && item.attemptId === source.attemptId;
}

function errorIdentity(content: string): string {
  const text = content.trim();
  const unwrapped = text.replace(/^(?:ToolFailed|ProviderError):\s*/, "");
  // Runtime summaries wrap the original provider error. Keep request details
  // in the comparison, and never collapse unrelated tool failures by category.
  return unwrapped !== text && explainFailure(unwrapped)?.kind === "provider" ? unwrapped : text;
}

function repeatsLastError(items: TimelineItem[], content: string, owner: TimelineOwner): boolean {
  const previous = items.at(-1);
  // The worker can save one failure as both an error event and an assistant echo.
  return previous?.type === "error" && sameOwner(previous, owner)
    && errorIdentity(previous.content) === errorIdentity(content);
}

function eventId(event: RunEvent): string {
  return `event-${event.webRunId ? `${event.webRunId}-` : ""}${event.sequence}`;
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
  owner?: TimelineOwner,
): TimelineItem[] {
  return items.map((item) => {
    if (item.type === "progress" && item.questionId) return item;
    if (item.type === "progress" && item.status === "running" && (!owner || sameOwner(item, owner))) {
      // Older runs lack a preparation completion receipt. A later run failure
      // does not establish that this earlier workspace operation failed.
      if (status !== "complete" && preparationProgress.test(item.content)) {
        return { ...item, content: `${item.content}（未收到完成记录）`, status: "stopped" };
      }
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
    if (item.type === "assistant") return { ...item, streaming: false,
      deliveryState: item.deliveryState === "pending" ? "rejected" : item.deliveryState };
    if (item.type === "tool" && item.status === "running") {
      return { ...item, status: "unknown", output: item.output || "执行已结束，但未收到这次调用的结果。" };
    }
    return item;
  });
}

export function historyToTimeline(messages: StoredMessage[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  let status: RunEvent["status"];
  const pendingByUseId = new Map<string, number>();
  const callKey = (message: StoredMessage) => JSON.stringify([message.executionId, message.nodeId, message.attemptId, message.toolUseId]);
  const settle = (state: "complete" | "failed" | "stopped", owner?: TimelineOwner) => {
    items.splice(0, items.length, ...settleProgressItems(items, state, owner));
  };
  messages.forEach((message, index) => {
    if (message.role === "run_event" && message.event) {
      // Older snapshots copied the turn outcome into every event's status.
      status = message.event.turnStatus ?? message.event.status ?? status;
      const next = reduceRunEvent(items, { ...message.event, sequence: index + 1,
        webRunId: "history", timestamp: 0 });
      items.splice(0, items.length, ...next);
      return;
    }
    if (message.role === "tool_image") {
      // Keep pending tool indexes stable until every result has been matched.
      const fresh = appendRunImages(items, message.images, 0, false);
      items.splice(0, items.length, ...fresh);
      return;
    }
    const content = typeof message.content === "string" ? message.content : "";
    const owner = timelineOwner(message);
    if (message.role === "user" && content) {
      if (!message._userInputId) {
        settle(status === "failed" || status === "stopped" ? status : "complete");
        status = undefined;
      }
      // Summary inputs keep the same anchor when earlier process pages arrive.
      items.push({ id: message._userInputId ? `input-${message._userInputId}` : `history-user-${message.displayIndex ?? index}`,
        type: "user", content, timestamp: 0, ...owner,
        ...(message._userInputId ? { steeringInputId: message._userInputId } : {}) });
      return;
    }
    if (message.role === "assistant" && content) {
      if (message.deliveryState !== "accepted" && message.phase !== "commentary"
        && (/^(?:Runtime completion gate rejected delivery:|Required consultation failed; no further primary model calls or edits are permitted in this run\.)/.test(content)
          || explainFailure(content) !== null)) {
        if (repeatsLastError(items, content, owner)) return;
        items.push({ id: `history-error-${index}`, type: "error", content, timestamp: 0, ...owner });
        return;
      }
      settle("complete", owner);
      items.push({ id: `history-assistant-${index}`, type: "assistant", content, timestamp: 0, ...owner,
        messageId: message.messageId, deliveryState: message.deliveryState,
        ...(message.phase ? { phase: message.phase } : {}) });
      return;
    }
    if (message.role === "assistant_tool_call") {
      settle("complete", owner);
      const itemIndex = items.length;
      items.push({
        id: `history-tool-${index}`,
        type: "tool",
        toolName: message.toolName || "tool",
        input: message.input,
        output: "",
        timestamp: 0,
        status: "running",
        ...owner,
        toolCallId: message.toolUseId, startOrder: index,
        toolRecovery: message.toolRecovery,
      });
      if (message.toolUseId) pendingByUseId.set(callKey(message), itemIndex);
      return;
    }
    if (message.role === "tool_result") {
      settle("complete", owner);
      let target = message.toolUseId ? pendingByUseId.get(callKey(message)) ?? -1 : -1;
      if (target < 0 && !message.toolUseId) {
        for (let cursor = items.length - 1; cursor >= 0; cursor -= 1) {
          const candidate = items[cursor];
          if (
            candidate.type === "tool"
            && candidate.status === "running"
            && (!message.toolName || candidate.toolName === message.toolName)
            && (!message.executionId || candidate.executionId === message.executionId)
            && (!message.nodeId || candidate.nodeId === message.nodeId)
            && (!message.attemptId || candidate.attemptId === message.attemptId)
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
            commandResult: message.commandResult,
            commandCheckReport: message.commandCheckReport,
            resultOrder: index,
            toolInput: message.toolInput,
            toolRecovery: message.toolRecovery,
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
          commandResult: message.commandResult,
          commandCheckReport: message.commandCheckReport,
          ...owner,
          toolCallId: message.toolUseId,
          resultOrder: index,
          toolInput: message.toolInput,
          toolRecovery: message.toolRecovery,
        });
      }
      if (message.toolUseId) pendingByUseId.delete(callKey(message));
    }
  });
  const settled = settleProgressItems(items, status === "failed" || status === "stopped" ? status : "complete");
  return positionRunImages(settled.map(item => item.type === "approval" && !item.resolved
    ? { ...item, resolved: "history_only" } : item));
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
      ? formatStopReason(runtime?.stop_reason || event.stopReason)
      : runtime?.widening_reason || runtime?.evidence_summary || runtime?.message || event.content || "";
  return detail ? `${label}${phase} — ${detail}` : `${label}${phase}`;
}

export function reduceRunEvent(items: TimelineItem[], event: RunEvent): TimelineItem[] {
  if (event.kind === "tool_images") return appendRunImages(items, event.images, event.timestamp);
  if (event.kind === "run_prompt") {
    // A new submission already paints its user message before the host receipt.
    const last = items.at(-1);
    if (!event.content || last?.type === "user" && last.content === event.content) return items;
    return [...items, { id: `run-${event.webRunId}-user`, type: "user",
      content: event.content, timestamp: event.timestamp }];
  }
  // Snapshots and queue acknowledgements are persistent UI state, not messages.
  if (["execution_snapshot", "model_status", "history_saved", "input_capabilities"].includes(event.kind)) return items;
  const owner = timelineOwner(event);
  if (event.kind === "question_requested" && event.requestId && event.question) {
    if (items.some(item => item.type === "progress" && item.questionId === event.requestId)) return items;
    return [...items, { id: `question-${event.requestId}`, type: "progress", questionId: event.requestId,
      questionTurnId: event.turnId, questionRunId: event.webRunId,
      questionGroupId: event.questionGroupId, questionIndex: event.questionIndex, questionCount: event.questionCount,
      question: event.question,
      content: `等待你的回答：${event.question.question}`, timestamp: event.timestamp,
      status: "complete", source: "runtime", ...owner }];
  }
  if (event.kind === "question_resolved" && event.requestId) {
    return items.map(item => item.type === "progress" && item.questionId === event.requestId
      ? { ...item, questionAnswer: event.answer, questionResolution: event.resolution,
        content: `提问：${item.question?.question || item.content.replace(/^等待你的回答：/, "")}\n${event.resolution === "answered"
          ? `你的回答：${event.answer || ""}` : "任务已停止，问题未回答。"}`, status: "complete" } : item);
  }
  if (event.kind === "run_input") {
    const input = event.runInput;
    if (!input || input.status !== "applied" || items.some((item) => item.id === `input-${input.inputId}`)) return items;
    return [...items, { id: `input-${input.inputId}`, type: "user", timestamp: event.timestamp, ...owner,
      ...(input.kind === "steer" ? { steeringInputId: input.inputId } : {}),
      content: `${input.kind === "supplement" ? "补充要求：" : ""}${input.text}` }];
  }
  if (
    event.kind === "status"
    && (event.status === "completed" || event.status === "failed" || event.status === "stopped")
  ) {
    const settled = event.status === "completed" ? "complete" : event.status;
    return settleTransientItems(items, settled);
  }

  if (
    event.kind === "thinking_status"
    && items.some((item) => item.type === "progress" && item.source === "thinking" && sameOwner(item, owner))
  ) {
    return items;
  }

  let currentItems = event.kind === "progress" || event.kind === "thinking_status"
    ? items
    : settleProgressItems(items, "complete", owner);

  if (event.messageId && ["assistant_start", "assistant_stream", "assistant", "assistant_status"].includes(event.kind)) {
    if (event.replacesMessageId && event.phase === "final_answer" && event.deliveryState === "pending") {
      currentItems = currentItems.filter((item) => !(item.type === "assistant" && item.phase === "commentary"
        && item.messageId === event.replacesMessageId && sameOwner(item, owner)
        && (item.lastSequence ?? -1) < event.sequence));
    }
    let index = currentItems.findIndex((item) => item.type === "assistant"
      && item.messageId === event.messageId && sameOwner(item, owner));
    let primaryDeliveryReceipt = false;
    if (index < 0 && event.executionId && !event.nodeId && !event.attemptId
      && (event.kind === "assistant" || event.kind === "assistant_status")
      && event.phase === "final_answer"
      && (event.deliveryState === "accepted" || event.deliveryState === "rejected")) {
      // The run's delivery gate omits node/attempt coordinates on its final receipt.
      // Only an unambiguous primary answer can inherit that run-level decision.
      const primaryAnswers = currentItems.map((item, candidateIndex) => item.type === "assistant"
        && item.executionId === event.executionId && item.nodeId === "primary"
        && item.phase === "final_answer" && item.messageId === event.messageId ? candidateIndex : -1)
        .filter((candidateIndex) => candidateIndex >= 0);
      if (primaryAnswers.length === 1) {
        index = primaryAnswers[0];
        primaryDeliveryReceipt = true;
      }
    }
    const previous = index >= 0 ? currentItems[index] : undefined;
    const existing = previous?.type === "assistant" ? previous : undefined;
    // Replayed or late chunks cannot duplicate text or reopen an accepted answer.
    if (existing && (event.sequence <= (existing.lastSequence ?? -1)
      || existing.deliveryState === "accepted" && event.deliveryState !== "accepted")) return items;
    if (event.kind === "assistant_status" && !existing) return items;
    const next: Extract<TimelineItem, { type: "assistant" }> = {
      id: existing?.id || eventId(event), type: "assistant",
      ...timelineOwner(existing || event),
      messageId: event.messageId, lastSequence: event.sequence,
      timestamp: existing?.timestamp ?? event.timestamp,
      phase: event.phase ?? existing?.phase,
      deliveryState: event.deliveryState ?? existing?.deliveryState,
      streaming: event.kind === "assistant_start" || event.kind === "assistant_stream",
      content: event.kind === "assistant_stream" ? (existing?.content || "") + (event.content || "")
        : event.kind === "assistant_status" ? existing?.content || ""
          : event.content ?? (primaryDeliveryReceipt ? existing?.content || "" : ""),
    };
    return index < 0 ? [...currentItems, next] : currentItems.map((item, i) => i === index ? next : item);
  }

  if (event.kind === "assistant_stream") {
    const existingIndex = currentItems.findIndex((item) => item.type === "assistant"
      && item.streaming && !item.messageId && sameOwner(item, owner));
    if (existingIndex >= 0) {
      return currentItems.map((item, index) =>
        index === existingIndex && item.type === "assistant"
          ? { ...item, content: item.content + (event.content || ""), ...(event.phase ? { phase: event.phase } : {}) }
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
        ...owner,
        streaming: true,
        ...(event.phase ? { phase: event.phase } : {}),
      },
    ];
  }

  if (event.kind === "assistant") {
    const streamingIndex = currentItems.findIndex((item) => item.type === "assistant"
      && item.streaming && !item.messageId && sameOwner(item, owner));
    const nextItem: TimelineItem = {
      id: eventId(event),
      type: "assistant",
      content: event.content || "",
      timestamp: event.timestamp,
      ...owner,
      ...(event.phase ? { phase: event.phase } : {}),
    };
    if (streamingIndex >= 0) {
      return currentItems.map((item, index) => (index === streamingIndex && item.type === "assistant"
        ? { ...nextItem, phase: nextItem.phase || item.phase } : item));
    }
    return [...currentItems, nextItem];
  }

  if (event.kind === "tool_start") {
    return positionRunImages([
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
        ...owner,
        toolCallId: event.toolCallId,
        startOrder: event.sequence,
      },
    ]);
  }

  if (event.kind === "tool_rejected") {
    // Native admission rejection: there was no execution to start or finish.
    return positionRunImages([...currentItems, {
      id: eventId(event), type: "tool", toolName: event.toolName || "tool",
      input: event.input, output: event.output || "", timestamp: event.timestamp,
      status: "error", step: event.step, toolCallId: event.toolCallId,
      startOrder: event.sequence, resultOrder: event.sequence,
      toolRecovery: event.toolRecovery, ...owner,
    }]);
  }

  if (event.kind === "tool_recovery") {
    if (!event.toolCallId || !event.toolRecovery) return items;
    return currentItems.map(item => item.type === "tool" && item.toolCallId === event.toolCallId
      && item.step === event.toolRecovery!.step
      && item.toolName === event.toolName && item.executionId === event.executionId
      && item.nodeId === event.nodeId && item.attemptId === event.attemptId
      ? { ...item, toolRecovery: event.toolRecovery } : item);
  }

  if (event.kind === "tool_input") {
    // This annotates an existing native receipt, never its execution status.
    if (!event.toolCallId || !event.toolInput) return items;
    return currentItems.map(item => item.type === "tool" && item.toolCallId === event.toolCallId
      && item.step === event.toolInput!.step
      && item.toolName === event.toolName && item.executionId === event.executionId
      && item.nodeId === event.nodeId && item.attemptId === event.attemptId
      ? { ...item, toolInput: event.toolInput } : item);
  }

  if (event.kind === "tool_result") {
    let target = -1;
    for (let index = currentItems.length - 1; index >= 0; index -= 1) {
      const item = currentItems[index];
      if (item.type === "tool" && item.status === "running" && item.toolName === event.toolName
        && (!event.executionId || item.executionId === event.executionId)
        && (event.toolCallId
          ? item.toolCallId === event.toolCallId
            && (!event.attemptId || item.attemptId === event.attemptId)
            && (!event.nodeId || item.nodeId === event.nodeId)
          : !item.toolCallId && (!event.nodeId || item.nodeId === event.nodeId))) {
        target = index;
        break;
      }
    }
    if (target < 0) return positionRunImages([...currentItems, {
      id: eventId(event), type: "tool", toolName: event.toolName || "tool",
      input: null, output: event.output || "", timestamp: event.timestamp,
      status: event.isError ? "error" : "success", durationMs: event.durationMs,
      commandResult: event.commandResult,
      commandCheckReport: event.commandCheckReport,
      toolCallId: event.toolCallId, ...owner,
    }]);
    return currentItems.map((item, index) =>
      index === target && item.type === "tool"
        ? {
            ...item,
            output: event.output || "",
            status: event.isError ? "error" : "success",
            commandResult: event.commandResult,
            commandCheckReport: event.commandCheckReport,
            durationMs: event.durationMs,
            resultOrder: event.sequence,
          }
        : item,
    );
  }

  if (event.kind === "progress" || event.kind === "thinking_status" || event.kind === "runtime") {
    const content = event.kind === "runtime" ? runtimeProgressContent(event) : event.content || "";
    const stopReason = event.kind === "runtime" && event.runtime?.category === "stop"
      ? event.runtime.stop_reason || event.stopReason : undefined;
    // Submission is a completed handoff event, not a promise of final delivery.
    // A later failure of another node must not rewrite this recorded event.
    const submitted = ["builder_submitted", "candidate_submitted", "consultation_submitted", "done", "completed"].includes(stopReason || "");
    if (!content) return items;
    if (/^Runtime profile=/i.test(content)) return items;
    const last = items.at(-1);
    // Runtime also emits a plain progress callback for legacy consumers.
    // Collapse only its adjacent echo in the same activation and model step.
    if (event.kind === "progress" && last?.type === "progress" && last.runtimeProgressEcho
      && sameOwner(last, owner) && last.runtimeProgressEcho.step === event.step
      && last.runtimeProgressEcho.content === content) return items;
    // File counts are updates to one preparation phase, not completed work.
    // Keep the event log intact while avoiding dozens of near-identical rows.
    const update = event.kind === "progress" ? preparationProgress.exec(content) : null;
    // A blocked stop is already terminal; later settling must not append
    // another "失败" or "完成" to its recorded explanation.
    const progressStatus = stopReason === "blocked" ? "failed" : event.progressStatus || (submitted || update?.[3]
      || event.kind === "progress" && completedRotationNotice.test(content) ? "complete" : "running");
    const previous = last?.type === "progress" ? preparationProgress.exec(last.content) : null;
    if (update && previous && update[1] === previous[1]
      && last?.type === "progress" && last.status === "running" && sameOwner(last, owner)) {
      if (!update[3] && Number(update[2]) <= Number(previous[2])) return items;
      return [...items.slice(0, -1), { ...last, content, timestamp: event.timestamp,
        status: progressStatus }];
    }
    if (
      event.kind === "progress"
      && /^Runtime phase:/i.test(content)
      && last?.type === "progress"
      && sameOwner(last, owner)
      && last.status === "running"
      && last.content.startsWith("运行阶段")
    ) return items;
    if (last?.type === "progress" && sameOwner(last, owner)
      && last.content === content && last.status === "running") {
      return progressStatus === "running" ? items : [...items.slice(0, -1), { ...last, status: progressStatus }];
    }
    return [
      ...settleProgressItems(items, "complete", owner),
      {
        id: eventId(event),
        type: "progress",
        content,
        ...(stopReason ? { stopReason } : {}),
        ...(event.kind === "runtime" && event.runtime?.category === "progress" && typeof event.step === "number"
          ? { runtimeProgressEcho: { step: event.step, content: event.runtime.message || event.content || "" } } : {}),
        timestamp: event.timestamp,
        ...owner,
        status: progressStatus,
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
        ...owner,
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

  if (event.kind === "event_gap") {
    // A reconnect outside the retained event window loses live details, not
    // the task itself. Match the local retention notice and keep real errors
    // separate so an active run is never presented as a failed delivery.
    return [...currentItems, {
      id: eventId(event), type: "progress",
      content: event.message || event.content || "这次旧任务未保存较早的实时日志。已保存的对话和工具记录可在工作过程中查看。",
      timestamp: event.timestamp, status: "complete", source: "runtime", ...owner,
    }];
  }

  if (event.kind === "error") {
    const content = event.message || event.content || "任务运行失败。";
    if (repeatsLastError(currentItems, content, owner)) return currentItems;
    return [
      ...currentItems,
      {
        id: eventId(event),
        type: "error",
        content,
        timestamp: event.timestamp,
        ...owner,
      },
    ];
  }

  return currentItems;
}
