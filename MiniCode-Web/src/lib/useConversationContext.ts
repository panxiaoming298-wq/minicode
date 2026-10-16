import { useCallback, useEffect, useReducer, useState } from "react";
import { api } from "../api/client";
import type { ConversationContext, ConversationContextView, ConversationScope, ReasoningEffort } from "../types";

export const DEFAULT_CONTEXT_TOKENS = 400_000;
const PREFIX = "minicode.conversationContext.v1:";
type Entry = { view?: ConversationContextView; pending?: boolean; error?: string; revision: number };

function readDraft(key: string): ConversationContext {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    if (raw !== null) {
      const value = JSON.parse(raw);
      if (value === null || (Number.isSafeInteger(value) && value >= 8192)) return { maxTokens: value };
      if (typeof value === "object" && value !== null
        && (value.maxTokens === null || (Number.isSafeInteger(value.maxTokens) && value.maxTokens >= 8192))
        && (value.reasoningEffort === undefined || ["low", "high", "max"].includes(value.reasoningEffort))) return value;
    }
  } catch { /* In-memory selection remains available without browser storage. */ }
  return { maxTokens: DEFAULT_CONTEXT_TOKENS };
}

export function contextView(base: ConversationContextView, maxTokens: number | null,
  reasoningEffort: ReasoningEffort | undefined = base.reasoningEffort): ConversationContextView {
  // New policies require provider-aware server evaluation. This placeholder
  // stays blocked until reload/preview completes, rather than inventing limits.
  if (base.budgetPolicy) return base;
  const effectiveMaxTokens = Math.min(maxTokens ?? base.availableMaxTokens, base.availableMaxTokens);
  return { ...base, maxTokens, ...(reasoningEffort ? { reasoningEffort } : {}), effectiveMaxTokens,
    effectiveInputTokens: Math.max(0, effectiveMaxTokens - base.reservedTokens),
    capacityError: effectiveMaxTokens <= base.reservedTokens ? "output_reserve_exceeds_window" : "" };
}

export function useConversationContext(key: string, sessionId: string | null, scope: ConversationScope,
  workspace: string, defaults?: ConversationContextView) {
  const [entries] = useState(() => new Map<string, Entry>());
  const [, render] = useReducer((value: number) => value + 1, 0);
  const supported = Boolean(defaults);
  const model = defaults?.model;
  const available = defaults?.availableMaxTokens;
  const reserve = defaults?.reservedTokens;
  const reasoningSupported = defaults?.reasoningEffortSupported;
  const policySignature = JSON.stringify(defaults?.budgetPolicy);
  const reload = useCallback(async () => {
    if (!supported || (scope === "project" && !workspace)) return;
    const previous = entries.get(key);
    if (previous?.pending) return;
    const revision = (previous?.revision || 0) + 1;
    entries.set(key, { ...previous, pending: true, error: undefined, revision });
    render();
    try {
      const view = await api.getContextSettings(sessionId, scope, workspace);
      if (entries.get(key)?.revision !== revision) return;
      const draft = previous?.view ?? readDraft(key);
      const preferences = { maxTokens: draft.maxTokens, reasoningEffort: draft.reasoningEffort ?? view.reasoningEffort };
      const resolved = sessionId ? view : view.budgetPolicy
        ? await api.previewContextSettings(scope, workspace, preferences)
        : contextView(view, preferences.maxTokens, preferences.reasoningEffort);
      if (entries.get(key)?.revision !== revision) return;
      entries.set(key, { view: resolved, revision });
    } catch (cause) {
      if (entries.get(key)?.revision === revision) entries.set(key, { ...previous, revision,
        error: cause instanceof Error ? cause.message : "上下文设置读取失败，请重试。" });
    } finally { render(); }
  }, [entries, key, scope, sessionId, supported, workspace]);

  useEffect(() => { void reload(); }, [reload, model, available, reserve, reasoningSupported, policySignature]);

  const entry = entries.get(key);
  const draft = readDraft(key);
  const view = entry?.view ?? (defaults ? contextView(defaults, sessionId ? DEFAULT_CONTEXT_TOKENS : draft.maxTokens,
    sessionId ? defaults.reasoningEffort : draft.reasoningEffort ?? defaults.reasoningEffort) : undefined);
  const blocked = supported && (!entry?.view || Boolean(entry.pending || entry.error || entry.view.capacityError));
  return {
    supported, view, blocked, pending: entry?.pending || false, error: entry?.error,
    reload,
    selection: () => {
      const current = entries.get(key);
      if (!supported) return undefined;
      if (!current?.view || current.pending || current.error || current.view.capacityError) throw new Error("请先确认并保存当前对话的上下文设置。");
      return { maxTokens: current.view.maxTokens,
        ...(current.view.reasoningEffort ? { reasoningEffort: current.view.reasoningEffort } : {}) };
    },
    save: async (maxTokens: number | null, reasoningEffort: ReasoningEffort | undefined = view?.reasoningEffort) => {
      const current = entries.get(key);
      if (current?.pending || !view) return;
      const revision = (current?.revision || 0) + 1;
      entries.set(key, { ...current, revision, pending: true });
      render();
      try {
        if (maxTokens !== null && (!Number.isSafeInteger(maxTokens) || maxTokens < 8192)) throw new Error("请输入至少 8,192 的整数 Token 数。");
        const preferences = { maxTokens, ...(reasoningEffort ? { reasoningEffort } : {}) };
        const next = contextView(view, maxTokens, reasoningEffort);
        if (!view.budgetPolicy && next.capacityError) throw new Error(`上下文上限需大于输出及安全预留（${next.reservedTokens.toLocaleString()} tokens）。`);
        const saved = sessionId ? await api.saveContextSettings(sessionId, preferences)
          : view.budgetPolicy ? await api.previewContextSettings(scope, workspace, preferences) : next;
        entries.set(key, { view: saved, revision });
        if (!sessionId) {
          try { localStorage.setItem(PREFIX + key, JSON.stringify(preferences)); } catch { /* Keep selection in memory. */ }
        }
      } catch (cause) {
        entries.set(key, { ...current, revision, error: cause instanceof Error ? cause.message : "上下文设置保存失败，请重试。" });
      } finally { render(); }
    },
    consumeDraft: (source: string, preferences: ConversationContext) => {
      const current = entries.get(source)?.view;
      if (current?.maxTokens !== preferences.maxTokens || current.reasoningEffort !== preferences.reasoningEffort) return;
      entries.delete(source);
      try { localStorage.removeItem(PREFIX + source); } catch { /* Ignore unavailable storage. */ }
      render();
    },
  };
}

export type ConversationContextSelection = ReturnType<typeof useConversationContext>;
