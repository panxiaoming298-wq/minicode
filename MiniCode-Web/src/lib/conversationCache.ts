import type { SessionInspect, SessionSummary, SessionTurn, StoredMessage, TurnsPage } from "../types";

/** Merge pages from one immutable turn without downgrading already loaded fields. */
export function mergeProcessMessages(current: StoredMessage[], incoming: StoredMessage[]): StoredMessage[] {
  const messages = new Map(current.map(message => [message.displayIndex, message]));
  for (const message of incoming) {
    const previous = messages.get(message.displayIndex);
    const merged = { ...message };
    const truncated = new Set(message.truncatedFields);
    for (const field of ["content", "output", "input", "toolRecovery", "commandResult"] as const) {
      if (previous && truncated.has(field) && !previous.truncatedFields?.includes(field) && previous[field] !== undefined) {
        Object.assign(merged, { [field]: previous[field] });
        truncated.delete(field);
      }
    }
    if (truncated.size) merged.truncatedFields = [...truncated];
    else delete merged.truncatedFields;
    messages.set(message.displayIndex, merged);
  }
  return [...messages.values()].sort((left, right) => (left.displayIndex || 0) - (right.displayIndex || 0));
}

export interface ConversationViewState {
  scrollTop?: number;
  anchorId?: string;
  offset: number;
  following: boolean;
  expanded: string[];
  details?: string[];
}

export interface CachedConversation {
  session: SessionSummary;
  history: TurnsPage;
  inspect: SessionInspect | null;
  view?: ConversationViewState;
}

/** Per-app LRU: large histories cannot crowd out the browser's working memory. */
export class ConversationCache {
  private entries = new Map<string, { value: CachedConversation; bytes: number }>();
  private bytes = 0;
  constructor(private maxEntries = 5, private maxBytes = 16 * 1024 * 1024) {}

  get(id: string): CachedConversation | undefined {
    const entry = this.entries.get(id);
    if (!entry) return undefined;
    this.entries.delete(id);
    this.entries.set(id, entry);
    return entry.value;
  }

  set(id: string, value: CachedConversation) {
    const view = value.view ?? this.entries.get(id)?.value.view;
    const next = { ...value, view };
    const bytes = new TextEncoder().encode(JSON.stringify(next)).byteLength;
    this.delete(id);
    if (bytes > this.maxBytes) return;
    this.entries.set(id, { value: next, bytes });
    this.bytes += bytes;
    while (this.entries.size > this.maxEntries || this.bytes > this.maxBytes) {
      this.delete(this.entries.keys().next().value!);
    }
  }

  saveView(id: string, view: ConversationViewState) {
    const entry = this.entries.get(id);
    if (entry) {
      // Scroll updates do not stringify the (potentially large) transcript again.
      const size = (value: ConversationViewState | undefined) => value ? new TextEncoder().encode(JSON.stringify(value)).byteLength : 0;
      const delta = size(view) - size(entry.value.view);
      entry.value.view = view;
      entry.bytes += delta; this.bytes += delta;
      while (this.bytes > this.maxBytes) this.delete(this.entries.keys().next().value!);
    }
  }

  retain(ids: Set<string>) {
    for (const id of this.entries.keys()) if (!ids.has(id)) this.delete(id);
  }

  delete(id: string) {
    const previous = this.entries.get(id);
    if (previous) this.bytes -= previous.bytes;
    this.entries.delete(id);
  }
}

/** Keep loaded immutable process pages when only session/run metadata changes. */
export function mergeConversationPage(current: TurnsPage | null, page: TurnsPage, earlier = false): TurnsPage {
  if (!current) return page;
  if (!earlier && page.revision === current.revision) return current;
  if (page.total < current.total) return page;
  const known = new Map(current.turns.map(turn => [turn.turnId, turn]));
  if (!earlier && !page.turns.some(turn => known.has(turn.turnId))) return page;
  const merged = (next: SessionTurn): SessionTurn => {
    const previous = known.get(next.turnId);
    if (!previous?.processMessages || !next.processAvailable || !next.displayRevision
      || previous.displayRevision !== next.displayRevision) return next;
    return { ...next, processMessages: previous.processMessages,
      nextProcessAfter: previous.nextProcessAfter, processTotal: previous.processTotal };
  };
  const fresh = new Map(page.turns.map(turn => [turn.turnId, merged(turn)]));
  const retained = current.turns.map(turn => fresh.get(turn.turnId) || turn);
  const added = page.turns.filter(turn => !known.has(turn.turnId));
  return { ...page, turns: earlier ? [...added, ...retained] : [...retained, ...added],
    nextBefore: earlier ? page.nextBefore : current.nextBefore };
}
