import type { RecentWorkspace } from "../types";

export const WORKSPACE_ORDER_KEY = "minicode.rail.workspace-order.v1";
type OrderStorage = Pick<Storage, "getItem" | "setItem"> | null;
export type WorkspaceDropPosition = "before" | "after";

export function workspacePathIdentity(path: string): string {
  return path.replace(/\//g, "\\").replace(/[\\]+$/, "").toLocaleLowerCase();
}

export function mergeWorkspaceOrder(order: readonly string[], paths: readonly string[]): string[] {
  return [...new Set([...order, ...paths].filter(Boolean).map(workspacePathIdentity))];
}

export function loadWorkspaceOrder(storage: OrderStorage): string[] {
  try {
    const saved: unknown = JSON.parse(storage?.getItem(WORKSPACE_ORDER_KEY) || "[]");
    return Array.isArray(saved)
      ? mergeWorkspaceOrder([], saved.filter((path): path is string => typeof path === "string"))
      : [];
  } catch { return []; }
}

export function saveWorkspaceOrder(storage: OrderStorage, order: readonly string[]): void {
  try { storage?.setItem(WORKSPACE_ORDER_KEY, JSON.stringify(order)); }
  catch { /* Keep the chosen order in memory when browser storage is unavailable. */ }
}

export function orderedWorkspaces(projects: readonly RecentWorkspace[], order: readonly string[]): RecentWorkspace[] {
  const positions = new Map(mergeWorkspaceOrder(order, projects.map(project => project.path))
    .map((path, index) => [path, index]));
  return [...projects].sort((left, right) =>
    positions.get(workspacePathIdentity(left.path))! - positions.get(workspacePathIdentity(right.path))!);
}

export function moveWorkspace(order: readonly string[], source: string, target: string, position: WorkspaceDropPosition): string[] {
  const from = workspacePathIdentity(source);
  const to = workspacePathIdentity(target);
  if (from === to || !order.includes(from) || !order.includes(to)) return [...order];
  const next = order.filter(path => path !== from);
  next.splice(next.indexOf(to) + (position === "after" ? 1 : 0), 0, from);
  return next;
}
