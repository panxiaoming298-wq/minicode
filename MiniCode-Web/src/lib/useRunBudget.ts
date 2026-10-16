import { useReducer, useState } from "react";
import type { RunBudget } from "../types";

export interface RunBudgetDraft {
  context: string;
  revision: number;
  maxTokens: string;
  durationSeconds: string;
  unlimited?: boolean;
}

export function emptyRunBudget(context: string, revision = 0): RunBudgetDraft {
  return { context, revision, maxTokens: "", durationSeconds: "" };
}

export function defaultRunBudget(context: string, revision = 0): RunBudgetDraft {
  return { ...emptyRunBudget(context, revision), unlimited: true };
}

function budgetDraft(context: string, budget: RunBudget): RunBudgetDraft {
  return { context, revision: 0, maxTokens: budget.maxTokens?.toString() || "",
    durationSeconds: budget.durationSeconds?.toString() || "", unlimited: budget.unlimited === true };
}

type BudgetStorage = Pick<Storage, "getItem" | "setItem">;
const PREFIX = "minicode.runBudget.v1:";

export class RunBudgetStore {
  private entries = new Map<string, RunBudgetDraft | undefined>();

  constructor(private storage: BudgetStorage | null) {}

  private stored(context: string): RunBudgetDraft | undefined {
    if (this.entries.has(context)) return this.entries.get(context);
    let draft: RunBudgetDraft | undefined;
    try {
      const raw = this.storage?.getItem(PREFIX + context);
      const value: unknown = raw ? JSON.parse(raw) : null;
      if (value && typeof value === "object"
        && "maxTokens" in value && typeof value.maxTokens === "string"
        && "durationSeconds" in value && typeof value.durationSeconds === "string"
        && "unlimited" in value && typeof value.unlimited === "boolean"
        && !(value.unlimited && (value.maxTokens.trim() || value.durationSeconds.trim()))) {
        draft = { context, revision: 0, maxTokens: value.maxTokens,
          durationSeconds: value.durationSeconds, unlimited: value.unlimited };
      }
    } catch { /* Keep conversation budgets usable without browser storage. */ }
    this.entries.set(context, draft);
    return draft;
  }

  read(context: string, previous?: RunBudget | null): RunBudgetDraft {
    return this.stored(context) ?? (previous ? budgetDraft(context, previous) : defaultRunBudget(context));
  }

  write(context: string, selection: Pick<RunBudgetDraft, "maxTokens" | "durationSeconds" | "unlimited">): RunBudgetDraft {
    const draft = { context, revision: this.read(context).revision + 1, maxTokens: selection.maxTokens,
      durationSeconds: selection.durationSeconds, unlimited: selection.unlimited === true };
    this.entries.set(context, draft);
    try {
      this.storage?.setItem(PREFIX + context, JSON.stringify({ maxTokens: draft.maxTokens,
        durationSeconds: draft.durationSeconds, unlimited: draft.unlimited }));
    } catch { /* The selection remains available in this page. */ }
    return draft;
  }

  bindSession(submitted: RunBudgetDraft, context: string, resetSource = false) {
    const current = this.read(submitted.context);
    // A prepared session or a newer choice must survive a late acknowledgement.
    if (!this.stored(context)) this.write(context, submitted);
    if (resetSource && submitted.context !== context && current.revision === submitted.revision) {
      this.write(submitted.context, defaultRunBudget(submitted.context));
    }
  }
}

export function runBudgetSelection(draft: RunBudgetDraft, supported: boolean): RunBudget | undefined {
  if (draft.unlimited) {
    if (!supported) throw new Error("当前后端不支持任务预算，请升级并重启本机服务后重试。");
    return { unlimited: true };
  }
  if (!draft.maxTokens.trim() && !draft.durationSeconds.trim()) return undefined;
  if (!supported) throw new Error("当前后端不支持任务预算，请升级并重启本机服务后重试；不会发送忽略预算的任务。");
  const result: RunBudget = {};
  if (draft.maxTokens.trim()) {
    const value = Number(draft.maxTokens);
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error("任务 Token 上限必须是大于 0 的安全整数。");
    result.maxTokens = value;
  }
  if (draft.durationSeconds.trim()) {
    const value = Number(draft.durationSeconds);
    if (!Number.isFinite(value) || value <= 0 || value > 86400) throw new Error("任务运行时长必须大于 0 且不超过 86400 秒。");
    result.durationSeconds = value;
  }
  return result;
}

export function useRunBudget(context: string, previous?: RunBudget | null) {
  const [store] = useState(() => {
    try { return new RunBudgetStore(window.localStorage); }
    catch { return new RunBudgetStore(null); }
  });
  const [, refresh] = useReducer((value: number) => value + 1, 0);
  const draft = store.read(context, previous);
  const save = (selection: Pick<RunBudgetDraft, "maxTokens" | "durationSeconds" | "unlimited">) => {
    store.write(context, selection);
    refresh();
  };
  return {
    draft,
    change: (field: "maxTokens" | "durationSeconds", value: string) => save({ ...draft, [field]: value, unlimited: false }),
    unlimit: () => save(defaultRunBudget(context)),
    reuse: (budget: RunBudget) => save(budgetDraft(context, budget)),
    clear: () => save(emptyRunBudget(context)),
    bindSession: (submitted: RunBudgetDraft, nextContext: string, resetSource = false) => {
      store.bindSession(submitted, nextContext, resetSource);
      refresh();
    },
  };
}
