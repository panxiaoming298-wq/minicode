import type { SavedConnection, SettingsView } from "../types";

type HistoryStorage = Pick<Storage, "getItem" | "setItem">;
type ConnectionSettings = Pick<SettingsView, "provider" | "model" | "baseUrl" | "settingsPath" | "connectionHistory">;
const storageKey = (settings: ConnectionSettings) => `minicode.connection-history.v1:${settings.settingsPath}`;

function browserStorage(): HistoryStorage | null {
  try { return typeof window === "undefined" ? null : window.localStorage; } catch { return null; }
}

export const connectionId = (connection: SavedConnection) => JSON.stringify([
  connection.provider, connection.model.trim(), connection.baseUrl.trim().replace(/\/+$/, ""),
]);

function mergeConnections(...sources: unknown[]): SavedConnection[] {
  const result: SavedConnection[] = [];
  const seen = new Set<string>();
  for (const source of sources) {
    if (!Array.isArray(source)) continue;
    for (const item of source) {
      if (!item || typeof item !== "object" || !["provider", "model", "baseUrl"].every(key => typeof item[key] === "string")) continue;
      const connection: SavedConnection = { provider: item.provider.trim().toLowerCase(), model: item.model.trim(),
        baseUrl: item.baseUrl.trim().replace(/\/+$/, "") };
      if (!connection.provider || !connection.model) continue;
      try {
        const url = new URL(connection.baseUrl);
        if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) continue;
      } catch { continue; }
      const id = connectionId(connection);
      if (seen.has(id)) continue;
      seen.add(id);
      result.push(connection);
      if (result.length === 20) return result;
    }
  }
  return result;
}

export function readConnectionHistory(settings: ConnectionSettings, storage = browserStorage()): SavedConnection[] {
  let local: unknown = [];
  try { local = JSON.parse(storage?.getItem(storageKey(settings)) ?? "[]"); } catch { /* Corrupt or unavailable browser storage. */ }
  return mergeConnections([settings], settings.connectionHistory, local);
}

export function rememberConnectionHistory(settings: ConnectionSettings, previous: SavedConnection[], storage = browserStorage()) {
  const history = mergeConnections([settings], settings.connectionHistory, previous, readConnectionHistory(settings, storage));
  let persisted = Array.isArray(settings.connectionHistory);
  try {
    if (storage) { storage.setItem(storageKey(settings), JSON.stringify(history)); persisted = true; }
  } catch { /* New backends already saved their history; old ones report storage failure. */ }
  return { history, persisted };
}
