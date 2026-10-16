import { useReducer, useState } from "react";
import type { PermissionMode } from "../types";
import { PermissionModeStore } from "./permissionMode";

export function usePermissionMode(context: string, fallback: PermissionMode = "bypass") {
  const [store] = useState(() => {
    try { return new PermissionModeStore(window.localStorage); }
    catch { return new PermissionModeStore(null); }
  });
  const [, refresh] = useReducer((value: number) => value + 1, 0);

  return {
    value: store.read(context) ?? fallback,
    change: (mode: PermissionMode) => { store.write(context, mode); refresh(); },
    bindSession: (source: string, session: string, mode: PermissionMode, consumeDraft = false) => {
      store.bindSession(source, session, mode, consumeDraft);
      refresh();
    },
  };
}
