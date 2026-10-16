import type { ConversationScope } from "../types";
import { mergeAttachments, validateAttachments, type Attachment } from "./attachments";

export interface DraftContext {
  sessionId: string | null;
  scope: ConversationScope;
  workspace: string;
}

interface DraftEntry {
  value: string;
  attachments: Attachment[];
  revision: number;
  saved: boolean;
}

type DraftStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
const PREFIX = "minicode.draft.v1:";
const ACTIVE_KEY = "minicode.draft.active";

function normalizePath(path: string): string {
  const normalized = path.replace(/\\/g, "/").replace(/\/+$/, "");
  return /^[a-z]:\//i.test(normalized) || normalized.startsWith("//")
    ? normalized.toLocaleLowerCase()
    : normalized;
}

export function draftKey(context: DraftContext): string {
  if (context.sessionId) return `${PREFIX}session:${context.sessionId}`;
  return context.scope === "project"
    ? `${PREFIX}project:${normalizePath(context.workspace)}`
    : `${PREFIX}standalone`;
}

export class DraftStore {
  private entries = new Map<string, DraftEntry>();

  constructor(private storage: DraftStorage | null) {}

  read(key: string): DraftEntry {
    const cached = this.entries.get(key);
    if (cached) return cached;
    let value = "";
    let attachments: Attachment[] = [];
    let saved = Boolean(this.storage);
    try {
      const raw = this.storage?.getItem(key);
      if (raw) {
        const data: unknown = JSON.parse(raw);
        if (data && typeof data === "object" && "value" in data && typeof data.value === "string") {
          value = data.value;
          if ("attachments" in data && Array.isArray(data.attachments)) {
            validateAttachments(data.attachments);
            attachments = data.attachments;
          }
        }
      }
    } catch { saved = false; }
    const entry = { value, attachments, saved, revision: 0 };
    this.entries.set(key, entry);
    return entry;
  }

  write(key: string, value: string, attachments = this.read(key).attachments): DraftEntry {
    validateAttachments(attachments);
    const entry = { value, attachments, saved: false, revision: this.read(key).revision + 1 };
    this.entries.set(key, entry);
    try {
      if (this.storage) {
        if (value || attachments.length) this.storage.setItem(key, JSON.stringify({ value, attachments }));
        else this.storage.removeItem(key);
        entry.saved = true;
      }
    } catch { /* Keep the full text in memory when browser storage is unavailable. */ }
    return entry;
  }

  consume(key: string, revision: number) {
    // An old response may acknowledge a task after the user has edited this draft again.
    if (this.read(key).revision !== revision) return;
    this.write(key, "", []);
  }

  /** Carry an unsent message; keep the source draft and any existing target text. */
  carry(sourceKey: string, target: DraftContext): { value: string; attachments: Attachment[]; removed: number } {
    const key = draftKey(target);
    const source = this.read(sourceKey);
    const current = this.read(key);
    if (key === sourceKey) return { ...source, removed: 0 };
    const local = source.attachments.filter(file => file.source === "local");
    return {
      value: !source.value || current.value.includes(source.value) ? current.value
        : source.value.includes(current.value) ? source.value
        : current.value ? `${source.value}\n\n${current.value}` : source.value,
      attachments: mergeAttachments(local, current.attachments),
      removed: source.attachments.length - local.length,
    };
  }

  remember(context: DraftContext) {
    try { this.storage?.setItem(ACTIVE_KEY, JSON.stringify(context)); }
    catch { /* Drafts remain usable in the current page. */ }
  }

  lastContext(includeEmptySession = false): DraftContext | null {
    try {
      const raw = this.storage?.getItem(ACTIVE_KEY);
      if (!raw) return null;
      const context: unknown = JSON.parse(raw);
      if (!context || typeof context !== "object"
        || !("sessionId" in context) || !("scope" in context) || !("workspace" in context)
        || (context.sessionId !== null && typeof context.sessionId !== "string")
        || !["standalone", "project"].includes(String(context.scope))
        || typeof context.workspace !== "string") return null;
      const target = context as DraftContext;
      const entry = this.read(draftKey(target));
      return entry.value || entry.attachments.length || (includeEmptySession && target.sessionId) ? target : null;
    } catch { return null; }
  }
}
