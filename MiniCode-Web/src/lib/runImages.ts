import type { ImageAvailability, RunImage, SessionTurn, StoredMessage, TimelineItem, TimelineOwner } from "../types";

export type DisplayImageStatus = ImageAvailability | "failed";
export function imageStatusLabel(status: DisplayImageStatus): string {
  return { available: "图片可查看", cleared: "图片已清理", missing: "图片文件缺失",
    corrupt: "图片文件损坏", failed: "图片加载失败" }[status];
}

export function imageGroupSummary(images: RunImage[], statuses: Record<string, DisplayImageStatus>): string {
  const counts = { available: 0, cleared: 0, missing: 0, corrupt: 0, failed: 0 };
  for (const image of images) {
    const status = statuses[imageUrl(image)] || image.availability;
    if (status && Object.hasOwn(counts, status)) counts[status]++;
  }
  return [`本轮图片 ${images.length} 张`, counts.available && `${counts.available} 张可查看`,
    counts.cleared && `${counts.cleared} 张已清理`, (counts.missing + counts.corrupt) && `${counts.missing + counts.corrupt} 张不可用`,
    counts.failed && `${counts.failed} 张加载失败`].filter(Boolean).join(" · ");
}

export function validRunImages(value: unknown): RunImage[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is RunImage => item && typeof item === "object"
    && typeof item.id === "string" && /^artifact-[a-f0-9]{32}$/.test(item.id)
    && typeof item.sessionId === "string" && /^[A-Za-z0-9_-]+$/.test(item.sessionId)
    && typeof item.name === "string" && typeof item.caption === "string"
    && ["image/png", "image/jpeg", "image/webp", "image/gif"].includes(item.mediaType)
    && Number.isSafeInteger(item.size) && item.size > 0
    && Number.isFinite(item.width) && item.width > 0 && Number.isFinite(item.height) && item.height > 0
    && Number.isFinite(item.createdAt));
}

export function imageUrl(image: RunImage, download = false): string {
  return `/api/sessions/${encodeURIComponent(image.sessionId)}/images/${encodeURIComponent(image.id)}${download ? "?download=true" : ""}`;
}

export function imageProducerKey(item: TimelineOwner & { toolCallId?: string }): string | null {
  return item.toolCallId ? JSON.stringify([item.executionId || "", item.nodeId || "", item.attemptId || "", item.toolCallId]) : null;
}

/** Images stay beside their exact producer, including delayed/replayed events. */
export function positionRunImages(items: TimelineItem[]): TimelineItem[] {
  const producers = new Map<string, TimelineItem[]>();
  const groups = new Map<string, Extract<TimelineItem, { type: "images" }>>();
  for (const item of items) {
    if (item.type !== "tool" && item.type !== "images") continue;
    const key = imageProducerKey(item);
    if (!key) continue;
    if (item.type === "tool") producers.set(key, [...(producers.get(key) || []), item]);
    else {
      const group = groups.get(key);
      groups.set(key, group ? { ...group, images: [...group.images, ...item.images] } : item);
    }
  }
  const inserted = new Set<string>();
  const result: TimelineItem[] = [];
  for (const item of items) {
    if (item.type === "tool") {
      result.push(item);
      const key = imageProducerKey(item);
      if (key && producers.get(key)?.length === 1 && groups.has(key)) {
        result.push(groups.get(key)!); inserted.add(key);
      }
    } else if (item.type === "images") {
      const key = imageProducerKey(item);
      if (!key || (producers.get(key)?.length || 0) > 1) result.push(item);
      else if (!producers.has(key) && !inserted.has(key)) {
        result.push(groups.get(key)!); inserted.add(key);
      }
    } else result.push(item);
  }
  return result;
}

export function appendRunImages(items: TimelineItem[], value: unknown, timestamp = 0, position = true): TimelineItem[] {
  const seen = new Set(items.flatMap(item => item.type === "images" ? item.images.map(image => `${image.sessionId}:${image.id}`) : []));
  const fresh = validRunImages(value).filter(image => {
    const key = `${image.sessionId}:${image.id}`;
    if (seen.has(key)) return false;
    seen.add(key); return true;
  });
  if (!fresh.length) return items;
  const result = [...items];
  for (const image of fresh) {
    const entry: TimelineItem = { id: `image-${image.sessionId}-${image.id}`,
      type: "images", images: [image], timestamp: timestamp || image.createdAt,
      executionId: image.runId, nodeId: image.nodeId, attemptId: image.attemptId,
      toolCallId: image.toolCallId, toolName: image.toolName };
    result.push(entry);
  }
  return position ? positionRunImages(result) : result;
}

