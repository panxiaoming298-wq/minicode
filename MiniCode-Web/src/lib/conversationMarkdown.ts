import { api } from "../api/client";
import type { SessionTurn, StoredMessage } from "../types";
import { messagePreview, parseMessage } from "./attachments";

function inlineText(value: string): string {
  return value.replace(/\s+/g, " ").trim().replace(/[\\`*_{}[\]<>#|]/g, "\\$&");
}

function userMarkdown(content: string): string {
  const { text, attachments } = parseMessage(content);
  return [text, ...attachments.map(file => {
    if (file.kind === "image") return `图片附件：${inlineText(file.name)}`;
    const fence = "`".repeat(Math.max(3, ...Array.from(file.content.matchAll(/`+/g), match => match[0].length + 1)));
    return `### 附件：${inlineText(file.name)}\n\n${fence}text\n${file.content}\n${fence}`;
  })].filter(Boolean).join("\n\n");
}

export interface ConversationExport {
  title: string;
  turns: SessionTurn[];
}

/** Shared visible-message selection for Markdown and both PDF layouts. */
export function conversationMessage(message: StoredMessage): { role: "user" | "assistant"; content: string } | null {
  if (message.role === "user" && message.content) return { role: "user", content: message.content };
  if (message.role === "assistant" && message.content && message.phase !== "commentary"
    && message.deliveryState !== "rejected" && message.deliveryState !== "pending") {
    return { role: "assistant", content: message.content };
  }
  const event = message.role === "run_event" ? message.event : undefined;
  if (event?.kind === "question_requested" && event.question) {
    const options = event.question.options?.map((option, index) => `${index + 1}. ${option}`).join("\n");
    return { role: "assistant", content: `${event.question.question}${options ? `\n\n${options}` : ""}` };
  }
  if (event?.kind === "question_resolved" && event.resolution === "answered" && event.answer != null) {
    return { role: "user", content: event.answer };
  }
  return null;
}

function messageMarkdown(message: StoredMessage): string {
  const visible = conversationMessage(message);
  if (!visible) return "";
  return visible.role === "user" ? `## 用户\n\n${userMarkdown(visible.content)}` : `## MiniCode\n\n${visible.content}`;
}

/** Use the browser's user/answer projection, without internal prompts or tool traces. */
export function formatConversationMarkdown(title: string, turns: readonly SessionTurn[]): string {
  const messages = turns.flatMap(turn => turn.displayMessages.map(messageMarkdown)).filter(Boolean);
  if (!messages.length) throw new Error("当前聊天还没有可导出的消息。");
  return `# ${inlineText(messagePreview(title)) || "MiniCode 聊天"}\n\n${messages.join("\n\n---\n\n")}\n`;
}

/** Fetch every page independently of the reader's currently expanded history. */
export async function readConversationExport(sessionId: string, signal: AbortSignal): Promise<ConversationExport> {
  let page = await api.getConversationPage(sessionId, undefined, signal);
  const { revision, total, session } = page;
  const pages: SessionTurn[][] = [];
  const seen = new Set<string>();
  let before = total;
  while (true) {
    signal.throwIfAborted();
    if (page.revision !== revision || page.total !== total) {
      throw new Error("聊天记录正在更新，请稍后重试。");
    }
    for (const turn of page.turns) {
      if (["running", "queued", "stopping"].includes(turn.status)) {
        throw new Error("当前任务仍在进行，请在结束后复制或导出完整聊天。");
      }
      if (seen.has(turn.turnId) || !turn.displayRevision) {
        throw new Error("部分聊天记录无法完整读取，请刷新后重试。");
      }
      seen.add(turn.turnId);
    }
    pages.push(page.turns);
    if (page.nextBefore === null) break;
    if (!page.turns.length || page.nextBefore >= before) throw new Error("聊天历史分页异常，请刷新后重试。");
    before = page.nextBefore;
    page = await api.getConversationPage(sessionId, before, signal);
  }
  if (seen.size !== total) throw new Error("聊天记录未读取完整，请稍后重试。");
  const latest = await api.getConversationPage(sessionId, undefined, signal);
  if (latest.revision !== revision) throw new Error("聊天记录正在更新，请稍后重试。");
  signal.throwIfAborted();
  return { title: messagePreview(session.title || session.firstMessage || "MiniCode 聊天").replace(/\s+/g, " ").trim(),
    turns: pages.reverse().flat() };
}

export async function readConversationMarkdown(sessionId: string, signal: AbortSignal): Promise<string> {
  const conversation = await readConversationExport(sessionId, signal);
  return formatConversationMarkdown(conversation.title, conversation.turns);
}
