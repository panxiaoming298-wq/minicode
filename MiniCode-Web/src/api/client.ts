import type {
  ConversationPage,
  ImageStatus,
  ImageStorageView,
  TurnMessagesPage,
  BootstrapPayload,
  CandidateRecovery,
  SubmittedCandidate,
  RecoverableCandidate,
  CapabilitySnapshot,
  ConversationScope,
  ConversationContext,
  ConversationContextView,
  ExecutionProfile,
  PermissionMode,
  ExecutionLocation,
  ExtensionCatalog,
  SkillInventory,
  SkillPreview,
  ExtensionHealthCheck,
  ExtensionMutationResponse,
  ExtensionScope,
  ManagedExtension,
  NavigationPayload,
  ProjectClosePayload,
  RunEvent,
  RunInfo,
  RunBudget,
  RunInput,
  RunSnapshot,
  SessionInspect,
  ContextKnowledgeView,
  WorkspaceOpenPayload,
  WorkspaceDirectory,
  WorkspaceFileContent,
  SessionSummary,
  TrashedSession,
  SettingsView,
  SettingsUpdate,
  WebSearchSettingsView,
  WebSearchSettingsUpdate,
  SearchUsageView,
  ProviderBalance,
  StoredMessage,
  WorkspaceStatus,
  TurnsPage,
  TurnFileChanges,
  TurnUndoState,
  WorktreeOperation,
  WorktreeChanges,
  TaskFileSelection,
  TaskFilePreview,
} from "../types";
import { parseDevelopmentService, parseDevelopmentServiceLogs, parseDevelopmentServices } from "../lib/developmentServices";
import type { ImageAttachment } from "../lib/attachments";

const API_ROOT = (import.meta.env.VITE_MINICODE_API_URL || "").replace(/\/$/, "");

export function terminalSocketUrl(ticket: string): string {
  const url = new URL(`${API_ROOT}/api/terminal/${encodeURIComponent(ticket)}`, window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

function skillQueryParams(workspace: string, scope: ConversationScope, invocation?: {workspace: string; scope: ConversationScope}) {
  const query = new URLSearchParams({ workspace, scope });
  if (invocation) {
    query.set("invocationWorkspace", invocation.workspace);
    query.set("invocationScope", invocation.scope);
  }
  return query;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  return parseApiResponse<T>(response, path);
}

export async function parseApiResponse<T>(response: Response, path: string): Promise<T> {
  const contentType = response.headers.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase() || "";
  if (contentType !== "application/json" && !contentType.endsWith("+json")) {
    throw new Error(`接口 ${path.split("?", 1)[0]} 返回了非 JSON 内容（HTTP ${response.status}）。请确认前后端版本一致，重启 MiniCode 本机服务后刷新页面。`);
  }
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try {
      const payload = (await response.json()) as { detail?: string | Array<{ loc?: string[]; msg?: string }> };
      if (typeof payload.detail === "string") detail = payload.detail;
      else if (Array.isArray(payload.detail)) {
        detail = payload.detail.map((item) => `${item.loc?.slice(1).join(".") || "设置"}：${item.msg || "请检查输入值"}`).join("；");
      }
    } catch {
      // Keep the status-based fallback.
    }
    throw new ApiError(detail, response.status);
  }
  try {
    return (await response.json()) as T;
  } catch {
    throw new Error(`接口 ${path.split("?", 1)[0]} 返回了无效 JSON。请重启 MiniCode 本机服务后刷新页面。`);
  }
}

async function sessionRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15_000);
  try {
    return await request<T>(path, { ...init, signal: init?.signal ? AbortSignal.any([controller.signal, init.signal]) : controller.signal });
  } catch (error) {
    if (controller.signal.aborted) throw new Error(init?.method ? "保存结果未确认，请重新读取设置后重试。" : "读取会话超时，请重试打开；已有记录仍保留。");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export function parseTurnsPage(value: unknown): TurnsPage {
  const record = (item: unknown): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item);
  const natural = (item: unknown): item is number => typeof item === "number" && Number.isSafeInteger(item) && item >= 0;
  const valid = record(value) && Array.isArray(value.turns) && natural(value.total)
    && value.total >= value.turns.length
    && (value.nextBefore === null || (natural(value.nextBefore) && value.nextBefore <= value.total))
    && value.turns.every(turn => record(turn) && typeof turn.turnId === "string" && Boolean(turn.turnId)
      && typeof turn.prompt === "string" && typeof turn.status === "string" && typeof turn.canFork === "boolean"
      && (turn.reason === undefined || typeof turn.reason === "string")
      && Array.isArray(turn.displayMessages) && turn.displayMessages.every(message => record(message)
        && typeof message.role === "string"
        && ["toolName", "toolUseId", "output"].every(key => message[key] === undefined || typeof message[key] === "string")));
  if (!valid) throw new Error("轮次历史响应格式不兼容。请确认前后端版本一致，重启本机服务后重新打开会话；未采用这份异常历史。");
  return value as unknown as TurnsPage;
}