/** Saved inputs and inline records stay visible independently of process pagination. */
export function turnMessagesWithImages(turn: SessionTurn): StoredMessage[] {
  if (!turn.processMessages) return turn.displayMessages;
  const messages = turn.processMessages;
  const result = [...messages];
  for (const message of turn.displayMessages) if (message.role === "user") {
    const index = result.findIndex(candidate => candidate.role === "user" && (
      message.displayIndex != null && candidate.displayIndex === message.displayIndex
      || message._userInputId && candidate._userInputId === message._userInputId
      || message.displayIndex == null && !message._userInputId && !candidate._userInputId
        && (candidate.displayIndex == null || candidate.displayIndex === 0)));
    if (index >= 0) result[index] = { ...message, displayIndex: result[index].displayIndex ?? message.displayIndex };
    else if (message.displayIndex == null && !message._userInputId) result.unshift(message);
    else {
      const later = result.findIndex(candidate => message.displayIndex != null && candidate.displayIndex != null
        && candidate.displayIndex > message.displayIndex);
      if (later < 0) result.push(message);
      else result.splice(later, 0, message);
    }
  }
  for (const message of turn.displayMessages) if (message.role === "tool_result" && message.toolName === "record_task_checks") {
    const index = result.findIndex(candidate => candidate.role === message.role && candidate.toolUseId === message.toolUseId
      && candidate.executionId === message.executionId && candidate.nodeId === message.nodeId && candidate.attemptId === message.attemptId);
    if (index < 0) {
      const answer = result.findIndex(candidate => candidate.role === "assistant"
        && (candidate.displayIndex === turn.answerIndex || candidate.phase === "final_answer"));
      if (answer < 0) result.push(message);
      else result.splice(answer, 0, message);
    }
    else if (result[index].truncatedFields?.length) result[index] = message;
  }
  // A partially loaded work transcript must not hide saved questions or their
  // answers. Summary events carry the complete text and are replayed once.
  for (const message of turn.displayMessages) if (message.role === "run_event"
    && ["question_requested", "question_resolved"].includes(message.event?.kind || "")) {
    const index = result.findIndex(candidate => candidate.role === "run_event"
      && candidate.event?.requestId === message.event?.requestId && candidate.event?.kind === message.event?.kind);
    if (index >= 0) result[index] = { ...message, displayIndex: result[index].displayIndex ?? message.displayIndex };
    else {
      const later = result.findIndex(candidate => message.displayIndex != null && candidate.displayIndex != null
        && candidate.displayIndex > message.displayIndex);
      const answer = result.findIndex(candidate => candidate.role === "assistant"
        && (candidate.displayIndex === turn.answerIndex || candidate.phase === "final_answer"));
      const before = later >= 0 ? later : answer;
      if (before < 0) result.push(message);
      else result.splice(before, 0, message);
    }
  }
  if (turn.answerIndex != null) {
    const answer = turn.displayMessages.find(message => message.role === "assistant"
      && (message.displayIndex == null || message.displayIndex === turn.answerIndex));
    if (answer) {
      const index = result.findIndex(message => message.role === "assistant" && message.displayIndex === turn.answerIndex);
      if (index < 0) result.push(answer);
      // Process pages contain bounded previews. The saved answer is complete
      // and must stay complete when a background preload reaches its position.
      else if (result[index].truncatedFields?.includes("content")) result[index] = { ...answer, displayIndex: turn.answerIndex };
    }
  }
  for (const message of turn.displayMessages) if (message.role === "tool_image") {
    const images = validRunImages(message.images);
    if (images.length) result.push({ ...message, images });
  }
  return result;
}

type ImageProcessSection = { type: "process"; id: string; items: TimelineItem[] }
  | { type: "question"; id: string; item: Extract<TimelineItem, { type: "progress" }> }
  | { type: "image"; id: string; item: Extract<TimelineItem, { type: "images" }>;
      producer?: Extract<TimelineItem, { type: "tool" }> };

/** Keep images and questions at their original positions while surrounding work can collapse. */
export function imageProcessSections(items: TimelineItem[]): ImageProcessSection[] {
  const sections: ImageProcessSection[] = [];
  let pending: TimelineItem[] = [];
  const flush = () => {
    if (pending.length) sections.push({ type: "process", id: pending[0].id, items: pending });
    pending = [];
  };
  for (const item of positionRunImages(items)) {
    if (item.type === "progress" && item.questionId && item.question) {
      flush();
      sections.push({ type: "question", id: item.id, item });
      continue;
    }
    if (item.type !== "images") { pending.push(item); continue; }
    const previous = pending.at(-1);
    const key = imageProducerKey(item);
    // Ambiguous or unloaded producers must not borrow another call's result.
    const producer = key && previous?.type === "tool" && imageProducerKey(previous) === key
      && items.filter(candidate => candidate.type === "tool" && imageProducerKey(candidate) === key).length === 1
      ? previous : undefined;
    if (producer) pending.pop();
    flush();
    sections.push({ type: "image", id: item.id, item, producer });
  }
  flush();
  return sections;
}

export function imagesInTimeline(items: TimelineItem[]): RunImage[] {
  const unique = new Map<string, RunImage>();
  for (const item of items) if (item.type === "images")
    for (const image of item.images) unique.set(`${image.sessionId}:${image.id}`, image);
  return [...unique.values()];
}
