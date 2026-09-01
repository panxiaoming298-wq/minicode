import type {
  BootstrapPayload,
  ConversationScope,
  NavigationPayload,
  RunEvent,
  RunInfo,
  WorkspaceOpenPayload,
  WorkspaceDirectory,
  WorkspaceFileContent,
  SessionSummary,
  SettingsView,
  StoredMessage,
  WorkspaceStatus,
} from "../types";

const API_ROOT = (import.meta.env.VITE_MINICODE_API_URL || "").replace(/\/$/, "");

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) detail = payload.detail;
    } catch {
      // Keep the status-based fallback.
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export const api = {
  bootstrap: () => request<BootstrapPayload>("/api/bootstrap"),
  getSettings: () => request<SettingsView>("/api/settings"),
  saveSettings: (payload: {
    provider: string;
    model: string;
    baseUrl: string;
    apiKey?: string;
    clearApiKey?: boolean;
    thinkingMode?: string;
    reasoningEffort?: string;
  }) =>
    request<SettingsView>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  testSettings: () =>
    request<{ ok: boolean; latencyMs: number; message: string }>("/api/settings/test", {
      method: "POST",
    }),
  pickWorkspace: (initialPath: string) =>
    request<{ cancelled: boolean; workspace: string }>("/api/workspaces/pick", {
      method: "POST",
      body: JSON.stringify({ initialPath }),
    }),
  openWorkspace: (path: string) =>
    request<WorkspaceOpenPayload>("/api/workspaces/open", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  getNavigation: () => request<NavigationPayload>("/api/navigation"),
  setNavigationPin: (payload: {
    kind: "workspace" | "session";
    target: string;
    pinned: boolean;
  }) =>
    request<NavigationPayload>("/api/navigation/pins", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  listSessions: (workspace: string) =>
    request<{ sessions: SessionSummary[] }>(`/api/sessions?workspace=${encodeURIComponent(workspace)}`),
  getSession: (sessionId: string) =>
    request<{ session: SessionSummary; messages: StoredMessage[] }>(
      `/api/sessions/${encodeURIComponent(sessionId)}`,
    ),
  deleteSession: (sessionId: string) =>
    request<{ deleted: boolean }>(`/api/sessions/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    }),
  startRun: (payload: {
    scope: ConversationScope;
    workspace?: string;
    prompt: string;
    sessionId?: string;
  }) =>
    request<{ run: RunInfo }>("/api/runs", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  stopRun: (runId: string) =>
    request<{ run: RunInfo }>(`/api/runs/${encodeURIComponent(runId)}/stop`, {
      method: "POST",
    }),
  approve: (
    runId: string,
    payload: { requestId: string; decision: string; feedback?: string },
  ) =>
    request<{ run: RunInfo }>(`/api/runs/${encodeURIComponent(runId)}/approvals`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  workspaceStatus: (workspace: string) =>
    request<WorkspaceStatus>(`/api/workspace/status?path=${encodeURIComponent(workspace)}`),
  workspaceFiles: (workspace: string, directory = "") => {
    const query = new URLSearchParams({ path: workspace, directory });
    return request<WorkspaceDirectory>(`/api/workspace/files?${query.toString()}`);
  },
  workspaceFile: (workspace: string, file: string) => {
    const query = new URLSearchParams({ path: workspace, file });
    return request<WorkspaceFileContent>(`/api/workspace/file?${query.toString()}`);
  },
  workspaceDiff: (workspace: string, file?: string) => {
    const query = new URLSearchParams({ path: workspace });
    if (file) query.set("file", file);
    return request<{ diff: string; file: string }>(`/api/workspace/diff?${query.toString()}`);
  },
  eventSource: (runId: string) => new EventSource(`${API_ROOT}/api/runs/${encodeURIComponent(runId)}/events`),
};

export function parseRunEvent(event: MessageEvent<string>): RunEvent {
  return JSON.parse(event.data) as RunEvent;
}
