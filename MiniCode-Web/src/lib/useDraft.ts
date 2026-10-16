import { useCallback, useReducer, useRef, useState } from "react";

import { draftKey, DraftStore, type DraftContext } from "./drafts";
import type { Attachment } from "./attachments";

export function useDraft(context: DraftContext) {
  const [store] = useState(() => {
    try { return new DraftStore(window.localStorage); }
    catch { return new DraftStore(null); }
  });
  const [, refresh] = useReducer((value: number) => value + 1, 0);
  const key = draftKey(context);
  const activeKey = useRef(key);
  activeKey.current = key;
  const entry = store.read(key);
  const { sessionId, scope, workspace } = context;
  const setValue = useCallback((next: string | ((current: string) => string)) => {
    const value = typeof next === "function" ? next(store.read(key).value) : next;
    store.write(key, value);
    if (value || store.read(key).attachments.length) store.remember({ sessionId, scope, workspace });
    refresh();
  }, [key, store, sessionId, scope, workspace]);

  return {
    key,
    value: entry.value,
    attachments: entry.attachments,
    revision: entry.revision,
    status: entry.value || entry.attachments.length ? entry.saved ? "saved" as const : "memory" as const : "empty" as const,
    setValue,
    store,
    consume: (submittedKey: string, revision: number) => { store.consume(submittedKey, revision); refresh(); },
    addAttachments: (files: Attachment[], targetKey = key) => {
      const current = store.read(targetKey);
      store.write(targetKey, current.value, [...current.attachments, ...files]);
      if (targetKey === activeKey.current) store.remember({ sessionId, scope, workspace });
      refresh();
    },
    removeAttachment: (id: string) => {
      store.write(key, store.read(key).value, store.read(key).attachments.filter(file => file.id !== id));
      refresh();
    },
    remove: (removedKey: string) => { store.write(removedKey, "", []); refresh(); },
  };
}
