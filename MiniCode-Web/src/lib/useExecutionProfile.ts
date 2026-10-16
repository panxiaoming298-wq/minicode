import { useReducer, useState } from "react";
import type { ExecutionProfile } from "../types";
import { ExecutionProfileStore } from "./executionProfile";

export function useExecutionProfile(context: string, fallback: ExecutionProfile) {
  const [store] = useState(() => {
    try { return new ExecutionProfileStore(window.localStorage); }
    catch { return new ExecutionProfileStore(null); }
  });
  const [, refresh] = useReducer((value: number) => value + 1, 0);

  return {
    // Resolve by context during render so a session switch cannot reuse the
    // previous conversation's selection while asynchronous history loads.
    value: store.read(context) ?? fallback,
    change: (profile: ExecutionProfile) => { store.write(context, profile); refresh(); },
    bindSession: (source: string, session: string, profile: ExecutionProfile, consumeDraft = false) => {
      store.bindSession(source, session, profile, consumeDraft);
      refresh();
    },
  };
}
