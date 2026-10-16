import { useCallback, useEffect, useRef, useState } from "react";
import type { ContextStatsView, RunEvent } from "../types";

export function isContextRefreshEvent(event: RunEvent): boolean {
  return (!event.nodeId || event.nodeId === "root" || event.nodeId === "primary")
    && ["model_status", "assistant_start", "assistant", "tool_result", "turn_saved"].includes(event.kind);
}

/** Refresh the selected conversation independently of the usage popover. */
export function useContextRefresh({ identity, enabled, active, revision, onRefresh }: {
  identity: string; enabled: boolean; active: boolean; revision: number;
  onRefresh: () => Promise<ContextStatsView | undefined>;
}) {
  const [state, setState] = useState<{ identity: string; refreshing: boolean; error: boolean; context: ContextStatsView | null }>(
    { identity, refreshing: false, error: false, context: null });
  const currentIdentity = useRef(identity);
  currentIdentity.current = identity;
  const inFlight = useRef<{ identity: string; promise: Promise<void> } | null>(null);

  const refresh = useCallback(() => {
    if (!enabled) return Promise.resolve();
    if (inFlight.current?.identity === identity) return inFlight.current.promise;
    const request = { identity, promise: Promise.resolve() };
    inFlight.current = request;
    setState(current => ({ identity, refreshing: true, error: false,
      context: current.identity === identity ? current.context : null }));
    request.promise = (async () => {
      try {
        const context = await onRefresh();
        if (context && currentIdentity.current === identity && inFlight.current === request) {
          setState(current => ({ ...current, context }));
        }
      }
      catch {
        if (currentIdentity.current === identity && inFlight.current === request) {
          setState(current => ({ ...current, error: true }));
        }
      } finally {
        if (currentIdentity.current === identity && inFlight.current === request) {
          inFlight.current = null;
          setState(current => ({ ...current, refreshing: false }));
        }
      }
    })();
    return request.promise;
  }, [enabled, identity, onRefresh]);

  useEffect(() => {
    if (!enabled) return;
    const sync = () => { if (document.visibilityState !== "hidden") void refresh(); };
    if (active) sync();
    const timer = active ? window.setInterval(sync, 3_000) : undefined;
    document.addEventListener("visibilitychange", sync);
    window.addEventListener("focus", sync);
    return () => {
      if (timer !== undefined) window.clearInterval(timer);
      document.removeEventListener("visibilitychange", sync);
      window.removeEventListener("focus", sync);
    };
  }, [active, enabled, refresh]);

  useEffect(() => {
    if (!enabled || !active || !revision) return;
    // Model/tool boundaries can arrive together; streaming deltas never trigger reads.
    const timer = window.setTimeout(() => {
      if (document.visibilityState !== "hidden") void refresh();
    }, 200);
    return () => window.clearTimeout(timer);
  }, [active, enabled, refresh, revision]);

  return { refresh, refreshing: state.identity === identity && state.refreshing,
    context: state.identity === identity ? state.context : null,
    error: state.identity === identity && state.error };
}