export function parseSubmittedCandidates(value: unknown): { candidates: SubmittedCandidate[] } {
  const record = (item: unknown): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item);
  const valid = record(value) && Array.isArray(value.candidates) && value.candidates.every(candidate => record(candidate)
    && typeof candidate.runId === "string" && Boolean(candidate.runId.trim())
    && ["available", "submitted", "integrated"].every(key => typeof candidate[key] === "boolean")
    && typeof candidate.verificationStatus === "string"
    && ["artifactId", "revision", "workspace"].every(key => candidate[key] === undefined || typeof candidate[key] === "string"));
  if (!valid) throw new Error("候选状态响应格式不兼容。请确认前后端版本一致，重启本机服务后重试；未采用这份异常候选数据。");
  return value as { candidates: SubmittedCandidate[] };
}

export const api = {
  createTerminalTicket(input: { workspace: string; sessionId?: string | null; cols: number; rows: number }, signal: AbortSignal) {
    return request<{ ticket: string; workspace: string; shell: string }>("/api/terminal/tickets", {
      method: "POST", body: JSON.stringify(input), signal,
    });
  },
  imageStatus: (sessionId: string, imageIds: string[], signal?: AbortSignal) => request<{ images: ImageStatus[] }>(
    `/api/sessions/${encodeURIComponent(sessionId)}/images/status?${new URLSearchParams({ image_ids: imageIds.join(",") })}`, { signal }),
  imageStorage: (signal?: AbortSignal) => request<ImageStorageView>("/api/storage/images", { signal }),
  clearSessionImages: (sessionId: string, revision: string) => request<{ clearedCount: number; releasedBytes: number }>(
    `/api/sessions/${encodeURIComponent(sessionId)}/images?${new URLSearchParams({ confirmed: "true", revision })}`, { method: "DELETE" }),
  contextKnowledge: (sessionId: string, signal?: AbortSignal) => request<ContextKnowledgeView>(
    `/api/sessions/${encodeURIComponent(sessionId)}/context-knowledge`, { signal }),
  exportTaskMemory: async (sessionId: string, signal?: AbortSignal): Promise<Blob> => {
    const path = `/api/sessions/${encodeURIComponent(sessionId)}/memory/export`;
    const response = await fetch(`${API_ROOT}${path}`, { signal, cache: "no-store", headers: { Accept: "text/markdown" } });
    if (!response.ok) await parseApiResponse<never>(response, path);
    if (response.headers.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase() !== "text/markdown") {
      throw new Error("服务未返回 Markdown 文件，请更新并重启 MiniCode 后重试。");
    }
    return response.blob();
  },
  contextRule: (sessionId: string, name: string, signal?: AbortSignal) => request<{ content: string; path: string; contentHash: string }>(
    `/api/sessions/${encodeURIComponent(sessionId)}/context-knowledge/rules/${encodeURIComponent(name)}`, { signal }),
  reviseMemory: (sessionId: string, scope: string, id: string, revision: string, change: { content?: string; archived?: boolean }) =>
    request<ContextKnowledgeView>(`/api/sessions/${encodeURIComponent(sessionId)}/context-knowledge/memories/${encodeURIComponent(scope)}/${encodeURIComponent(id)}`,
      { method: "PUT", body: JSON.stringify({ revision, ...change }) }),
  uploadImage: (file: File) => request<ImageAttachment>(`/api/image-attachments?name=${encodeURIComponent(file.name)}`, {
    method: "POST", body: file, headers: { "Content-Type": file.type || "application/octet-stream" },
  }),
  imageUrl: (id: string) => `${API_ROOT}/api/image-attachments/${encodeURIComponent(id)}`,
  runImageUrl: (sessionId: string, id: string) => `${API_ROOT}/api/sessions/${encodeURIComponent(sessionId)}/images/${encodeURIComponent(id)}`,
  getConversationPage: async (id: string, before?: number, signal?: AbortSignal): Promise<ConversationPage> => {
    const query = new URLSearchParams({ limit: "5" });
    if (before !== undefined) query.set("before", String(before));
    const value = await sessionRequest<ConversationPage>(`/api/sessions/${encodeURIComponent(id)}/view?${query}`, { signal });
    parseTurnsPage(value);
    if (value.session?.sessionId !== id || typeof value.revision !== "string") throw new Error("对话展示数据不兼容，请重新打开会话。");
    return value;
  },
  getTurnMessages: (id: string, turnId: string, after = 0, signal?: AbortSignal) =>
    sessionRequest<TurnMessagesPage>(`/api/sessions/${encodeURIComponent(id)}/turns/${encodeURIComponent(turnId)}/messages?after=${after}`, { signal }),
  getTurnMessage: (id: string, turnId: string, index: number, signal?: AbortSignal) =>
    sessionRequest<{ message: StoredMessage }>(`/api/sessions/${encodeURIComponent(id)}/turns/${encodeURIComponent(turnId)}/messages/${index}`, { signal }),
  getContextStats: (id: string) => sessionRequest<{ contextStats: SessionInspect["contextStats"] }>(`/api/sessions/${encodeURIComponent(id)}/context-stats`),
  developmentServices: async (workspace?: string, sessionId?: string, signal?: AbortSignal) => {
    const query = new URLSearchParams(sessionId ? { sessionId } : { workspace: workspace || "" });
    return parseDevelopmentServices(await request<unknown>(`/api/development-services?${query}`, { signal }));
  },
  developmentServiceLogs: async (serviceId: string, signal?: AbortSignal) =>
    parseDevelopmentServiceLogs(await request<unknown>(`/api/development-services/${encodeURIComponent(serviceId)}/logs`, { signal })),
  stopDevelopmentService: async (serviceId: string) => {
    const result = await request<{ service: unknown }>(`/api/development-services/${encodeURIComponent(serviceId)}/stop`, { method: "POST" });
    return parseDevelopmentService(result.service);
  },
  restartDevelopmentService: async (serviceId: string) => {
    const result = await request<{ service: unknown }>(`/api/development-services/${encodeURIComponent(serviceId)}/restart`, { method: "POST" });
    return parseDevelopmentService(result.service);
  },
  getTurnChanges: (sessionId: string, turnId: string, runId?: string) => request<{ changes: TurnFileChanges | null }>(runId
    ? `/api/runs/${encodeURIComponent(runId)}/changes?turnId=${encodeURIComponent(turnId)}`
    : `/api/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(turnId)}/changes`),
  getTurnUndo: (sessionId: string, turnId: string) => request<{ undo: TurnUndoState }>(
    `/api/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(turnId)}/undo`),
  applyTurnUndo: (sessionId: string, turnId: string, action: "undo" | "restore", revision: number) => request<{ undo: TurnUndoState }>(
    `/api/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(turnId)}/undo`,
    { method: "POST", body: JSON.stringify({ action, revision }) }),
  candidateRecoveries: (id: string) => request<{ candidates: RecoverableCandidate[] }>(`/api/sessions/${encodeURIComponent(id)}/candidate-recoveries`),
  submittedCandidates: async (id: string, signal?: AbortSignal) => parseSubmittedCandidates(await request<unknown>(`/api/sessions/${encodeURIComponent(id)}/submitted-candidates`, { signal })),
  candidateFiles: (id: string, runId: string, path: string) => request<WorkspaceDirectory>(
    `/api/sessions/${encodeURIComponent(id)}/submitted-candidates/${encodeURIComponent(runId)}/files?${new URLSearchParams({path})}`),
  candidateFile: (id: string, runId: string, path: string) => request<WorkspaceFileContent>(
    `/api/sessions/${encodeURIComponent(id)}/submitted-candidates/${encodeURIComponent(runId)}/files?${new URLSearchParams({path, content: "true"})}`),
  candidateArchiveUrl: (id: string, runId: string) => `${API_ROOT}/api/sessions/${encodeURIComponent(id)}/submitted-candidates/${encodeURIComponent(runId)}/archive`,
  restoreCandidate: (id: string, runId: string, artifactId: string) => request<{ recovery: CandidateRecovery }>(
    `/api/sessions/${encodeURIComponent(id)}/candidate-recoveries/${encodeURIComponent(runId)}/${encodeURIComponent(artifactId)}/restore`,
    { method: "POST", body: JSON.stringify({ confirm: true }) },
  ),
  getTurns: async (id: string, before?: number) => parseTurnsPage(await sessionRequest<unknown>(`/api/sessions/${encodeURIComponent(id)}/turns${before === undefined ? "" : `?before=${before}`}`)),
  forkSession: (id: string, turnId: string, mode: "local" | "worktree", requestId: string, includeIgnored: string[]) =>
    request<{ operation: WorktreeOperation }>(`/api/sessions/${encodeURIComponent(id)}/fork`, {
      method: "POST", body: JSON.stringify({ turnId, mode, requestId, includeIgnored }),
    }),
  createWorktreeSession: (workspace: string, requestId: string) =>
    request<{ operation: WorktreeOperation }>("/api/sessions", {
      method: "POST", body: JSON.stringify({ workspace, scope: "project", mode: "worktree", requestId }),
    }),
  getWorktreeOperation: (id: string) => request<{ operation: WorktreeOperation }>(`/api/worktree-operations/${id}`),
  worktreeChanges: (id: string) => request<WorktreeChanges>(`/api/worktrees/${id}/changes`),
  previewWorktree: (id: string) => request<WorktreeChanges>(`/api/worktrees/${id}/apply-preview`, { method: "POST" }),
  applyWorktree: (id: string, previewId: string, requestId: string) => request<{ operation: WorktreeOperation }>(`/api/worktrees/${id}/apply`, {
    method: "POST", body: JSON.stringify({ previewId, requestId }),
  }),
  bootstrap: () => request<BootstrapPayload>("/api/bootstrap"),
  getSettings: () => request<SettingsView>("/api/settings"),
  getSearchSettings: (signal?: AbortSignal) => request<WebSearchSettingsView>("/api/settings/search", { signal, cache: "no-store" }),
  saveSearchSettings: (payload: WebSearchSettingsUpdate) => request<WebSearchSettingsView>("/api/settings/search", {
    method: "PUT", body: JSON.stringify(payload),
  }),
  refreshSearchUsage: (service: "brave" | "tavily", keyId: string, revision: string, allowSearch: boolean) =>
    request<SearchUsageView>(`/api/settings/search/${service}/keys/${encodeURIComponent(keyId)}/usage`, {
      method: "POST", body: JSON.stringify({ revision, allowSearch }), cache: "no-store",
    }),
  getProviderBalance: async (signal?: AbortSignal) => {
    try {
      return await request<ProviderBalance>("/api/settings/balance", { signal, cache: "no-store" });
    } catch (error) {
      if (error instanceof TypeError) throw new Error("无法查询余额，请检查本机服务和网络后重试。");
      throw error;
    }
  },
  saveSettings: async (payload: SettingsUpdate) => {
    const result = await request<SettingsView>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    if (payload.runtimeNetworkAccess !== undefined && typeof result.runtimeNetworkAccess !== "boolean") {
      throw new Error("当前后端未接入运行设置，请重启 MiniCode 本机服务并刷新页面后重试。");
    }
    if (payload.webAgentSettings && (!result.webAgentSettings || Object.entries(payload.webAgentSettings).some(
      ([key, value]) => result.webAgentSettings?.[key as keyof typeof payload.webAgentSettings] !== value,
    ))) {
      throw new Error("后端未确认保存协作设置，请重启本机服务并刷新页面后重试。");
    }
    return result;
  },
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
  renameNavigationItem: (kind: "workspace" | "session", target: string, name: string) =>
    request<NavigationPayload>("/api/navigation/names", {
      method: "PUT",
      body: JSON.stringify({ kind, target, name }),
    }),
  closeProject: (target: string) =>
    request<ProjectClosePayload>("/api/navigation/workspaces", {
      method: "DELETE",
      body: JSON.stringify({ target }),
    }),
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
    sessionRequest<{ session: SessionSummary; messages: StoredMessage[] }>(
      `/api/sessions/${encodeURIComponent(sessionId)}`,
    ),
  getContextSettings: (sessionId: string | null, scope: ConversationScope, workspace: string) =>
    sessionRequest<ConversationContextView>(sessionId
      ? `/api/sessions/${encodeURIComponent(sessionId)}/context-settings`
      : `/api/context-settings?scope=${scope}${scope === "project" ? `&workspace=${encodeURIComponent(workspace)}` : ""}`),
  saveContextSettings: (sessionId: string, payload: ConversationContext) =>
    sessionRequest<ConversationContextView>(`/api/sessions/${encodeURIComponent(sessionId)}/context-settings`, {
      method: "PUT", body: JSON.stringify(payload),
    }),
  previewContextSettings: (scope: ConversationScope, workspace: string, payload: ConversationContext) =>
    sessionRequest<ConversationContextView>(`/api/context-settings/preview?scope=${scope}${scope === "project" ? `&workspace=${encodeURIComponent(workspace)}` : ""}`, {
      method: "POST", body: JSON.stringify(payload),
    }),
  getSessionInspect: (sessionId: string, signal?: AbortSignal) =>
    sessionRequest<{ session: SessionSummary; inspect: SessionInspect }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/inspect`, { signal },
    ),
  getSessionRun: (sessionId: string, signal?: AbortSignal, compact = false) =>
    sessionRequest<RunSnapshot>(`/api/sessions/${encodeURIComponent(sessionId)}/run${compact ? "?compact=true" : ""}`, { signal }),
  listTrash: () => request<{ sessions: TrashedSession[] }>("/api/trash"),
  archiveSession: (sessionId: string) =>
    request<{ archived: boolean; session: TrashedSession }>(`/api/sessions/${encodeURIComponent(sessionId)}/archive`, {
      method: "POST",
    }),
  restoreSession: (sessionId: string) =>
    request<{ restored: boolean }>(`/api/sessions/${encodeURIComponent(sessionId)}/restore`, {
      method: "POST",
    }),
  deleteSession: (sessionId: string, worktree: "clean" | "keep" | "discard" = "clean") =>
    request<{ deleted: boolean }>(`/api/sessions/${encodeURIComponent(sessionId)}?worktree=${worktree}`, {
      method: "DELETE",
    }),
  startRun: (payload: {
    scope: ConversationScope;
    workspace?: string;
    prompt: string;
    sessionId?: string;
    executionProfile: ExecutionProfile;
    permissionMode?: PermissionMode;
    executionLocation?: ExecutionLocation;
    contextSettings?: ConversationContext;
    taskFile?: TaskFileSelection;
    runBudget?: RunBudget;
    continuation?: { runId: string; artifactId: string };
    resumeQueueFrom?: string;
  }) =>
    request<{ run: RunInfo }>(payload.runBudget ? "/api/runs/budgeted" : "/api/runs", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  previewTaskFile: (payload: { workspace: string; sessionId?: string; scope: "project"; path: string }) =>
    request<TaskFilePreview>("/api/task-files/preview", { method: "POST", body: JSON.stringify(payload) }),
  addRunInput: (runId: string, payload: Pick<RunInput, "inputId" | "kind" | "text" | "executionProfile" | "contextSettings"> & { stage?: boolean }) =>
    request<{ input: RunInput }>(`/api/runs/${runId}/inputs`, { method: "POST", body: JSON.stringify(payload) }),
  cancelRunInput: (runId: string, inputId: string) =>
    request<{ input: RunInput }>(`/api/runs/${runId}/inputs/${encodeURIComponent(inputId)}`, { method: "DELETE" }),
  steerRunInput: (runId: string, inputId: string) =>
    request<{ input: RunInput }>(`/api/runs/${runId}/inputs/${encodeURIComponent(inputId)}/steer`, { method: "POST" }),
  stopRun: (runId: string) =>
    request<{ run: RunInfo }>(`/api/runs/${encodeURIComponent(runId)}/stop`, {
      method: "POST",
    }),
  stopAgent: (runId: string, executionId: string, taskId: string, operationId: string) =>
    request<{ status: string; notice: string }>(`/api/runs/${encodeURIComponent(runId)}/agents/stop`, {
      method: "POST", body: JSON.stringify({ executionId, taskId, operationId }),
    }),
  getRun: (runId: string, after = 0) => {
    const query = after > 0 ? `?after=${after}` : "";
    return sessionRequest<RunSnapshot>(`/api/runs/${encodeURIComponent(runId)}${query}`);
  },
  approve: (
    runId: string,
    payload: { requestId: string; decision: string; feedback?: string },
  ) =>
    request<{ run: RunInfo }>(`/api/runs/${encodeURIComponent(runId)}/approvals`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  answerQuestion: (runId: string, requestId: string, answer: string) =>
    request<{ run: RunInfo }>(`/api/runs/${encodeURIComponent(runId)}/questions`, {
      method: "POST", body: JSON.stringify({ requestId, answer }),
    }),
  answerQuestionGroup: async (runId: string, groupId: string, answers: { requestId: string; answer: string }[]) => {
    if (!answers.length) throw new Error("请填写本组问题的答案。");
    try {
      return await request<{ run: RunInfo }>(`/api/runs/${encodeURIComponent(runId)}/question-groups/${encodeURIComponent(groupId)}`, {
        method: "POST", body: JSON.stringify({ answers }),
      });
    } catch (cause) {
      if (!(cause instanceof ApiError) || ![404, 405].includes(cause.status)) throw cause;
      // A page refresh can load the new UI while an older local server is still
      // running. Send these answers only after the same explicit group submit.
      let result: { run: RunInfo } | undefined;
      for (const answer of answers) result = await request<{ run: RunInfo }>(`/api/runs/${encodeURIComponent(runId)}/questions`, {
        method: "POST", body: JSON.stringify(answer),
      });
      return result!;
    }
  },
  workspaceStatus: (workspace: string) =>
    request<WorkspaceStatus>(`/api/workspace/status?path=${encodeURIComponent(workspace)}`),
  workspaceCapabilities: (workspace: string, scope: ConversationScope = "project") => {
    const query = new URLSearchParams({ scope });
    if (scope === "project") query.set("path", workspace);
    return request<{ capabilities: CapabilitySnapshot }>(
      `/api/workspace/capabilities?${query.toString()}`,
    );
  },
  workspaceFiles: (workspace: string, directory = "") => {
    const query = new URLSearchParams({ path: workspace, directory });
    return request<WorkspaceDirectory>(`/api/workspace/files?${query.toString()}`);
  },
  workspaceFile: (workspace: string, file: string) => {
    const query = new URLSearchParams({ path: workspace, file });
    return request<WorkspaceFileContent>(`/api/workspace/file?${query.toString()}`);
  },
  workspaceImageUrl: (workspace: string, file: string) =>
    `${API_ROOT}/api/workspace/image?${new URLSearchParams({ path: workspace, file })}`,
  workspaceDiff: (workspace: string, file?: string) => {
    const query = new URLSearchParams({ path: workspace });
    if (file) query.set("file", file);
    return request<{ diff: string; file: string }>(`/api/workspace/diff?${query.toString()}`);
  },
  getExtensions: (workspace: string, contextScope: ConversationScope = "project") => {
    const query = new URLSearchParams({ contextScope });
    if (workspace) query.set("workspace", workspace);
    const suffix = query.size ? `?${query.toString()}` : "";
    return request<ExtensionCatalog>(`/api/extensions${suffix}`);
  },
  getSkillInventory: (workspace: string, scope: ConversationScope, invocation?: {workspace: string; scope: ConversationScope}) =>
    request<SkillInventory>(`/api/skills?${skillQueryParams(workspace, scope, invocation)}`),
  previewSkill: (workspace: string, scope: ConversationScope, key: string) =>
    request<SkillPreview>(`/api/skills/${encodeURIComponent(key)}/preview?${new URLSearchParams({ workspace, scope })}`),
  skillCommand: (workspace: string, scope: ConversationScope, key: string, invocation?: {workspace: string; scope: ConversationScope}) =>
    request<{ key: string; command: string }>(`/api/skills/${encodeURIComponent(key)}/command?${skillQueryParams(workspace, scope, invocation)}`),
  installExtension: (payload: {
    source: string;
    workspace?: string;
    contextScope?: ConversationScope;
    scope: ExtensionScope;
    trusted?: boolean;
    grantedPermissions?: string[];
    revision?: string;
  }) =>
    request<ExtensionMutationResponse>("/api/extensions/install", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  installMcpExtension: (payload: {
    name: string;
    transport: "streamable-http" | "stdio";
    workspace?: string;
    contextScope?: ConversationScope;
    scope: ExtensionScope;
    url?: string;
    command?: string;
    args?: string[];
  }) =>
    request<ExtensionMutationResponse>("/api/extensions/install/mcp", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateExtension: (
    scope: ExtensionScope,
    extensionId: string,
    payload: {
      workspace?: string;
      contextScope?: ConversationScope;
      enabled?: boolean;
      trusted?: boolean;
      grantedPermissions?: string[];
    },
  ) =>
    request<ExtensionMutationResponse>(
      `/api/extensions/${encodeURIComponent(scope)}/${encodeURIComponent(extensionId)}`,
      { method: "PATCH", body: JSON.stringify(payload) },
    ),
  getExtensionHealth: (scope: ExtensionScope, extensionId: string, workspace: string, contextScope: ConversationScope = "project") => {
    const query = new URLSearchParams({ contextScope });
    if (workspace) query.set("workspace", workspace);
    const suffix = query.size ? `?${query.toString()}` : "";
    return request<{ extension: ManagedExtension; checks: ExtensionHealthCheck[] }>(
      `/api/extensions/${encodeURIComponent(scope)}/${encodeURIComponent(extensionId)}/health${suffix}`,
    );
  },
  rollbackExtension: (scope: ExtensionScope, extensionId: string, workspace: string, contextScope: ConversationScope = "project") =>
    request<ExtensionMutationResponse>(
      `/api/extensions/${encodeURIComponent(scope)}/${encodeURIComponent(extensionId)}/rollback`,
      { method: "POST", body: JSON.stringify({ workspace, contextScope }) },
    ),
  uninstallExtension: (scope: ExtensionScope, extensionId: string, workspace: string, contextScope: ConversationScope = "project") => {
    const query = new URLSearchParams({ contextScope });
    if (workspace) query.set("workspace", workspace);
    const suffix = query.size ? `?${query.toString()}` : "";
    return request<ExtensionMutationResponse>(
      `/api/extensions/${encodeURIComponent(scope)}/${encodeURIComponent(extensionId)}${suffix}`,
      { method: "DELETE" },
    );
  },
  eventSource: (runId: string, after = 0) => {
    const query = after > 0 ? `?after=${after}` : "";
    return new EventSource(`${API_ROOT}/api/runs/${encodeURIComponent(runId)}/events${query}`);
  },
};

export function parseRunEvent(event: MessageEvent<string>): RunEvent {
  return JSON.parse(event.data) as RunEvent;
}
