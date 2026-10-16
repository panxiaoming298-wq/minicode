import type { SessionSummary } from "../types";
import type { WorkspaceDropPosition } from "./workspaceOrder";

export const SESSION_ORDER_KEY = "minicode.rail.session-order.v1";
export type SessionOrders = Record<string, string[]>;
type OrderStorage = Pick<Storage, "getItem" | "setItem"> | null;

export function loadSessionOrders(storage: OrderStorage): SessionOrders {
  try {
    const saved: unknown = JSON.parse(storage?.getItem(SESSION_ORDER_KEY) || "{}");
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return {};
    return Object.fromEntries(Object.entries(saved).flatMap(([group, ids]) => Array.isArray(ids)
      ? [[group, [...new Set(ids.filter((id): id is string => typeof id === "string" && Boolean(id)))]]]
      : []));
  } catch { return {}; }
}

export function saveSessionOrders(storage: OrderStorage, orders: SessionOrders): void {
  try { storage?.setItem(SESSION_ORDER_KEY, JSON.stringify(orders)); }
  catch { /* Keep sorting available when browser storage cannot be written. */ }
}

export function mergeSessionOrder(order: readonly string[], ids: readonly string[]): string[] {
  // New conversations start at the top. Archived/hidden entries keep their
  // places so restoring them does not disturb a manually arranged list.
  const known = new Set(order);
  return [...new Set([...ids.filter(id => !known.has(id)), ...order])];
}

export function orderedSessions(sessions: readonly SessionSummary[], order: readonly string[] = []): SessionSummary[] {
  const positions = new Map(mergeSessionOrder(order, sessions.map(session => session.sessionId))
    .map((id, index) => [id, index]));
  return [...sessions].sort((left, right) => positions.get(left.sessionId)! - positions.get(right.sessionId)!);
}

export function moveSession(order: readonly string[], source: string, target: string, position: WorkspaceDropPosition): string[] {
  if (source === target || !order.includes(source) || !order.includes(target)) return [...order];
  const next = order.filter(id => id !== source);
  next.splice(next.indexOf(target) + (position === "after" ? 1 : 0), 0, source);
  return next;
}
